"""P2.0 fixture-only protocol experiment. Not registered in the production API.

Only 127.0.0.1:<explicit fixture port> is reachable from this broker. The P1
Host, tool gateway, Worker and engine pin remain unchanged.
"""
import base64
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import threading
import time

from .host import AtlasTaskHost, REPO, TaskError, canonical, digest, verify_engine
from .windows import JobProcess

MODEL = 'atlas-fixture-model'


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def validate_sse(body):
    events, items, done, terminal = [], {}, {}, None
    for block in body.decode('utf-8', errors='strict').replace('\r\n','\n').split('\n\n'):
        data = '\n'.join(line[6:] for line in block.splitlines() if line.startswith('data: '))
        if not data or data=='[DONE]':
            continue
        event = strict_json(data)
        if terminal is not None or not isinstance(event,dict):
            raise ValueError('event after terminal')
        kind = event.get('type')
        if kind=='response.output_item.added':
            index, item = event['output_index'], event['item']
            if not isinstance(index,int) or index in items or item['type'] not in ('function_call','message'):
                raise ValueError('invalid item')
            items[index] = item['id']
        elif kind in ('response.function_call_arguments.delta','response.function_call_arguments.done','response.output_text.delta'):
            if event.get('output_index') not in items:
                raise ValueError('unknown output slot')
        elif kind=='response.output_item.done':
            index, item = event['output_index'], event['item']
            if index in done or items.get(index)!=item['id']:
                raise ValueError('item identity')
            if item['type']=='function_call' and not isinstance(strict_json(item['arguments']),dict):
                raise ValueError('invalid tool arguments')
            done[index] = item
        elif kind=='response.completed':
            terminal = event['response']
            if terminal.get('status')!='completed' or terminal.get('model')!=MODEL or not terminal.get('id'):
                raise ValueError('incomplete response')
        elif kind in ('response.incomplete','response.failed','error'):
            raise ValueError('failed response')
        elif kind not in ('response.created','response.in_progress','response.content_part.added','response.content_part.done','response.output_text.done'):
            raise ValueError('unsupported event')
        events.append(event)
    if not terminal or not items or set(items)!=set(done) or terminal.get('output')!=[done[key] for key in sorted(done)]:
        raise ValueError('missing terminal or unfinished item')
    usage = terminal.get('usage')
    if usage is not None and (not isinstance(usage,dict) or any(type(usage.get(key)) is not int or usage[key]<0 for key in ('input_tokens','output_tokens'))):
        raise ValueError('invalid usage')
    return usage


@dataclass(frozen=True)
class FixtureProfile:
    port: int
    token: str

    def __post_init__(self):
        if type(self.port) is not int or not 1<=self.port<=65535 or not self.token.startswith('fixture-host-'):
            raise ValueError('fixture profile required')

    @property
    def fingerprint(self):
        return digest(dict(mode='model_fixture',host='127.0.0.1',port=self.port,model=MODEL,protocol=2))


