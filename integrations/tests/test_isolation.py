import ctypes as ct
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas_bridge.host import REPO, verify_engine, TaskError
from atlas_bridge.windows import JobProcess, close_handle, wait_process, bind

PROBE = Path(__file__).with_name('isolation_probe.mjs')
open_process = bind('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        (REPO/'.artifacts/atlas-p1').mkdir(parents=True,exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='isolation-', dir=REPO/'.artifacts/atlas-p1'))
        self.environment = {name:os.environ[name] for name in ('SystemRoot','WINDIR') if name in os.environ}
        self.node = shutil.which('node')

    def probe(self, permissions):
        secret = self.directory / 'outside.txt'
        secret.write_text('unchanged', encoding='utf-8')
        args = [self.node]
        if permissions:
            args += ['--permission', '--allow-fs-read='+str(PROBE)]
        job = JobProcess(args + [str(PROBE), 'probe', str(secret)], self.directory, self.environment)
        try:
            result = json.loads(job.stdout.readline())
            self.assertEqual(result['spawn'] == 'allowed', False, result)
            if permissions:
                self.assertEqual(result['read'], 'ERR_ACCESS_DENIED')
                self.assertEqual(result['write'], 'ERR_ACCESS_DENIED')
                self.assertEqual(result['spawn'], 'ERR_ACCESS_DENIED')
                self.assertEqual(secret.read_text('utf-8'), 'unchanged')
            return result
        finally:
            job.close()

    def test_node_permission_denies_files_and_child_process(self):
        self.probe(True)

    def test_job_denies_child_even_without_node_permission(self):
        self.probe(False)

    def test_parent_force_kill_also_kills_worker(self):
        parent = subprocess.Popen([sys.executable, str(PROBE.with_name('job_parent.py')), str(self.directory)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**self.environment, 'PATH':os.environ['PATH']})
        handle = None
        try:
            worker = json.loads(parent.stdout.readline())['pid']
            handle = open_process(0x100000, False, worker)
            self.assertTrue(handle)
            self.assertEqual(wait_process(handle, 0), 0x102)
            parent.kill()
            parent.wait(5)
            self.assertEqual(wait_process(handle, 5000), 0)
        finally:
            if handle:
                close_handle(handle)
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)

    def test_engine_pin_detects_modified_source(self):
        # Use a mocked read only for one byte stream; never modify reviewed source.
        from unittest.mock import patch
        original = Path.read_bytes
        target = REPO / 'packages/atlas-runtime/src/worker.ts'
        with patch.object(Path, 'read_bytes', lambda path: b'tampered' if path==target else original(path)):
            with self.assertRaisesRegex(TaskError, 'engine_pin_mismatch'):
                verify_engine()


if __name__ == '__main__':
    unittest.main(verbosity=2)
