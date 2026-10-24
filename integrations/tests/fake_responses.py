"""Deterministic loopback Responses server. It never contacts a model provider."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import threading
import uuid


def sse_response(body, scenario):
    outputs = [item for item in body['input'] if item.get('type')=='function_call_output']
    identity = 'resp_fixture_'+str(len(outputs))
    if scenario=='denied_tool':
        item = dict(type='function_call',id='fc_denied',call_id='denied',name='bash',arguments='{"command":"forbidden"}',status='completed') if not outputs else dict(type='message',id='msg_done',role='assistant',status='completed',content=[dict(type='output_text',text='Fixture stopped.',annotations=[])])
    elif scenario in ('bad_arguments','early_publish') and not outputs:
        item = dict(type='function_call',id='fc_invalid',call_id='invalid',name='read_document' if scenario=='bad_arguments' else 'publish_report',arguments=json.dumps({'document_id':'d1','path':'../outside'} if scenario=='bad_arguments' else {'content':'premature'}),status='completed')
    elif not outputs:
        item = dict(type='function_call',id='fc_read',call_id='read_d1',name='read_document',arguments='{"document_id":"d1"}',status='completed')
    elif len(outputs)==1 and scenario not in ('bad_arguments','early_publish'):
        document = json.loads(outputs[0]['output'])
        content = '# Fixture report\n\n模式：model_fixture（无真实模型推理）\n\n'+document['content']+'\nSHA-256: '+document['sha256']
        item = dict(type='function_call',id='fc_publish',call_id='publish',name='publish_report',arguments=json.dumps({'content':content},ensure_ascii=False),status='completed')
    else:
        item = dict(type='message',id='msg_done',role='assistant',status='completed',content=[dict(type='output_text',text='Fixture workflow complete.',annotations=[])])
    events = [dict(type='response.created',response=dict(id=identity,model='atlas-fixture-model',status='in_progress')),
              dict(type='response.output_item.added',output_index=0,item={**item,**({'arguments':''} if item['type']=='function_call' else {'content':[]})})]
    if item['type']=='function_call':
        arguments = item['arguments']
        for index in range(0,len(arguments),7):
            events.append(dict(type='response.function_call_arguments.delta',item_id=item['id'],output_index=0,delta=arguments[index:index+7]))
        events.append(dict(type='response.function_call_arguments.done',item_id=item['id'],output_index=0,arguments=arguments))
    else:
        events.append(dict(type='response.output_text.delta',output_index=0,content_index=0,delta=item['content'][0]['text']))
    events.append(dict(type='response.output_item.done',output_index=0,item=item))
    response = dict(id=identity,model='atlas-fixture-model',status='completed',output=[item],usage=None if scenario=='missing_usage' else dict(input_tokens=12,output_tokens=8,total_tokens=20,input_tokens_details={'cached_tokens':0},output_tokens_details={'reasoning_tokens':0}))
    events.append(dict(type='response.completed',response=response))
    if scenario=='framed':
        events[0]['response']['metadata']={'padding':'中'*6000}
    elif scenario=='partial':
        events = events[:3]
    elif scenario=='unfinished_item':
        events = [event for event in events if event['type']!='response.output_item.done']
    elif scenario=='invalid_json':
        item['arguments']='{"document_id":'
    elif scenario=='duplicate_json':
        item['arguments']='{"document_id":"d1","document_id":"other"}'
    elif scenario=='wrong_model':
        response['model']='unapproved-model'
    elif scenario=='truncated':
        events[-1]['type']='response.incomplete'
        response['status']='incomplete'
    elif scenario=='post_terminal':
        events.append(dict(type='response.output_text.delta',output_index=0,delta='late'))
    data = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n\n' for event in events).encode('utf-8')
    if scenario=='invalid_utf8':
        data+=b'\xff'
    elif scenario=='oversize':
        data+=b'x'*262145
    return data


class FakeProvider:
    def __init__(self, scenario='normal'):
        self.scenario = scenario
        self.token = 'fixture-host-'+uuid.uuid4().hex
        self.requests = []
        self.arrived = threading.Event()
        self.closed = threading.Event()
        self.transport_resets = 0
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError,ConnectionAbortedError,BrokenPipeError):
                    fixture.transport_resets += 1
                    fixture.closed.set()
            def log_message(self,*_):
                pass
            def do_POST(self):
                self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                raw = self.rfile.read(int(self.headers['Content-Length']))
                fixture.requests.append(dict(path=self.path,authorized=self.headers.get('Authorization')=='Bearer '+fixture.token,body=json.loads(raw)))
                fixture.arrived.set()
                if fixture.scenario.startswith('http_'):
                    status = int(fixture.scenario[5:])
                    data = b'{"error":{"message":"fixture failure"}}'
                    self.send_response(status)
                    self.send_header('Content-Length',str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('X-Atlas-Fixture','responses-probe-v2')
                if fixture.scenario=='hold':
                    self.send_header('Transfer-Encoding','chunked')
                    self.end_headers()
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
                data = sse_response(json.loads(raw),fixture.scenario)
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                # Split inside UTF-8 and JSON boundaries, independently of SSE events.
                try:
                    for index in range(0,len(data),13):
                        self.wfile.write(data[index:index+13])
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,OSError):
                    fixture.closed.set()
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