class FixtureBroker:
    def __init__(self, host, profile):
        self.host, self.profile = host, profile
        self.connections = {}
        with host.lock:
            host.db.execute('''CREATE TABLE IF NOT EXISTS probe_models(task_id TEXT,generation_id TEXT,hash TEXT,
              state TEXT,body BLOB,usage TEXT,PRIMARY KEY(task_id,generation_id))''')
            host.db.execute("UPDATE probe_models SET state='outcome_unknown' WHERE state='dispatching'")

    def blocked(self, task_id):
        with self.host.lock:
            return self.host.db.execute("SELECT 1 FROM probe_models WHERE task_id=? AND state!='received'",(task_id,)).fetchone() is not None

    def request(self, task_id, record):
        if self.host.get(task_id)['state']!='running':
            raise TaskError('model_task_not_executable',403)
        if set(record)!={'protocol','type','id','generation_id','body'} or record['protocol']!=2 or record['type']!='model_request' or not isinstance(record['id'],str) or not 1<=len(record['id'])<=96 or not isinstance(record['generation_id'],str) or not 1<=len(record['generation_id'])<=120:
            raise TaskError('model_request_invalid')
        body = record['body']
        if not isinstance(body,str) or len(body.encode('utf-8'))>131072:
            raise TaskError('model_request_limit')
        payload = strict_json(body)
        if not isinstance(payload,dict) or set(payload)-{'model','input','stream','store','max_output_tokens','tools'} or payload.get('model')!=MODEL or payload.get('stream') is not True or payload.get('store') is not False or payload.get('max_output_tokens')!=1024:
            raise TaskError('model_profile_denied')
        if not isinstance(payload.get('input'),list) or any(not isinstance(item,dict) for item in payload['input']) or not isinstance(payload.get('tools'),list) or len(payload['tools'])!=2 or any(not isinstance(item,dict) or not isinstance(item.get('name'),str) or item.get('type')!='function' for item in payload['tools']) or {item['name'] for item in payload['tools']}!={'read_document','publish_report'}:
            raise TaskError('model_tools_denied')
        request_hash = digest(dict(profile=self.profile.fingerprint,payload=payload))
        identity = record['generation_id']
        with self.host.lock:
            old = self.host.db.execute('SELECT * FROM probe_models WHERE task_id=? AND generation_id=?',(task_id,identity)).fetchone()
            if old:
                if old['hash']!=request_hash:
                    raise TaskError('model_request_content_conflict',409)
                if old['state']!='received':
                    raise TaskError('model_outcome_unknown',409)
                self.host._event(task_id,'model_cache_replayed',{'generation_id':identity})
                return bytes(old['body'])
            if self.blocked(task_id):
                raise TaskError('model_outcome_unknown',409)
            if self.host.db.execute('SELECT COUNT(*) FROM probe_models WHERE task_id=?',(task_id,)).fetchone()[0]>=4:
                raise TaskError('model_attempt_budget',429)
            self.host.db.execute("INSERT INTO probe_models VALUES(?,?,?,'dispatching',NULL,NULL)",(task_id,identity,request_hash))
            self.host._event(task_id,'model_dispatching',{'generation_id':identity})
        connection = http.client.HTTPConnection('127.0.0.1',self.profile.port,timeout=5)
        self.connections[record['id']] = connection
        try:
            connection.request('POST','/v1/responses',body=canonical(payload).encode('utf-8'),headers={'Authorization':'Bearer '+self.profile.token,'Content-Type':'application/json'})
            response = connection.getresponse()
            if response.status!=200:
                with self.host.lock:
                    self.host.db.execute("UPDATE probe_models SET state='rejected' WHERE task_id=? AND generation_id=?",(task_id,identity))
                raise TaskError('fixture_http_'+str(response.status),response.status)
            if not response.getheader('Content-Type','').startswith('text/event-stream') or response.getheader('X-Atlas-Fixture')!='responses-probe-v2':
                raise ValueError('content type')
            raw = response.read(262145)
            if len(raw)>262144:
                raise ValueError('response limit')
            usage = validate_sse(raw)
            with self.host.lock:
                self.host.db.execute('BEGIN IMMEDIATE')
                try:
                    self.host.db.execute("UPDATE probe_models SET state='received',body=?,usage=? WHERE task_id=? AND generation_id=?",(raw,canonical(usage),task_id,identity))
                    self.host._event(task_id,'model_received',{'generation_id':identity,'sha256':hashlib.sha256(raw).hexdigest(),'usage_available':usage is not None})
                    self.host.db.execute('COMMIT')
                except BaseException:
                    self.host.db.execute('ROLLBACK')
                    raise
            return raw
        except TaskError:
            raise
        except (OSError, http.client.HTTPException):
            with self.host.lock:
                self.host.db.execute("UPDATE probe_models SET state='outcome_unknown' WHERE task_id=? AND generation_id=?",(task_id,identity))
            raise TaskError('model_outcome_unknown',409) from None
        except (ValueError, KeyError, TypeError):
            with self.host.lock:
                self.host.db.execute("UPDATE probe_models SET state='rejected' WHERE task_id=? AND generation_id=?",(task_id,identity))
            raise TaskError('model_response_invalid',409) from None
        finally:
            self.connections.pop(record['id'],None)
            connection.close()

    def abort(self, identity=None):
        for key, connection in list(self.connections.items()):
            if identity is None or key==identity:
                if connection.sock:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()


