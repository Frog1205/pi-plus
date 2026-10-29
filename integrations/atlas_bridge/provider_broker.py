"""Closed provider bridge: Host rebuilds Messages JSON, caches a canonical Pi response."""
import hashlib
import http.client
import re
import threading
import time
from urllib.parse import urlsplit

from .host import TaskError, canonical, digest
from .model_broker import ModelBroker
from .model_probe import strict_json
from .provider_ledger import ProviderLedger
from .provider_policy import ProviderConnection, provider_prompt
from .provider_protocol import normalized_response, parse_provider_response, remote_tools


class ProviderBroker(ModelBroker):
    def __init__(self,host,profile,grant):
        self.host,self.profile,self.grant = host,profile,grant
        self._credential = grant.secret(profile)
        self.connections,self.network_lock = {},threading.Lock()
        self.ledger = ProviderLedger(host,profile)

    def expected_input(self,task_id,before_sequence=None):
        with self.host.lock:
            value = super().expected_input(task_id,before_sequence)
            snapshots = strict_json(self.host.db.execute('SELECT snapshots FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            value[0]['content'][0]['text'] = provider_prompt(self.host.get(task_id),snapshots)
            return value

    def remote_payload(self,task_id,payload,before_sequence=None):
        with self.host.lock:
            messages = [dict(role='user',content=[dict(type='text',text=payload['input'][0]['content'][0]['text'])])]
            sql = "SELECT p.* FROM provider_receipts p JOIN model_requests m ON p.task_id=m.task_id AND p.generation_id=m.generation_id WHERE p.task_id=? AND m.state='received'"
            parameters = [task_id]
            if before_sequence is not None:
                sql += ' AND m.sequence<?'
                parameters.append(before_sequence)
            for row in self.host.db.execute(sql+' ORDER BY m.sequence',parameters).fetchall():
                raw = bytes(row['raw'])
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise TaskError('provider_cache_corrupt',409)
                value = parse_provider_response(raw,self.profile)
                messages.append(dict(role='assistant',content=value['content']))
                for block in value['content']:
                    if block['type'] != 'tool_use':
                        continue
                    operation = 'read:'+str(block['input'].get('document_id')) if block['name']=='read_document' else 'publish:report'
                    receipt = self.host.db.execute("SELECT receipt,hash FROM operations WHERE task_id=? AND operation_id=? AND state='completed'",(task_id,operation)).fetchone()
                    if not receipt or receipt['hash'] != digest(dict(tool=block['name'],parameters=block['input'])):
                        raise TaskError('provider_tool_receipt_missing',409)
                    messages.append(dict(role='user',content=[dict(type='tool_result',tool_use_id=block['id'],content=canonical(strict_json(receipt['receipt'])))]))
            return dict(model=self.profile.remote_model,max_tokens=1024,stream=False,thinking={'type':'disabled'},messages=messages,tools=remote_tools(),tool_choice={'type':'auto'})

    def request(self,task_id,record,*,prepared=None,dispatched=None):
        host,profile = self.host,self.profile
        with host.lock:
            task = host.get(task_id)
            snapshots = strict_json(host.db.execute('SELECT snapshots FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if len(snapshots) != 1 or snapshots[0]['sha256'] != self.grant.source_sha256:
                raise TaskError('provider_source_not_authorized',403)
            deadline = host.deadline(task_id)
            if task['state'] != 'running' or task['profile_hash'] != profile.fingerprint or task['policy_hash'] != host.policy_hash or time.time() > deadline:
                raise TaskError('provider_task_not_executable',403)
            payload,_ = self.validate_request(task_id,record)
            identity = record['generation_id']
            old = host.db.execute('SELECT sequence FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,identity)).fetchone()
            remote = self.remote_payload(task_id,payload,old[0] if old else None)
            bound = max(len(canonical(payload).encode('utf-8')),len(canonical(remote).encode('utf-8'))*2+512)
            if bound > profile.input_byte_limit:
                raise TaskError('provider_context_limit',429)
            request_hash = digest(dict(payload=payload,remote=remote,profile=profile.fingerprint,runtime=host.runtime_hash,protocol=2))
            row = self.ledger.prepare(task_id,identity,request_hash,payload,bound)
            if row['state'] == 'received':
                raw = bytes(row['body'])
                if hashlib.sha256(raw).hexdigest() != row['response_hash']:
                    raise TaskError('provider_cache_corrupt',409)
                host._event(task_id,'model_cache_replayed',{'generation_id':identity})
                return raw
            if prepared:
                prepared()
            self.ledger.dispatch(task_id,identity)
        if dispatched:
            dispatched()
        target = urlsplit(profile.endpoint)
        timeout = max(.05,min(5,deadline-time.time()))
        connection = http.client.HTTPConnection('127.0.0.1',target.port,timeout=timeout) if profile.mode=='model_fixture' else ProviderConnection(target.hostname,timeout=timeout)
        with self.network_lock:
            self.connections[record['id']] = connection
        request_id = None
        try:
            if host.cancelled.is_set():
                raise OSError('cancelled')
            connection.request('POST',target.path,body=canonical(remote).encode('utf-8'),headers={'x-api-key':self._credential,'anthropic-version':'2023-06-01','Content-Type':'application/json'})
            host._event(task_id,'provider_http_sent',{'generation_id':identity,'provider':profile.provider,'host':target.hostname})
            response = connection.getresponse()
            identifier = response.getheader('request-id') or response.getheader('x-request-id')
            request_id = identifier if identifier and re.fullmatch(r'[A-Za-z0-9_.-]{1,192}',identifier) else None
            if response.status != 200:
                code = 'provider_auth' if response.status in (401,403) else 'provider_redirect' if 300<=response.status<400 else 'provider_unknown' if response.status==429 or response.status>=500 else 'provider_rejected'
                self.ledger.fail(task_id,identity,'outcome_unknown' if code=='provider_unknown' else 'rejected',code,request_id)
                raise TaskError(code,409)
            if response.getheader('Content-Type','').split(';')[0].lower() != 'application/json' or (profile.mode=='model_fixture' and response.getheader('X-Atlas-P22-Fixture') != profile.provider):
                raise ValueError('provider response type')
            raw = bytearray()
            while True:
                if host.cancelled.is_set() or time.time() > deadline:
                    raise OSError('cancelled or deadline')
                chunk = response.read1(min(16384,profile.response_byte_limit+1-len(raw)))
                if not chunk:
                    if response.length is not None and response.length > 0:
                        raise http.client.IncompleteRead(bytes(raw),response.length)
                    break
                raw.extend(chunk)
                if len(raw) > profile.response_byte_limit:
                    raise ValueError('provider response limit')
            if self._credential.encode('utf-8') in raw:
                raise ValueError('credential reflection')
            value = parse_provider_response(bytes(raw),profile)
            if self._credential in canonical(value):
                raise ValueError('decoded credential reflection')
            body,normalized = normalized_response(value)
            if len(body) > profile.response_byte_limit:
                raise ValueError('normalized response limit')
            self.ledger.receive_provider(task_id,identity,bytes(raw),body,normalized,value,request_id)
            return body
        except TaskError as error:
            with host.lock:
                state = host.db.execute('SELECT state FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,identity)).fetchone()[0]
            if state == 'dispatching':
                self.ledger.fail(task_id,identity,'outcome_unknown',error.code,request_id)
            raise
        except (OSError,http.client.HTTPException):
            self.ledger.fail(task_id,identity,'outcome_unknown','provider_transport_unknown',request_id)
            raise TaskError('provider_transport_unknown',409) from None
        except (ValueError,KeyError,TypeError):
            self.ledger.fail(task_id,identity,'rejected','provider_response_invalid',request_id)
            raise TaskError('provider_response_invalid',409) from None
        finally:
            with self.network_lock:
                self.connections.pop(record['id'],None)
            connection.close()

    def reconcile(self,task_id,generation_id,evidence,*,fault=None):
        with self.host.lock:
            if self.host.running_task is not None:
                raise TaskError('reconciliation_requires_idle_host',409)
            required = {'task_id','generation_id','profile_hash','request_hash','provider_request_id','response','note'}
            if not isinstance(evidence,dict) or set(evidence) != required or evidence['task_id'] != task_id or evidence['generation_id'] != generation_id or evidence['profile_hash'] != self.profile.fingerprint or not isinstance(evidence['note'],str) or not 1<=len(evidence['note'])<=256:
                raise TaskError('reconciliation_evidence_invalid')
            row = self.host.db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,generation_id)).fetchone()
            if not row or row['request_hash'] != evidence['request_hash'] or row['profile_hash'] != self.profile.fingerprint:
                raise TaskError('reconciliation_identity_conflict',409)
            evidence_hash = digest(evidence)
            old = self.host.db.execute('SELECT * FROM provider_reconciliations WHERE task_id=? AND generation_id=?',(task_id,generation_id)).fetchone()
            if old:
                if old['evidence_hash'] != evidence_hash:
                    raise TaskError('reconciliation_content_conflict',409)
                return strict_json(old['receipt'])
            if row['state'] != 'outcome_unknown' or row['dispatch_count'] != 1 or (row['provider_request_id'] and row['provider_request_id'] != evidence['provider_request_id']):
                raise TaskError('reconciliation_state_conflict',409)
            if evidence['provider_request_id'] is not None and (not isinstance(evidence['provider_request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,192}',evidence['provider_request_id'])):
                raise TaskError('reconciliation_evidence_invalid')
            raw = canonical(evidence['response']).encode('utf-8')
            if len(raw) > self.profile.response_byte_limit or self._credential.encode('utf-8') in raw:
                raise TaskError('reconciliation_evidence_invalid')
            try:
                value = parse_provider_response(raw,self.profile)
                body,normalized = normalized_response(value)
                if len(body) > self.profile.response_byte_limit:
                    raise ValueError('normalized response limit')
            except (ValueError,KeyError,TypeError):
                raise TaskError('reconciliation_response_invalid') from None
            return self.ledger.receive_provider(task_id,generation_id,raw,body,normalized,value,evidence['provider_request_id'],evidence=evidence_hash,fault=fault)
