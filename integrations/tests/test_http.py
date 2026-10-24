"""Exercise Atlas's actual HTTP handler and SDK against a controlled host."""
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_p1 import RuntimeTests
from atlas_bridge.host import REPO
# Load the exact shipped adapters. The legacy in-memory runtime is not involved
# in task routes, so use an inert fixture instead of an unversioned parent app.
legacy_runtime = ModuleType('runtime')
legacy_runtime.runtime = object()
server_spec = importlib.util.spec_from_file_location('atlas_bundled_server', REPO/'integrations/workbench/server.py')
sdk_spec = importlib.util.spec_from_file_location('atlas_bundled_sdk', REPO/'integrations/workbench/sdk.py')
server_module = importlib.util.module_from_spec(server_spec)
sdk_module = importlib.util.module_from_spec(sdk_spec)
with patch.dict(sys.modules, {'runtime':legacy_runtime}):
    server_spec.loader.exec_module(server_module)
sdk_spec.loader.exec_module(sdk_module)
Handler, ThreadingHTTPServer = server_module.Handler, server_module.ThreadingHTTPServer
AtlasClient = sdk_module.AtlasClient


class HttpTests(RuntimeTests):
    # Inherited runtime cases intentionally excluded by the test loader below.
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.server.task_host = self.host
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = AtlasClient(self.url, self.host.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def raw(self, body=None, auth=True, path='/api/v1/tasks', headers=None):
        request = urllib.request.Request(self.url+path, data=body, headers=headers or {})
        if auth:
            request.add_header('Authorization','Bearer '+self.host.token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_http_auth_and_parser(self):
        self.assertEqual(self.raw(auth=False)[0],401)
        self.assertEqual(self.raw(b'{"goal":1,"goal":2}')[1]['error'],'duplicate_json_field')
        self.assertEqual(self.raw(b'{"goal":NaN}')[0],400)
        self.assertEqual(self.raw(b'{broken')[0],400)
        self.assertEqual(self.raw(b'x'*16385)[0],413)
        # Send headers alone: the server rejects this transfer mode before body
        # parsing, so a client must not keep transmitting chunks after rejection.
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        connection.putrequest('POST','/api/v1/tasks')
        connection.putheader('Authorization','Bearer '+self.host.token)
        connection.putheader('Transfer-Encoding','chunked')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status,400)
        self.assertEqual(json.loads(response.read())['error'],'unsupported_transfer_encoding')
        connection.close()
        self.assertEqual(self.host.list(), [])

    def test_http_sdk_end_to_end(self):
        task = self.client.submit_task(**self.payload)
        task_id = task['task_id']
        self.wait(task_id)
        self.assertEqual(self.client.task(task_id)['state'],'completed')
        self.assertEqual(self.client.submit_task(**self.payload)['task_id'],task_id)
        self.assertEqual(len(self.client.task(task_id,'operations')),2)
        self.assertIn('immutable',self.client.task(task_id,'artifact')['content'])
        self.assertEqual(self.raw(auth=False,path=f'/api/v1/tasks/{task_id}/artifact')[0],401)
        self.assertEqual(self.raw(json.dumps({**self.payload,'goal':'changed'}).encode())[0],409)
        self.assertEqual(self.raw()[0],200)


if __name__ == '__main__':
    suite = unittest.TestSuite(HttpTests(name) for name in ('test_http_auth_and_parser','test_http_sdk_end_to_end'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
