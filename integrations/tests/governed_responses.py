"""Additional deterministic governance errors; no real network targets."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

from fake_responses import sse_response


class GovernanceProvider:
    def __init__(self,scenario):
        self.token = 'fixture-host-'+uuid.uuid4().hex
        self.requests, self.scenario = [], scenario
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append(dict(body=body,authorized=self.headers.get('Authorization')=='Bearer '+fixture.token))
                if fixture.scenario == 'redirect':
                    self.send_response(307)
                    self.send_header('Location','http://169.254.169.254/latest/meta-data/')
                    self.send_header('Content-Length','0')
                    self.end_headers()
                    return
                events = [json.loads(line[6:]) for line in sse_response(body,'normal').decode('utf-8').splitlines() if line.startswith('data: ')]
                if fixture.scenario == 'usage_overrun':
                    events[-1]['response']['usage']['output_tokens']=1025
                elif fixture.scenario == 'usage_invalid':
                    events[-1]['response']['usage']['input_tokens']=True
                elif fixture.scenario == 'delta_mismatch':
                    next(item for item in events if item['type']=='response.function_call_arguments.delta')['delta']='forged'
                elif fixture.scenario == 'credential_reflection':
                    events[0]['response']['metadata']={'credential':fixture.token}
                data = ''.join('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                if fixture.scenario != 'missing_marker':
                    self.send_header('X-Atlas-Fixture','responses-probe-v2')
                self.send_header('x-request-id','fixture-request-1')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
