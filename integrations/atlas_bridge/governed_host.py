"""P2.1 governed Host entry. Explicit fixture grant only; P1 remains the API default."""
import base64
from dataclasses import asdict
import hashlib
import json
import os
import queue
import sqlite3
import threading
import time

from .host import AtlasTaskHost, REPO, TaskError, canonical, digest, verify_engine
from .model_broker import ModelBroker, checked_response
from .model_ledger import migrate
from .model_policy import ModelProfile, task_prompt
from .model_probe import FixtureProfile, strict_json
from .windows import JobProcess


def verify_governance():
    pin = json.loads((REPO/'packages/atlas-runtime/governance-lock.json').read_text('utf-8'))
    verify_engine()
    for name, expected in pin['files'].items():
        if hashlib.sha256((REPO/name).read_bytes()).hexdigest() != expected:
            raise TaskError('governance_pin_mismatch',503)
    return digest(pin)


class GovernedHost(AtlasTaskHost):
    def __init__(self, configuration, profile, fixture_grant, *, migration_fault=None):
        self._ready = False
        if not isinstance(profile,ModelProfile) or profile.mode != 'model_fixture' or not isinstance(fixture_grant,FixtureProfile) or configuration.get('project_id') != 'fixture':
            raise TaskError('live_model_not_enabled',403)
        if sorted(profile.documents) != sorted(configuration['documents']) or configuration.get('timeout_seconds',30) != profile.timeout_seconds:
            raise TaskError('profile_host_contract_mismatch')
        self.profile, self.runtime_hash = profile, verify_governance()
        super().__init__(configuration)
        try:
            migrate(self,fault=migration_fault)
            self.policy_hash = digest(dict(base=self.policy_hash,profile=profile.fingerprint,runtime=self.runtime_hash,mode=profile.mode,governance=1))
            self.cancelled, self.trace, self.stderr = threading.Event(), [], ''
            self.job, self.run_thread, self.running_task = None, None, None
            self.broker = ModelBroker(self,profile,fixture_grant)
            self._ready = True
        except BaseException:
            self.db.close()
            self.owner.close()
            raise

    def _schedule(self, task_id):
        # This explicit programmatic entry does not auto-run or upgrade legacy tasks.
        pass

    def _event(self, task_id, kind, detail=None):
        with self.lock:
            if self._ready and kind == 'admitted':
                self.db.execute('INSERT INTO model_tasks VALUES(?,?,?,?,?,?)',
                                (task_id,self.profile.mode,self.profile.fingerprint,canonical(asdict(self.profile)),self.runtime_hash,
                                 canonical(dict(protocol=2,governance=1,model=self.profile.model,prompt=self.profile.prompt_version))))
                detail = dict(mode=self.profile.mode,profile_hash=self.profile.fingerprint,runtime_hash=self.runtime_hash,policy_hash=self.policy_hash)
            super()._event(task_id,kind,detail)

    def get(self, task_id):
        value = super().get(task_id)
        if self._ready:
            with self.lock:
                row = self.db.execute('SELECT * FROM model_tasks WHERE task_id=?',(task_id,)).fetchone()
                if not row:
                    raise TaskError('task_provenance_missing',409)
                value.update(mode=row['mode'],profile_hash=row['profile_hash'],runtime_hash=row['runtime_hash'],model_connected=False,
                             provenance=strict_json(row['provenance']))
                if row['profile_hash']:
                    value['model'] = strict_json(row['profile'])['model']
                    value['model_budget'] = self.broker.ledger.status(task_id)
        return value

    def deadline(self, task_id):
        with self.lock:
            return self.db.execute('SELECT created FROM tasks WHERE id=?',(task_id,)).fetchone()[0] + self.profile.timeout_seconds

    def gateway(self, task_id, tool, parameters):
        with self.lock:
            if self.cancelled.is_set() or time.time() > self.deadline(task_id) or self.broker.blocked(task_id):
                raise TaskError('governed_tool_not_executable',403)
            # A Worker can only execute a call present in a committed model response.
            permitted = False
            for row in self.db.execute("SELECT body FROM model_requests WHERE task_id=? AND state='received'",(task_id,)).fetchall():
                for item in checked_response(bytes(row[0]))['output']:
                    if item['type'] == 'function_call' and item['name'] == tool and strict_json(item['arguments']) == parameters:
                        permitted = True
            if not permitted:
                raise TaskError('model_tool_call_not_committed',403)
            if self.db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND type='governed_tool_call'",(task_id,)).fetchone()[0] >= 12:
                raise TaskError('persistent_tool_budget_exceeded',403)
            self._event(task_id,'governed_tool_call',{'tool':tool})
            if tool == 'publish_report':
                snapshots = strict_json(self.db.execute('SELECT snapshots FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
                content = parameters.get('content') if isinstance(parameters,dict) else None
                if not isinstance(content,str) or 'model_fixture' not in content or any(item['sha256'] not in content for item in snapshots):
                    raise TaskError('report_source_contract',403)
            return super().gateway(task_id,tool,parameters)

    def cancel(self, task_id):
        with self.lock:
            self.get(task_id)
            if self.running_task == task_id:
                self.cancelled.set()
            elif self.get(task_id)['state'] in ('queued','interrupted'):
                self._state(task_id,'cancelled')
            return self.get(task_id)

    def resume(self, task_id):
        if self.get(task_id)['state'] != 'interrupted':
            raise TaskError('task_not_resumable',409)
        return self.run(task_id)

    def run(self, task_id, *, after_model=None, after_tool=None):
        if not self.slot.acquire(blocking=False):
            raise TaskError('host_busy',409)
        self.run_thread = threading.current_thread()
        self.running_task = task_id
        try:
            return self._run(task_id,after_model,after_tool)
        finally:
            self.run_thread = None
            self.running_task = None
            self.slot.release()

    def _run(self, task_id, after_model, after_tool):
        if verify_governance() != self.runtime_hash:
            raise TaskError('governance_runtime_changed',503)
        task = self.get(task_id)
        if task['profile_hash'] != self.profile.fingerprint or task['runtime_hash'] != self.runtime_hash or task['policy_hash'] != self.policy_hash:
            raise TaskError('task_profile_conflict',409)
        if task['state'] == 'completed':
            return task
        if task['state'] not in ('queued','interrupted'):
            raise TaskError('task_not_executable',409)
        if self.broker.blocked(task_id):
            budget = self.broker.ledger.status(task_id)
            self._state(task_id,'interrupted' if budget['requires_reconciliation'] else 'failed','model_requires_reconciliation')
            return self.get(task_id)
        if time.time() > self.deadline(task_id):
            self._state(task_id,'failed','model_deadline_exceeded')
            return self.get(task_id)
        self.cancelled.clear()
        self.trace, self.stderr = [], ''
        self._state(task_id,'running')
        runtime = self.state/'governed-workers'/task_id
        runtime.mkdir(parents=True,exist_ok=True)
        environment = {key:os.environ[key] for key in ('SystemRoot','WINDIR','COMSPEC','PATHEXT') if key in os.environ}
        environment.update(HOME=str(runtime),USERPROFILE=str(runtime),TEMP=str(runtime),TMP=str(runtime),PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true')
        arguments = [self.node,'--permission'] + ['--allow-fs-read='+str(path) for path in (REPO/'packages',REPO/'node_modules',REPO/'package.json',REPO/'tsconfig.json',runtime)]
        arguments += ['--allow-fs-write='+str(runtime),'--import',(REPO/'packages/coding-agent/src/experimental/source-resolver.ts').as_uri(),str(REPO/'packages/atlas-runtime/src/governed-worker.ts')]
        records, readers, requests = queue.Queue(maxsize=64), [], []
        result, cancelled_at, active_model, calls = None, None, None, 0
        job = self.job = JobProcess(arguments,runtime,environment)
        def send(record):
            job.stdin.write((canonical({'protocol':2,**record})+'\n').encode('utf-8'))
        def output():
            try:
                while line := job.stdout.readline(1048577):
                    if len(line) > 1048576:
                        job.kill()
                        return
                    records.put(strict_json(line),timeout=1)
            except (ValueError,OSError,queue.Full):
                job.kill()
        def errors():
            captured = bytearray()
            try:
                while chunk := job.stderr.read(4096):
                    if len(captured) < 16384:
                        captured.extend(chunk[:16384-len(captured)])
            except (OSError,ValueError):
                pass
            self.stderr = captured.decode('utf-8',errors='replace')
        def model(record):
            try:
                body = self.broker.request(task_id,record)
                records.put(dict(type='model_ready',id=record['id'],generation_id=record['generation_id'],body=body),timeout=1)
            except TaskError as error:
                records.put(dict(type='model_error',id=record['id'],code=error.code),timeout=1)
            except (ValueError,TypeError,KeyError):
                records.put(dict(type='model_error',id=record.get('id'),code='model_request_invalid'),timeout=1)
            except sqlite3.Error:
                records.put(dict(type='model_error',id=record.get('id'),code='model_storage_interrupted'),timeout=1)
        for target in (output,errors):
            reader = threading.Thread(target=target,daemon=True)
            reader.start()
            readers.append(reader)
        try:
            with self.lock:
                snapshots = strict_json(self.db.execute('SELECT snapshots FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            send(dict(type='start',mode=self.profile.mode,run_id=task_id,request_id=task['request_id'],goal=task_prompt(task,snapshots)))
            while result is None:
                if self.cancelled.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    send(dict(type='cancel'))
                if self.closing or time.time() > self.deadline(task_id) or (cancelled_at and time.monotonic()-cancelled_at > 3):
                    raise TaskError('model_deadline_exceeded' if not cancelled_at else 'cancelled',409)
                try:
                    record = records.get(timeout=.05)
                except queue.Empty:
                    if job.poll() is not None:
                        raise TaskError('worker_exited',409)
                    continue
                if not isinstance(record,dict):
                    raise TaskError('governed_protocol')
                kind = record.get('type')
                if kind not in ('model_ready','model_error') and record.get('protocol') != 2:
                    raise TaskError('governed_protocol')
                self.trace.append({key:value for key,value in record.items() if key not in ('body','parameters')})
                if kind == 'generation':
                    continue
                if kind == 'model_request':
                    if active_model is not None:
                        raise TaskError('parallel_models_denied')
                    active_model = record.get('id')
                    request = threading.Thread(target=model,args=(record,),daemon=True)
                    requests.append(request)
                    request.start()
                elif kind == 'model_abort':
                    self._event(task_id,'model_abort_received')
                    self.broker.abort(record['id'])
                elif kind in ('model_ready','model_error'):
                    if kind == 'model_error':
                        raise TaskError(record['code'],409)
                    if after_model:
                        after_model(task_id,record['generation_id'],job)
                    body = record['body']
                    send(dict(type='model_start',id=record['id'],status=200))
                    for index in range(0,len(body),16384):
                        send(dict(type='model_chunk',id=record['id'],sequence=index//16384,data=base64.b64encode(body[index:index+16384]).decode()))
                    send(dict(type='model_end',id=record['id'],size=len(body),sha256=hashlib.sha256(body).hexdigest()))
                    active_model = None
                elif kind == 'tool_request':
                    calls += 1
                    if calls > 12 or not isinstance(record.get('id'),str) or active_model is not None:
                        raise TaskError('tool_budget_or_order_denied')
                    try:
                        receipt = self.gateway(task_id,record.get('tool'),record.get('parameters'))
                        if after_tool:
                            after_tool(task_id,record['tool'],job)
                        send(dict(type='tool_result',id=record['id'],ok=True,result=receipt))
                    except TaskError as error:
                        self._event(task_id,'tool_denied',{'code':error.code})
                        raise
                elif kind == 'result':
                    result = record
                else:
                    raise TaskError('governed_worker_failed')
            with self.lock:
                artifact = self.db.execute('SELECT 1 FROM artifacts WHERE task_id=?',(task_id,)).fetchone()
                denied = self.db.execute("SELECT 1 FROM events WHERE task_id=? AND type='tool_denied'",(task_id,)).fetchone()
                terminal = self.db.execute("SELECT body FROM model_requests WHERE task_id=? AND state='received' ORDER BY sequence DESC LIMIT 1",(task_id,)).fetchone()
            complete = terminal and checked_response(bytes(terminal[0]))['output'][0]['type'] == 'message'
            if not (result.get('status') == 'done' and artifact and complete and not denied and not result.get('denied') and not result.get('protocol_error') and result.get('native_fetch_calls') == 0 and not self.broker.blocked(task_id) and not self.cancelled.is_set()):
                raise TaskError('governed_task_contract',409)
            self._state(task_id,'completed')
        except (TaskError,OSError,ValueError,sqlite3.Error) as error:
            self.broker.abort()
            for request in requests:
                request.join(6)
            unknown = self.broker.ledger.status(task_id)['requires_reconciliation']
            code = error.code if isinstance(error,TaskError) else 'worker_transport_interrupted'
            self._state(task_id,'cancelled' if self.cancelled.is_set() else 'interrupted' if unknown or code in ('worker_exited','worker_transport_interrupted') else 'failed',code)
        finally:
            job.close()
            self.broker.abort()
            for thread in requests+readers:
                thread.join(6)
            self.job = None
        return {**self.get(task_id),'worker_result':result}

    def close(self):
        self.cancelled.set()
        self.broker.abort()
        if self.job:
            self.job.kill()
        if self.run_thread and self.run_thread is not threading.current_thread():
            self.run_thread.join(15)
            if self.run_thread.is_alive():
                raise RuntimeError('Governed Worker still stopping; retaining ownership')
        super().close()
