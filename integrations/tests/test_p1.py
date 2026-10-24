"""Real Windows process + pi-durable integration tests; no provider credentials."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas_bridge.host import AtlasTaskHost, TaskError, REPO, digest
from atlas_bridge.windows import JobProcess, read_project_file


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        evidence = REPO / '.artifacts/atlas-p1'
        evidence.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='test-', dir=evidence))
        self.project = self.directory / 'project'
        self.project.mkdir()
        (self.project / 'README.md').write_text('Atlas fixture: immutable authorized input.', encoding='utf-8')
        self.config = dict(state_dir=str(self.directory / 'state'), project_root=str(self.project),
                           principal='fixture-owner', project_id='fixture', documents=['README.md'])
        self.host = AtlasTaskHost(self.config)
        self.payload = dict(request_id='r1', project_id='fixture', goal='Verify the authorized document', documents=['README.md'])

    def tearDown(self):
        self.host.close()

    def wait(self, task_id, expected='completed'):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            task = self.host.get(task_id)
            if task['state'] in ('completed','interrupted','failed','cancelled') and task_id not in self.host.active:
                self.assertEqual(task['state'], expected, (task, self.host.diagnostics.get(task_id)))
                return task
            time.sleep(.05)
        self.fail(('timeout', self.host.get(task_id), self.host.diagnostics))

    def test_real_task_and_persistent_receipts(self):
        task = self.wait(self.host.submit(self.payload)['task_id'])
        self.assertFalse(task['model_connected'])
        self.assertEqual(task['engine_version'], '1.4.0')
        self.assertIn('immutable authorized input', self.host.artifact(task['task_id'])['content'])
        self.assertEqual(len(self.host.operations(task['task_id'])), 2)
        events = self.host.events(task['task_id'])
        self.assertEqual([event['sequence'] for event in events], list(range(len(events))))
        self.assertEqual(len({event['event_id'] for event in events}), len(events))
        self.host.close()
        self.host = AtlasTaskHost(self.config)
        self.assertEqual(self.host.submit(self.payload)['task_id'], task['task_id'])
        self.assertEqual(self.host.get(task['task_id'])['state'], 'completed')

    def test_concurrent_dedup_and_content_conflict(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda _: self.host.submit(self.payload)['task_id'], range(16)))
        self.assertEqual(len(set(ids)), 1)
        self.wait(ids[0])
        with self.assertRaisesRegex(TaskError, 'request_content_conflict'):
            self.host.submit({**self.payload, 'goal':'different body'})
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 1)

    def test_strict_admission(self):
        self.assertFalse(self.host.authenticate('Bearer é'))
        self.assertFalse(self.host.authenticate('Bearer '))
        self.assertFalse(self.host.authenticate(None))
        invalid = [({**self.payload, 'actor':'admin'}, 'invalid_request_fields'),
                   ({**self.payload, 'project_id':'other'}, 'project_not_granted'),
                   ({**self.payload, 'documents':['../secret']}, 'invalid_document'),
                   ({**self.payload, 'documents':['D:/secret']}, 'invalid_document'),
                   ({**self.payload, 'documents':['README.md','README.md']}, 'document_not_granted'),
                   ({**self.payload, 'documents':['unknown']}, 'document_not_granted'),
                   ({**self.payload, 'goal':''}, 'invalid_goal'),
                   ({**self.payload, 'request_id':42}, 'invalid_request_id')]
        for request, code in invalid:
            with self.subTest(code=code), self.assertRaisesRegex(TaskError, code):
                self.host.submit(request)
        self.assertEqual(self.host.list(), [])

    def test_empty_token_fails_closed(self):
        original = self.host.token
        self.host.close()
        token_file = Path(self.config['state_dir'])/'auth.token'
        token_file.write_text('',encoding='utf-8')
        with self.assertRaisesRegex(TaskError,'invalid_state_token'):
            AtlasTaskHost(self.config)
        token_file.write_text(original,encoding='utf-8')
        self.host = AtlasTaskHost(self.config)

    def test_gateway_closed_tools_parameters_and_order(self):
        self.host._schedule = lambda _: None
        task_id = self.host.submit(self.payload)['task_id']
        self.host._state(task_id, 'running')
        for tool, params, code in [('bash', {'command':'whoami'},'tool_not_granted'),
                                  ('read_document', {'document_id':'d1','path':'../secret'},'invalid_tool_parameters'),
                                  ('read_document', {'document_id':'d2'},'document_not_granted'),
                                  ('publish_report', {'content':'report'},'sources_not_read')]:
            with self.subTest(tool=tool), self.assertRaisesRegex(TaskError, code):
                self.host.gateway(task_id, tool, params)
        (self.project / 'README.md').write_text('Changed after admission', encoding='utf-8')
        first = self.host.gateway(task_id, 'read_document', {'document_id':'d1'})
        self.assertIn('immutable', first['content'])
        receipt = self.host.gateway(task_id, 'publish_report', {'content':'report'})
        self.assertEqual(receipt, self.host.gateway(task_id, 'publish_report', {'content':'report'}))
        with self.assertRaisesRegex(TaskError, 'operation_content_conflict'):
            self.host.gateway(task_id, 'publish_report', {'content':'changed'})
        self.host._state(task_id, 'completed')

    def test_crash_after_publish_commit_then_replay(self):
        killed = []
        def crash(task_id, tool, job):
            if tool == 'publish_report' and not killed:
                killed.append(job.pid)
                job.kill()
        self.host.after_operation = crash
        task_id = self.host.submit(self.payload)['task_id']
        self.wait(task_id, 'interrupted')
        artifact = self.host.artifact(task_id)
        self.host.after_operation = None
        self.host.resume(task_id)
        self.wait(task_id)
        self.assertEqual(artifact, self.host.artifact(task_id))
        self.assertEqual(len(self.host.operations(task_id)), 2)
        published = [e for e in self.host.events(task_id) if e['type']=='operation_completed' and e['detail']['tool']=='publish_report']
        self.assertEqual(len(published), 1)

    def test_restart_recovers_committed_publish(self):
        def crash(task_id, tool, job):
            if tool == 'publish_report':
                job.kill()
        self.host.after_operation = crash
        task_id = self.host.submit(self.payload)['task_id']
        self.wait(task_id, 'interrupted')
        artifact = self.host.artifact(task_id)
        self.host.close()
        self.host = AtlasTaskHost(self.config)
        self.wait(task_id)
        self.assertEqual(artifact, self.host.artifact(task_id))

    def test_hard_host_crash_after_commit(self):
        self.host.close()
        config_path = self.directory / 'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('host_crash.py')), str(config_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = child.communicate(timeout=35)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 93, stderr)
        record = json.loads(stdout)
        self.host = AtlasTaskHost(self.config)
        self.wait(record['task_id'])
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0], 1)
        published = [e for e in self.host.events(record['task_id']) if e['type']=='operation_completed' and e['detail']['tool']=='publish_report']
        self.assertEqual(len(published), 1)

    def test_cancel_and_owner_lock(self):
        with self.assertRaisesRegex(TaskError, 'state_already_owned'):
            AtlasTaskHost(self.config)
        entered, release = threading.Event(), threading.Event()
        def pause(task_id, tool, job):
            entered.set()
            release.wait(5)
        self.host.after_operation = pause
        task_id = self.host.submit(self.payload)['task_id']
        # Synchronize with the committed read, within the Host's execution
        # budget. Cold Windows startup must not consume a shorter test-only SLA.
        self.assertTrue(entered.wait(self.host.timeout + 5),
                        (self.host.get(task_id), self.host.diagnostics.get(task_id)))
        self.host.cancel(task_id)
        release.set()
        self.wait(task_id, 'cancelled')
        with self.assertRaisesRegex(TaskError, 'artifact_not_found'):
            self.host.artifact(task_id)

    def test_uncertain_intent_is_not_reexecuted(self):
        self.host._schedule = lambda _: None
        task_id = self.host.submit(self.payload)['task_id']
        self.host._state(task_id, 'running')
        params = {'document_id':'d1'}
        self.host.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',
                             (task_id,'read:d1','read_document',digest({'tool':'read_document','parameters':params}),'intent',None))
        with self.assertRaisesRegex(TaskError, 'operation_requires_reconciliation'):
            self.host.gateway(task_id, 'read_document', params)
        self.assertEqual(len(self.host.operations(task_id)), 1)
        self.host._state(task_id, 'failed')

    def test_failed_receipt_transaction_rolls_back_artifact(self):
        import sqlite3
        self.host._schedule = lambda _: None
        task_id = self.host.submit(self.payload)['task_id']
        self.host._state(task_id,'running')
        self.host.gateway(task_id,'read_document',{'document_id':'d1'})
        self.host.db.execute("CREATE TRIGGER receipt_fault BEFORE UPDATE ON operations WHEN NEW.operation_id='publish:report' BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
        with self.assertRaises(sqlite3.Error):
            self.host.gateway(task_id,'publish_report',{'content':'atomic report'})
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],0)
        self.assertEqual(len(self.host.operations(task_id)),1)
        self.host.db.execute('DROP TRIGGER receipt_fault')
        self.host.gateway(task_id,'publish_report',{'content':'atomic report'})
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],1)
        self.host._state(task_id,'completed')

    def test_junction_escape_and_size_limits(self):
        outside = self.directory / 'outside'
        outside.mkdir()
        (outside / 'secret.md').write_text('OUTSIDE', encoding='utf-8')
        subprocess.run(['cmd','/c','mklink','/J',str(self.project/'link'),str(outside)], check=True, capture_output=True)
        with self.assertRaises(PermissionError):
            read_project_file(self.project, 'link/secret.md')
        (self.project / 'big.md').write_bytes(b'x'*65537)
        with self.assertRaises(ValueError):
            read_project_file(self.project, 'big.md')

    def test_junction_swapped_after_attestation_reads_original_handle(self):
        from unittest.mock import patch
        from atlas_bridge import windows
        inside = self.project / 'inside'
        outside = self.directory / 'outside'
        inside.mkdir()
        outside.mkdir()
        for folder, content in ((inside,'AUTHORIZED'),(outside,'OUTSIDE')):
            (folder/'data.md').write_text(content, encoding='utf-8')
        link = self.project/'switch'
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(inside)],check=True,capture_output=True)
        original, switched = windows.handle_path, []
        def swap(handle):
            value = original(handle)
            if value.endswith('data.md'):
                link.rename(self.project/'old-switch')
                subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
                switched.append(True)
            return value
        with patch.object(windows, 'handle_path', swap):
            self.assertEqual(read_project_file(self.project, 'switch/data.md'),b'AUTHORIZED')
        self.assertEqual(switched,[True])
        self.assertEqual((link/'data.md').read_text('utf-8'),'OUTSIDE')

    def test_changed_policy_blocks_recovery(self):
        def crash(task_id, tool, job):
            if tool == 'read_document':
                job.kill()
        self.host.after_operation = crash
        task_id = self.host.submit(self.payload)['task_id']
        self.wait(task_id, 'interrupted')
        self.host.close()
        self.host = AtlasTaskHost({**self.config,'documents':['README.md','other.md']})
        self.wait(task_id,'failed')
        self.assertEqual(self.host.get(task_id)['error'],'policy_changed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
