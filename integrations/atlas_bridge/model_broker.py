"""Host-owned request reconstruction and model dispatch. Live activation stays closed."""
import hashlib
import http.client
import re
import socket
import threading
import time

from .host import TaskError, canonical, digest
from .model_ledger import ModelLedger
from .model_policy import MODEL, TOOLS, parse_target, task_prompt
from .model_probe import strict_json, validate_sse


def checked_response(raw):
    validate_sse(raw)
    arguments, texts, response = {}, {}, None
    for block in raw.decode('utf-8').replace('\r\n','\n').split('\n\n'):
        data = '\n'.join(line[6:] for line in block.splitlines() if line.startswith('data: '))
        if not data or data == '[DONE]':
            continue
        event = strict_json(data)
        kind, index = event['type'], event.get('output_index')
        if kind == 'response.function_call_arguments.delta':
            arguments[index] = arguments.get(index,'') + event['delta']
        elif kind == 'response.output_text.delta':
            texts[index] = texts.get(index,'') + event['delta']
        elif kind == 'response.function_call_arguments.done':
            if arguments.get(index) != event['arguments']:
                raise ValueError('arguments differ from deltas')
        elif kind == 'response.output_item.done':
            item = event['item']
            if item.get('status') != 'completed':
                raise ValueError('unfinished item')
            if item['type'] == 'function_call':
                if arguments.get(index) != item['arguments'] or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,48}',item[key]) for key in ('id','call_id','name')):
                    raise ValueError('tool identity or delta mismatch')
            elif item.get('role') != 'assistant' or item.get('content') != [dict(type='output_text', text=texts.get(index,''), annotations=[])]:
                raise ValueError('message delta mismatch')
        elif kind == 'response.completed':
            response = event['response']
    if len(response['output']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',response['id']):
        raise ValueError('unvalidated output shape')
    return response


class ModelBroker:
    def __init__(self, host, profile, fixture_grant):
        self.host, self.profile, self.grant = host, profile, fixture_grant
        if not isinstance(fixture_grant.token,str) or not re.fullmatch(r'fixture-host-[0-9a-f]{32}',fixture_grant.token):
            raise TaskError('fixture_credential_invalid',403)
        hostname, port, _ = parse_target(profile.endpoint, fixture=True)
        if profile.mode != 'model_fixture' or host.project_id != 'fixture' or hostname != '127.0.0.1' or port != fixture_grant.port:
            raise TaskError('fixture_grant_mismatch',403)
        self.ledger = ModelLedger(host,profile)
        self.connections, self.network_lock = {}, threading.Lock()

    def blocked(self, task_id):
        with self.host.lock:
            return self.ledger.blocked(task_id)

    def expected_input(self, task_id, before_sequence=None):
        db = self.host.db
        task = self.host.get(task_id)
        snapshots = strict_json(db.execute('SELECT snapshots FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
        result = [dict(role='user',content=[dict(type='input_text',text=task_prompt(task,snapshots))])]
        query = "SELECT * FROM model_requests WHERE task_id=? AND state='received'"
        parameters = [task_id]
        if before_sequence is not None:
            query += ' AND sequence<?'
            parameters.append(before_sequence)
        for row in db.execute(query + ' ORDER BY sequence', parameters).fetchall():
            response = checked_response(bytes(row['body']))
            for item in response['output']:
                if item['type'] != 'function_call':
                    raise TaskError('model_conversation_complete',409)
                args = strict_json(item['arguments'])
                # The fixed Responses adapter preserves this same-provider ID pair.
                call_id = item['call_id']
                result.append(dict(type='function_call',id=item['id'],call_id=call_id,name=item['name'],arguments=canonical(args)))
                operation = 'read:' + str(args.get('document_id')) if item['name'] == 'read_document' else 'publish:report' if item['name'] == 'publish_report' else None
                receipt = db.execute("SELECT * FROM operations WHERE task_id=? AND operation_id=? AND state='completed'",(task_id,operation)).fetchone()
                if not receipt or receipt['hash'] != digest(dict(tool=item['name'],parameters=args)):
                    raise TaskError('model_tool_receipt_missing',409)
                result.append(dict(type='function_call_output',call_id=call_id,output=canonical(strict_json(receipt['receipt']))))
        return result

    def validate_request(self, task_id, record):
        if not isinstance(record,dict) or set(record) != {'protocol','type','id','generation_id','body'} or record['protocol'] != 2 or record['type'] != 'model_request' or not isinstance(record['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',record['id']) or not isinstance(record['generation_id'],str) or not re.fullmatch(r'[0-9]{1,20}',record['generation_id']):
            raise TaskError('model_request_invalid')
        if not isinstance(record['body'],str) or len(record['body'].encode('utf-8')) > 131072:
            raise TaskError('model_request_limit')
        try:
            payload = strict_json(record['body'])
        except (ValueError,UnicodeError):
            raise TaskError('model_request_invalid') from None
        if not isinstance(payload,dict) or set(payload) != {'model','input','stream','store','max_output_tokens','tools'} or payload['model'] != self.profile.model or payload['stream'] is not True or payload['store'] is not False or type(payload['max_output_tokens']) is not int or payload['max_output_tokens'] != self.profile.max_output_tokens:
            raise TaskError('model_profile_denied')
        if payload['tools'] != TOOLS:
            raise TaskError('model_schema_denied')
        with self.host.lock:
            old = self.host.db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,record['generation_id'])).fetchone()
            expected = self.expected_input(task_id,old['sequence'] if old else None)
        # Only argument/output JSON encoding whitespace is non-semantic. No extra fields.
        if not isinstance(payload['input'],list) or len(payload['input']) != len(expected):
            raise TaskError('model_context_denied')
        normalized = []
        for actual, trusted in zip(payload['input'],expected):
            if not isinstance(actual,dict) or set(actual) != set(trusted):
                raise TaskError('model_context_denied')
            value = dict(actual)
            for field in ('arguments','output'):
                if field in value:
                    try:
                        value[field] = canonical(strict_json(value[field]))
                    except (ValueError,TypeError):
                        raise TaskError('model_context_denied') from None
            normalized.append(value)
        if normalized != expected:
            raise TaskError('model_context_denied')
        trusted = dict(model=self.profile.model,input=expected,stream=True,store=False,max_output_tokens=self.profile.max_output_tokens,tools=TOOLS)
        # Conservative byte-based token bound for this fixture contract; no guessed live tokenizer.
        bound = len(canonical(trusted).encode('utf-8'))
        if bound > self.profile.input_byte_limit:
            raise TaskError('model_context_budget',429)
        return trusted, bound

    def request(self, task_id, record, *, prepared=None, dispatched=None):
        host, profile = self.host, self.profile
        with host.lock:
            task = host.get(task_id)
            if task['state'] != 'running' or task['profile_hash'] != profile.fingerprint or task['policy_hash'] != host.policy_hash:
                raise TaskError('model_task_not_executable',403)
            if time.time() > host.deadline(task_id):
                raise TaskError('model_deadline_exceeded',504)
            payload, input_bound = self.validate_request(task_id,record)
            identity = record['generation_id']
            request_hash = digest(dict(payload=payload,profile=profile.fingerprint,runtime=host.runtime_hash,protocol=2))
            row = self.ledger.prepare(task_id,identity,request_hash,payload,input_bound)
            if row['state'] == 'received':
                raw = bytes(row['body'])
                if hashlib.sha256(raw).hexdigest() != row['response_hash']:
                    raise TaskError('model_cache_corrupt',409)
                host._event(task_id,'model_cache_replayed',{'generation_id':identity})
                return raw
            if prepared:
                prepared()
            self.ledger.dispatch(task_id,identity)
        if dispatched:
            dispatched()
        _, port, path = parse_target(profile.endpoint,fixture=True)
        connection = http.client.HTTPConnection('127.0.0.1',port,timeout=max(.05,min(5,host.deadline(task_id)-time.time())))
        with self.network_lock:
            self.connections[record['id']] = connection
        request_id = None
        try:
            if host.cancelled.is_set():
                raise OSError('cancelled before send')
            connection.request('POST',path,body=canonical(payload).encode('utf-8'),headers={'Authorization':'Bearer '+self.grant.token,'Content-Type':'application/json'})
            response = connection.getresponse()
            identifier = response.getheader('x-request-id')
            request_id = identifier if identifier and re.fullmatch(r'[A-Za-z0-9_.-]{1,96}',identifier) else None
            if response.status != 200:
                code = 'model_http_auth' if response.status in (401,403) else 'model_http_redirect' if 300 <= response.status < 400 else 'model_http_unknown' if response.status == 429 or response.status >= 500 else 'model_http_rejected'
                self.ledger.fail(task_id,identity,'outcome_unknown' if code == 'model_http_unknown' else 'rejected',code,request_id)
                raise TaskError(code,409)
            if not response.getheader('Content-Type','').startswith('text/event-stream') or response.getheader('X-Atlas-Fixture') != 'responses-probe-v2':
                raise ValueError('fixture marker missing')
            raw = bytearray()
            while True:
                if host.cancelled.is_set() or time.time() > host.deadline(task_id):
                    raise OSError('cancelled or deadline')
                chunk = response.read1(min(16384,profile.response_byte_limit+1-len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > profile.response_byte_limit:
                    raise ValueError('response exceeds budget')
            result = checked_response(bytes(raw))
            if self.grant.token.encode('utf-8') in raw:
                raise ValueError('credential reflected in response')
            self.ledger.receive(task_id,identity,bytes(raw),result,hashlib.sha256(raw).hexdigest(),request_id)
            return bytes(raw)
        except TaskError:
            raise
        except (OSError,http.client.HTTPException):
            self.ledger.fail(task_id,identity,'outcome_unknown','model_transport_unknown',request_id)
            raise TaskError('model_transport_unknown',409) from None
        except (ValueError,KeyError,TypeError):
            self.ledger.fail(task_id,identity,'rejected','model_response_invalid',request_id)
            raise TaskError('model_response_invalid',409) from None
        finally:
            with self.network_lock:
                self.connections.pop(record['id'],None)
            connection.close()

    def abort(self, identity=None):
        with self.network_lock:
            for key, connection in list(self.connections.items()):
                if identity is None or key == identity:
                    if connection.sock:
                        try:
                            connection.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    connection.close()
