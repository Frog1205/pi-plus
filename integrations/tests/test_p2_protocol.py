"""P2.0 tests use the real HTTP adapter + durable + Windows Job, no real model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas_bridge.model_probe import ProbeHost,FixtureProfile,TaskError,REPO
from atlas_bridge.host import AtlasTaskHost,canonical
from fake_responses import FakeProvider


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        parent = REPO/'.artifacts/atlas-p2'
        parent.mkdir(parents=True,exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='protocol-',dir=parent))
        project = self.root/'project'
        project.mkdir()
        (project/'README.md').write_text('授权的合成项目资料，禁止读取其他目录。',encoding='utf-8')
        self.config = dict(state_dir=str(self.root/'state'),project_root=str(project),project_id='fixture',documents=['README.md'])
        self.host = ProbeHost(self.config)
        self.fixture = FakeProvider()
        self.host.attach_fixture(FixtureProfile(self.fixture.port,self.fixture.token))
        self.task_id = self.host.submit(dict(request_id='probe-1',project_id='fixture',goal='Exercise fixture model protocol',documents=['README.md']))['task_id']

    def tearDown(self):
        evidence = os.environ.get('ATLAS_P2_EVIDENCE')
        if evidence:
            record = dict(test=self.id(),scenario=self.fixture.scenario,fixture_http_requests=len(self.fixture.requests),
                          authorized_requests=all(item['authorized'] for item in self.fixture.requests),mode='model_fixture',model_connected=False,
                          tasks=[self.host.probe_status(item['task_id']) for item in self.host.list()],
                          models=[dict(row) for row in self.host.db.execute('SELECT task_id,generation_id,hash,state,usage FROM probe_models')])
            with open(evidence,'a',encoding='utf-8') as stream:
                stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            if self._testMethodName=='test_http_adapter_tools_and_generation_memos' and self.host.get(self.task_id)['state']=='completed':
                Path(evidence).with_name('fixture-report.md').write_text(self.host.artifact(self.task_id)['content'],encoding='utf-8')
        self.host.close()
        self.fixture.close()

    def run_probe(self,**kwargs):
        result = self.host.run_probe(self.task_id,**kwargs)
        self.assertNotIn(self.fixture.token,self.host.stderr+json.dumps(self.host.trace))
        for file in (self.host.state/'probe-workers').rglob('*'):
            if file.is_file():
                self.assertNotIn(self.fixture.token.encode(),file.read_bytes(),str(file))
        return result

    def assert_no_effect(self):
        self.assertEqual(self.host.operations(self.task_id),[])
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],0)

    def test_http_adapter_tools_and_generation_memos(self):
        result = self.run_probe()
        self.assertEqual(result['state'],'completed',(result,self.host.stderr,self.host.trace))
        self.assertEqual(result['mode'],'model_fixture')
        self.assertFalse(result['model_connected'])
        self.assertEqual(result['worker_result']['native_fetch_calls'],0)
        self.assertEqual(result['worker_result']['fetch_calls'],3)
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(len(self.host.operations(self.task_id)),2)
        self.assertIn('授权的合成项目',self.host.artifact(self.task_id)['content'])
        generations = [item for item in self.host.trace if item['type']=='generation']
        self.assertEqual(len(generations),3)
        self.assertTrue(all(item['generation_id']==str(item['task_id']) for item in generations))
        self.assertTrue(all(item['authorized'] and item['path']=='/v1/responses' for item in self.fixture.requests))
        self.assertNotIn(self.fixture.token,json.dumps(self.fixture.requests))

    def test_committed_model_response_replayed_after_worker_kill(self):
        killed = []
        def crash(task_id,generation_id,job):
            if not killed:
                killed.append(generation_id)
                job.kill()
        self.assertEqual(self.run_probe(after_model=crash)['state'],'interrupted')
        self.assertEqual(len(self.fixture.requests),1)
        self.assert_no_effect()
        result = self.run_probe()
        self.assertEqual(result['state'],'completed',(result,self.host.stderr))
        self.assertEqual(len(self.fixture.requests),3)
        events = self.host.events(self.task_id)
        self.assertEqual(len([event for event in events if event['type']=='model_cache_replayed']),1)
        self.assertEqual(len(self.host.operations(self.task_id)),2)

    def test_report_commit_replayed_once(self):
        killed = []
        def crash(task_id,tool,job):
            if tool=='publish_report' and not killed:
                killed.append(True)
                job.kill()
        self.assertEqual(self.run_probe(after_tool=crash)['state'],'interrupted')
        artifact = self.host.artifact(self.task_id)
        self.assertEqual(self.run_probe()['state'],'completed')
        self.assertEqual(artifact,self.host.artifact(self.task_id))
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],1)

    def test_invalid_or_incomplete_responses_never_execute_tools(self):
        for scenario in ('partial','unfinished_item','invalid_json','duplicate_json','invalid_utf8','truncated','wrong_model','post_terminal','oversize'):
            with self.subTest(scenario=scenario):
                self.fixture.scenario = scenario
                self.task_id = self.host.submit(dict(request_id=scenario,project_id='fixture',goal='Reject invalid model response',documents=['README.md']))['task_id']
                before = len(self.fixture.requests)
                result = self.run_probe()
                self.assertNotEqual(result['state'],'completed',(result,self.host.stderr))
                self.assertEqual(len(self.fixture.requests)-before,1)
                self.assertEqual(self.host.operations(self.task_id),[])
                with self.assertRaises(TaskError):
                    self.host.artifact(self.task_id)

    def test_http_errors_do_not_retry_implicitly(self):
        for status in (401,403,429,500):
            with self.subTest(status=status):
                self.fixture.scenario='http_'+str(status)
                self.task_id = self.host.submit(dict(request_id='http-'+str(status),project_id='fixture',goal='Reject HTTP failure',documents=['README.md']))['task_id']
                before = len(self.fixture.requests)
                self.assertNotEqual(self.run_probe()['state'],'completed')
                self.assertEqual(len(self.fixture.requests)-before,1)
                self.assertEqual(self.host.operations(self.task_id),[])

    def test_tool_names_parameters_and_order_stay_restricted(self):
        for scenario in ('denied_tool','bad_arguments','early_publish'):
            with self.subTest(scenario=scenario):
                self.fixture.scenario=scenario
                self.task_id = self.host.submit(dict(request_id=scenario,project_id='fixture',goal='Ignore injected tool escalation',documents=['README.md']))['task_id']
                result = self.run_probe()
                self.assertNotEqual(result['state'],'completed')
                self.assertEqual(self.host.operations(self.task_id),[])
                with self.assertRaises(TaskError):
                    self.host.artifact(self.task_id)

    def test_abort_signal_closes_host_request(self):
        self.fixture.scenario='hold'
        results = []
        thread = threading.Thread(target=lambda:results.append(self.run_probe()))
        thread.start()
        try:
            self.assertTrue(self.fixture.arrived.wait(20))
            self.host.probe_cancel.set()
            thread.join(12)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['state'],'cancelled')
            self.assertTrue(self.fixture.closed.wait(5))
            self.assertTrue(any(event['type']=='model_abort_received' for event in self.host.events(self.task_id)))
            self.assertIsNone(self.host.job)
            self.assertEqual(len(self.fixture.requests),1)
            self.assert_no_effect()
        finally:
            if thread.is_alive():
                self.host.job.kill()
                thread.join(10)

    def test_corrupt_ipc_frames_fail_closed(self):
        for failure in ('sequence','checksum','frame_limit'):
            with self.subTest(failure=failure):
                self.task_id = self.host.submit(dict(request_id=failure,project_id='fixture',goal='Reject corrupt frames',documents=['README.md']))['task_id']
                def corrupt(frames):
                    if failure=='sequence':
                        frames[1]['sequence']=7
                    elif failure=='checksum':
                        frames[-1]['sha256']='incorrect'
                    else:
                        frames[1]['data']='A'*65540
                    return frames
                self.assertNotEqual(self.run_probe(transform=corrupt)['state'],'completed')
                self.assertEqual(self.host.operations(self.task_id),[])

    def test_missing_usage_is_preserved_as_unknown(self):
        self.fixture.scenario='missing_usage'
        result = self.run_probe()
        self.assertEqual(result['state'],'completed',(result,self.host.stderr))
        usages = [json.loads(row[0]) for row in self.host.db.execute('SELECT usage FROM probe_models')]
        self.assertEqual(usages,[None,None,None])

    def test_multiple_frames_reassemble_utf8_response(self):
        self.fixture.scenario='framed'
        chunks = []
        def observe(frames):
            chunks.append(len([frame for frame in frames if frame['type']=='model_chunk']))
            return frames
        result = self.run_probe(transform=observe)
        self.assertEqual(result['state'],'completed',(result,self.host.stderr))
        self.assertEqual(len(chunks),3)
        self.assertTrue(all(count>1 for count in chunks))
        self.assertIn('授权的合成项目',self.host.artifact(self.task_id)['content'])

    def test_profile_and_content_changes_cannot_reuse_cached_request(self):
        identities = []
        def pause(task_id,generation_id,job):
            identities.append(generation_id)
            job.kill()
        self.run_probe(after_model=pause)
        self.host._state(self.task_id,'running')
        payload = self.fixture.requests[0]['body']
        request = dict(protocol=2,type='model_request',id='direct-cache-test',generation_id=identities[0],body=canonical(payload))
        self.host.broker.request(self.task_id,request)
        for record,code in [({**request,'url':'https://unapproved.invalid'},'model_request_invalid'),
                            ({**request,'headers':{'Authorization':'forged'}},'model_request_invalid'),
                            ({**request,'body':canonical({**payload,'model':'other'})},'model_profile_denied'),
                            ({**request,'body':canonical({**payload,'input':[]})},'model_request_content_conflict')]:
            with self.subTest(code=code),self.assertRaisesRegex(TaskError,code):
                self.host.broker.request(self.task_id,record)
        self.assertEqual(len(self.fixture.requests),1)
        self.assert_no_effect()
        self.host._state(self.task_id,'failed')

    def test_model_attempt_limit_survives_host_reopen(self):
        self.host._state(self.task_id,'running')
        payload = dict(model='atlas-fixture-model',input=[],stream=True,store=False,max_output_tokens=1024,
                       tools=[{'type':'function','name':'read_document'},{'type':'function','name':'publish_report'}])
        request = dict(protocol=2,type='model_request',id='budget-check',generation_id='0',body=canonical(payload))
        for index in range(4):
            self.host.broker.request(self.task_id,{**request,'generation_id':str(index)})
        self.host.close()
        self.reopen()
        self.host._state(self.task_id,'running')
        with self.assertRaisesRegex(TaskError,'model_attempt_budget'):
            self.host.broker.request(self.task_id,{**request,'generation_id':'5'})
        self.assertEqual(len(self.fixture.requests),4)
        self.assert_no_effect()
        self.host._state(self.task_id,'failed')

    def test_probe_cannot_be_enabled_through_production_inputs(self):
        with self.assertRaisesRegex(TaskError,'probe_fixture_project_required'):
            ProbeHost({**self.config,'project_id':'atlas-brain'})
        for field in ('mode','fixture_profile','provider'):
            with self.subTest(field=field),self.assertRaisesRegex(TaskError,'unknown_configuration'):
                AtlasTaskHost({**self.config,field:'model_fixture'})
        with self.assertRaisesRegex(TaskError,'invalid_request_fields'):
            self.host.submit(dict(request_id='no-public-probe',project_id='fixture',goal='fixture',documents=['README.md'],mode='model_fixture'))

    def child(self,fault):
        self.host.close()
        process = subprocess.Popen([sys.executable,str(Path(__file__).with_name('p2_crash_host.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        configuration = dict(host=self.config,port=self.fixture.port,fixture_token=self.fixture.token,fault=fault)
        process.stdin.write((json.dumps(configuration)+'\n').encode())
        process.stdin.flush()
        self.task_id = json.loads(process.stdout.readline())['task_id']
        return process

    def reopen(self):
        self.host = ProbeHost(self.config)
        self.host.attach_fixture(FixtureProfile(self.fixture.port,self.fixture.token))

    def test_host_crash_after_response_commit_reuses_cache(self):
        process = self.child('after_model_commit')
        try:
            _,stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode,93,stderr)
            self.reopen()
            self.assertEqual(self.run_probe()['state'],'completed')
            self.assertEqual(len(self.fixture.requests),3)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_host_crash_inflight_blocks_two_restarts(self):
        self.fixture.scenario='hold'
        process = self.child('inflight')
        try:
            self.assertTrue(self.fixture.arrived.wait(20))
            process.kill()
            process.communicate(timeout=5)
            for _ in range(2):
                self.reopen()
                result = self.run_probe()
                self.assertEqual(result['state'],'interrupted')
                self.assertEqual(result['error'],'model_outcome_unknown')
                self.assertEqual(len(self.fixture.requests),1)
                self.host.close()
            self.reopen()
            self.assert_no_effect()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


if __name__=='__main__':
    unittest.main(verbosity=2)