class ProbeHost(AtlasTaskHost):
    """Explicit programmatic experiment; production task configuration cannot select it."""
    def __init__(self,configuration):
        if configuration.get('project_id')!='fixture':
            raise TaskError('probe_fixture_project_required')
        super().__init__(configuration)

    def _schedule(self, task_id):
        pass

    def attach_fixture(self, profile):
        self.broker = FixtureBroker(self,profile)
        self.probe_cancel = threading.Event()
        self.trace = []
        self.stderr = ''
        self.job = None

    def run_probe(self, task_id, *, after_model=None, after_tool=None, transform=None):
        pin = json.loads((REPO/'packages/atlas-runtime/p2-probe-lock.json').read_text('utf-8'))
        for name, sha in pin['files'].items():
            if hashlib.sha256((REPO/name).read_bytes()).hexdigest()!=sha:
                raise TaskError('probe_pin_mismatch',503)
        verify_engine()
        if self.broker.blocked(task_id):
            self._state(task_id,'interrupted','model_outcome_unknown')
            return self.probe_status(task_id)
        self.probe_cancel.clear()
        self._state(task_id,'running')
        runtime = self.state/'probe-workers'/task_id
        runtime.mkdir(parents=True,exist_ok=True)
        environment = {key:os.environ[key] for key in ('SystemRoot','WINDIR','COMSPEC','PATHEXT') if key in os.environ}
        environment.update(HOME=str(runtime),USERPROFILE=str(runtime),TEMP=str(runtime),TMP=str(runtime),PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true')
        arguments = [self.node,'--permission'] + ['--allow-fs-read='+str(path) for path in (REPO/'packages',REPO/'node_modules',REPO/'package.json',REPO/'tsconfig.json',runtime)]
        arguments += ['--allow-fs-write='+str(runtime),'--import',(REPO/'packages/coding-agent/src/experimental/source-resolver.ts').as_uri(),str(REPO/'packages/atlas-runtime/test/p2-probe-worker.ts')]
        records, readers, requests = queue.Queue(maxsize=64), [], []
        result, cancelled_at = None, None
        job = self.job = JobProcess(arguments,runtime,environment)
        def send(record):
            job.stdin.write((canonical({'protocol':2,**record})+'\n').encode('utf-8'))
        def output():
            try:
                while line:=job.stdout.readline(1048577):
                    if len(line)>1048576:
                        job.kill()
                        return
                    records.put(strict_json(line),timeout=1)
            except (ValueError,OSError,queue.Full):
                job.kill()
        def errors():
            captured = bytearray()
            try:
                while chunk:=job.stderr.read(4096):
                    if len(captured)<16384:
                        captured.extend(chunk[:16384-len(captured)])
            except (OSError,ValueError):
                pass
            self.stderr = captured.decode('utf-8',errors='replace')
        def model(record):
            try:
                body = self.broker.request(task_id,record)
                records.put({'type':'model_ready','id':record['id'],'generation_id':record['generation_id'],'body':body},timeout=1)
            except TaskError as error:
                records.put({'type':'model_error','id':record['id'],'code':error.code},timeout=1)
            except (ValueError,TypeError,KeyError):
                records.put({'type':'model_error','id':record['id'],'code':'model_request_invalid'},timeout=1)
        for target in (output,errors):
            reader = threading.Thread(target=target,daemon=True)
            reader.start()
            readers.append(reader)
        try:
            task = self.get(task_id)
            send(dict(type='start',mode='model_fixture',run_id=task_id,request_id=task['request_id'],goal=task['goal']))
            deadline = time.monotonic()+self.timeout
            while result is None:
                if self.probe_cancel.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    send({'type':'cancel'})
                if time.monotonic()>deadline or (cancelled_at and time.monotonic()-cancelled_at>3):
                    raise TaskError('probe_cancelled' if cancelled_at else 'probe_timeout')
                try:
                    record = records.get(timeout=.05)
                except queue.Empty:
                    if job.poll() is not None:
                        raise TaskError('probe_worker_exited')
                    continue
                kind = record.get('type')
                if kind not in ('model_ready','model_error') and record.get('protocol')!=2:
                    raise TaskError('probe_protocol')
                self.trace.append({key:value for key,value in record.items() if key not in ('body','parameters')})
                if kind=='generation':
                    continue
                if kind=='model_request':
                    request = threading.Thread(target=model,args=(record,),daemon=True)
                    requests.append(request)
                    request.start()
                elif kind=='model_abort':
                    with self.lock:
                        self._event(task_id,'model_abort_received')
                    self.broker.abort(record['id'])
                elif kind in ('model_ready','model_error'):
                    if kind=='model_ready':
                        if after_model:
                            after_model(task_id,record['generation_id'],job)
                        body, status = record['body'],200
                    else:
                        body, status = canonical({'error':{'message':record['code'],'type':'broker_denied'}}).encode(),403
                    frames = [dict(type='model_start',id=record['id'],status=status)]
                    frames += [dict(type='model_chunk',id=record['id'],sequence=index//16384,data=base64.b64encode(body[index:index+16384]).decode()) for index in range(0,len(body),16384)]
                    frames.append(dict(type='model_end',id=record['id'],size=len(body),sha256=hashlib.sha256(body).hexdigest()))
                    if transform:
                        frames = transform(frames)
                    for frame in frames:
                        send(frame)
                elif kind=='tool_request':
                    try:
                        receipt = self.gateway(task_id,record['tool'],record['parameters'])
                        if after_tool:
                            after_tool(task_id,record['tool'],job)
                        send(dict(type='tool_result',id=record['id'],ok=True,result=receipt))
                    except TaskError as error:
                        self._event(task_id,'tool_denied',{'code':error.code})
                        send(dict(type='tool_result',id=record['id'],ok=False,error=error.code))
                elif kind=='result':
                    result = record
                else:
                    raise TaskError('probe_worker_failed')
            artifact = self.db.execute('SELECT 1 FROM artifacts WHERE task_id=?',(task_id,)).fetchone()
            denied = self.db.execute("SELECT 1 FROM events WHERE task_id=? AND type='tool_denied'",(task_id,)).fetchone()
            success = result['status']=='done' and artifact and not denied and not result['denied'] and not result['protocol_error'] and result['native_fetch_calls']==0 and not self.broker.blocked(task_id)
            unknown = self.db.execute("SELECT 1 FROM probe_models WHERE task_id=? AND state='outcome_unknown'",(task_id,)).fetchone()
            self._state(task_id,'cancelled' if cancelled_at else 'completed' if success else 'interrupted' if unknown else 'failed',None if success else 'model_outcome_unknown' if unknown else 'probe_contract')
        except (TaskError,OSError,ValueError) as error:
            self._state(task_id,'cancelled' if self.probe_cancel.is_set() else 'interrupted',error.code if isinstance(error,TaskError) else 'probe_transport')
        finally:
            job.close()
            self.broker.abort()
            for thread in requests+readers:
                thread.join(6)
            self.job = None
        return {**self.probe_status(task_id),'worker_result':result}

    def probe_status(self, task_id):
        return {**self.get(task_id),'mode':'model_fixture','model_connected':False,'probe_only':True}
