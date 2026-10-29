"""Two-provider deterministic Messages HTTP faults. No upstream model calls."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import select
import socket
import threading
import uuid


class ProviderServer:
    def __init__(self,provider='deepseek',scenario='normal'):
        self.provider,self.scenario = provider,scenario
        self.token = 'fixture-host-'+uuid.uuid4().hex
        self.requests,self.responses = [],[]
        self.arrived,self.closed = threading.Event(),threading.Event()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):
                pass
            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError,ConnectionAbortedError,BrokenPipeError):
                    fixture.closed.set()
            def do_POST(self):
                self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append(dict(path=self.path,authorized=self.headers.get('x-api-key')==fixture.token,payload=payload))
                response = fixture.answer(payload)
                raw = json.dumps(response,ensure_ascii=False,separators=(',',':')).encode('utf-8')
                fixture.responses.append(response)
                fixture.arrived.set()
                scenario = fixture.scenario
                if scenario.startswith('http_') or scenario=='redirect':
                    status=307 if scenario=='redirect' else int(scenario[5:])
                    self.send_response(status)
                    self.send_header('Content-Length','0')
                    if scenario=='redirect':
                        self.send_header('Location','http://169.254.169.254/latest/')
                    self.end_headers()
                    return
                if scenario=='invalid_json':
                    raw=b'{'
                elif scenario=='invalid_utf8':
                    raw=b'\xff'
                elif scenario=='duplicate_json':
                    raw=b'{"type":"message","type":"other"}'
                elif scenario=='oversize':
                    raw=b'x'*262145
                elif scenario=='encoded_credential_reflection':
                    encoded=''.join('\\u'+format(ord(char),'04x') for char in fixture.token).encode('ascii')
                    raw=raw.replace(fixture.token.encode('ascii'),encoded)
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('X-Atlas-P22-Fixture',fixture.provider)
                self.send_header('request-id','req_fixture_'+str(len(fixture.requests)))
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                if scenario=='hold':
                    for _ in range(200):
                        ready,_,_ = select.select([self.connection],[],[],.05)
                        if ready:
                            try:
                                if not self.connection.recv(1):
                                    fixture.closed.set()
                                    return
                            except OSError:
                                fixture.closed.set()
                                return
                    return
                if scenario=='disconnect':
                    self.wfile.write(raw[:10])
                    self.wfile.flush()
                    self.close_connection=True
                    return
                try:
                    for index in range(0,len(raw),37):
                        self.wfile.write(raw[index:index+37])
                except OSError:
                    fixture.closed.set()
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def answer(self,payload):
        outputs = [block for message in payload['messages'] for block in message['content'] if block['type']=='tool_result']
        if not outputs:
            block=dict(type='tool_use',id='tool_read_1',name='read_document',input={'document_id':'d1'})
        elif len(outputs)==1:
            document=json.loads(outputs[0]['content'])
            mode=json.loads(payload['messages'][0]['content'][0]['text'].split('\n',1)[1])['mode']
            report='# Controlled report\n\n模式：'+mode+'\n\n事实一：'+document['content']+'\n事实二：该资料仅属于当前合成项目。\n来源：d1 SHA-256 '+document['sha256']+'\n建议：继续核对授权边界。'
            block=dict(type='tool_use',id='tool_publish_1',name='publish_report',input={'content':report})
        else:
            block=dict(type='text',text='Controlled task complete.')
        if self.scenario=='denied_tool' and not outputs:
            block.update(name='bash',input={'command':'forbidden'})
        elif self.scenario=='extra_parameter' and not outputs:
            block['input']['path']='../outside'
        elif self.scenario=='early_publish' and not outputs:
            block.update(name='publish_report',input={'content':'premature'})
        result=dict(type='message',id='msg_fixture_'+str(len(outputs)),role='assistant',model=payload['model'],content=[block],
                    stop_reason='tool_use' if block['type']=='tool_use' else 'end_turn',usage={'input_tokens':12,'output_tokens':8})
        if self.scenario=='wrong_model':
            result['model']='unapproved-model'
        elif self.scenario=='truncated':
            result['stop_reason']='max_tokens'
        elif self.scenario=='missing_usage':
            result.pop('usage')
        elif self.scenario=='usage_overrun':
            result['usage']['output_tokens']=1025
        elif self.scenario=='usage_invalid':
            result['usage']['input_tokens']=True
        elif self.scenario=='parallel_tools' and block['type']=='tool_use':
            result['content'].append({**block,'id':'tool_other'})
        elif self.scenario in ('credential_reflection','encoded_credential_reflection'):
            result['content'].append(dict(type='text',text=self.token))
        return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
