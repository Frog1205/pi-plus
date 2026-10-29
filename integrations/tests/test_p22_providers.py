"""P2.2 provider fault and manual reconciliation acceptance. Never uses real API keys."""
from dataclasses import asdict,replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas_bridge.host import REPO,TaskError,canonical
from atlas_bridge.provider_host import ProviderHost
from atlas_bridge.provider_policy import ProviderGrant,ProviderProfile,ROUTES,public_address
from atlas_bridge.provider_protocol import parse_provider_response,normalized_response
from atlas_bridge.model_policy import TOOLS
from provider_server import ProviderServer


class ProviderTests(unittest.TestCase):
    provider='deepseek'
    def setUp(self):
        parent=REPO/'.artifacts/atlas-p22'
        parent.mkdir(parents=True,exist_ok=True)
        self.directory=Path(tempfile.mkdtemp(prefix='provider-',dir=parent))
        project=self.directory/'project'
        project.mkdir()
        self.raw='事实：Atlas 只读取授权快照。事实：报告发布经事务回执。'
        (project/'README.md').write_text(self.raw,encoding='utf-8')
        self.source_sha=hashlib.sha256(self.raw.encode('utf-8')).hexdigest()
        self.fixture=ProviderServer(self.provider)
        route=ROUTES[self.provider]
        self.profile=ProviderProfile('p22-'+self.provider,self.provider,'http://127.0.0.1:'+str(self.fixture.port)+'/anthropic/v1/messages','model_fixture',('README.md',),
                                     remote_model=route['model'],credential_ref=route['key_ref'],input_price=route['input_price'],output_price=route['output_price'],batch_id='test-'+self.provider)
        self.grant=ProviderGrant(self.provider,'model_fixture',self.source_sha,5000000000,self.fixture.token)
        self.config=dict(state_dir=str(self.directory/'state'),project_root=str(project),project_id='fixture',documents=['README.md'],timeout_seconds=60)
        self.host=ProviderHost(self.config,self.profile,self.grant)
        self.payload=dict(request_id='p22-task',project_id='fixture',goal='核对两条合成事实并给出建议。',documents=['README.md'])
        self.task_id=self.host.submit(self.payload)['task_id']

    def tearDown(self):
        target=os.environ.get('ATLAS_P22_EVIDENCE')
        if target:
            with open(target,'a',encoding='utf-8') as stream:
                stream.write(canonical(dict(test=self.id(),provider=self.provider,fixture_http_requests=len(self.fixture.requests),
                                           authorized=all(row['authorized'] for row in self.fixture.requests),task=self.host.get(self.task_id),
                                           ledger=self.host.broker.ledger.audit(self.task_id)))+'\n')
        self.host.close()
        self.fixture.close()

    def reopen(self):
        self.host.close()
        self.host=ProviderHost(self.config,self.profile,self.grant)

    def run_task(self,**options):
        result=self.host.run(self.task_id,**options)
        self.assertNotIn(self.fixture.token,self.host.stderr+canonical(self.host.trace)+canonical(self.host.events(self.task_id))+canonical(self.host.broker.ledger.audit(self.task_id)))
        for file in (self.host.state/'provider-workers').rglob('*'):
            if file.is_file():
                self.assertNotIn(self.fixture.token.encode(),file.read_bytes(),str(file))
        return result

    def request(self,generation='1'):
        payload=dict(model=self.profile.model,input=self.host.broker.expected_input(self.task_id),stream=True,store=False,max_output_tokens=1024,tools=TOOLS)
        return dict(protocol=2,type='model_request',id='direct-'+generation,generation_id=generation,body=canonical(payload))

    def no_effect(self):
        self.assertEqual(self.host.operations(self.task_id),[])
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM artifacts WHERE task_id=?',(self.task_id,)).fetchone()[0],0)

    def test_normal_actual_pi_adapter_with_provider_protocol(self):
        result=self.run_task()
        self.assertEqual(result['state'],'completed',(result,self.host.stderr,self.host.trace))
        self.assertEqual(result['provider'],self.provider)
        self.assertEqual(result['model'],self.profile.remote_model)
        self.assertFalse(result['model_connected'])
        self.assertEqual(result['worker_result']['native_fetch_calls'],0)
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(len(self.host.operations(self.task_id)),2)
        for row in self.fixture.requests:
            self.assertEqual(set(row['payload']),{'model','max_tokens','stream','thinking','messages','tools','tool_choice'})
            self.assertFalse(row['payload']['stream'])
            self.assertEqual(row['payload']['thinking'],{'type':'disabled'})
        self.assertIn(self.source_sha,self.host.artifact(self.task_id)['content'])

    def test_response_commit_crash_reuses_provider_receipt(self):
        self.assertEqual(self.run_task(after_model=lambda task,generation,job:job.kill())['state'],'interrupted')
        self.reopen()
        self.assertEqual(self.run_task()['state'],'completed')
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(self.host.broker.ledger.status(self.task_id)['requests'],3)

    def test_report_commit_crash_publishes_only_once(self):
        def kill(task,tool,job):
            if tool=='publish_report':
                job.kill()
        self.assertEqual(self.run_task(after_tool=kill)['state'],'interrupted')
        artifact=self.host.artifact(self.task_id)
        self.reopen()
        self.assertEqual(self.run_task()['state'],'completed')
        self.assertEqual(self.host.artifact(self.task_id),artifact)
        self.assertEqual(len(self.fixture.requests),3)

    def child(self,fault):
        self.host.close()
        child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('p22_crash_host.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        config=dict(host=self.config,profile=asdict(self.profile),grant=asdict(self.grant),task_id=self.task_id,fault=fault)
        child.stdin.write((canonical(config)+'\n').encode('utf-8'))
        child.stdin.flush()
        self.assertEqual(json.loads(child.stdout.readline())['task_id'],self.task_id)
        return child

    def test_host_crash_after_commit_does_not_repeat_model(self):
        child=self.child('after_commit')
        try:
            _,errors=child.communicate(timeout=60)
            self.assertEqual(child.returncode,95,errors)
            self.host=ProviderHost(self.config,self.profile,self.grant)
            self.assertEqual(self.run_task()['state'],'completed')
            self.assertEqual(len(self.fixture.requests),3)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_host_crash_inflight_stays_unknown_without_second_send(self):
        self.fixture.scenario='hold'
        child=self.child('inflight')
        try:
            self.assertTrue(self.fixture.arrived.wait(30))
            child.kill()
            child.communicate(timeout=5)
            for _ in range(2):
                self.host=ProviderHost(self.config,self.profile,self.grant)
                self.assertEqual(self.host.resume(self.task_id)['state'],'interrupted')
                self.assertEqual(len(self.fixture.requests),1)
                self.assertGreater(self.host.broker.ledger.status(self.task_id)['held_cost'],0)
                self.host.close()
            self.host=ProviderHost(self.config,self.profile,self.grant)
            self.no_effect()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_cancel_closes_provider_connection_without_releasing_unknown_cost(self):
        self.fixture.scenario='hold'
        result=[]
        thread=threading.Thread(target=lambda:result.append(self.run_task()))
        thread.start()
        try:
            self.assertTrue(self.fixture.arrived.wait(30))
            self.host.cancel(self.task_id)
            thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]['state'],'cancelled')
            self.assertTrue(result[0]['model_budget']['requires_reconciliation'])
            self.assertGreater(result[0]['model_budget']['held_cost'],0)
            self.assertEqual(len(self.fixture.requests),1)
            self.no_effect()
        finally:
            if thread.is_alive():
                self.host.job.kill()
                thread.join(10)

    def test_fault_matrix_never_executes_unvalidated_tools(self):
        for scenario in ('invalid_json','invalid_utf8','duplicate_json','oversize','wrong_model','truncated','usage_overrun','usage_invalid','parallel_tools','credential_reflection','encoded_credential_reflection','denied_tool','extra_parameter','early_publish'):
            with self.subTest(scenario=scenario):
                self.fixture.scenario=scenario
                self.task_id=self.host.submit({**self.payload,'request_id':scenario})['task_id']
                before=len(self.fixture.requests)
                result=self.run_task()
                self.assertEqual(result['state'],'failed',result)
                self.assertEqual(len(self.fixture.requests)-before,1)
                self.no_effect()

    def test_http_matrix_no_redirect_or_retry(self):
        for scenario in ('http_401','http_403','http_429','http_500','redirect','disconnect'):
            with self.subTest(scenario=scenario):
                self.fixture.scenario=scenario
                self.task_id=self.host.submit({**self.payload,'request_id':scenario})['task_id']
                before=len(self.fixture.requests)
                result=self.run_task()
                expected='interrupted' if scenario in ('http_429','http_500','disconnect') else 'failed'
                self.assertEqual(result['state'],expected,result)
                self.assertEqual(len(self.fixture.requests)-before,1)
                self.assertGreater(result['model_budget']['held_cost'],0)
                self.no_effect()

    def unknown(self):
        self.fixture.scenario='disconnect'
        self.assertEqual(self.run_task()['state'],'interrupted')
        row=self.host.db.execute('SELECT * FROM model_requests WHERE task_id=?',(self.task_id,)).fetchone()
        evidence=dict(task_id=self.task_id,generation_id=row['generation_id'],profile_hash=row['profile_hash'],request_hash=row['request_hash'],
                      provider_request_id=row['provider_request_id'],response=self.fixture.responses[-1],note='Trusted fixture captured complete provider response')
        return row,evidence

    def test_unknown_stays_blocked_across_two_restarts(self):
        row,_=self.unknown()
        held=row['held_cost']
        for _ in range(2):
            self.reopen()
            result=self.host.resume(self.task_id)
            self.assertEqual(result['state'],'interrupted')
            self.assertEqual(result['model_budget']['held_cost'],held)
            self.host._state(self.task_id,'running')
            with self.assertRaises(TaskError):
                self.host.broker.request(self.task_id,self.request('999'))
        self.assertEqual(len(self.fixture.requests),1)
        self.no_effect()

    def test_manual_reconciliation_recovers_without_resending(self):
        row,evidence=self.unknown()
        self.reopen()
        receipt=self.host.broker.reconcile(self.task_id,row['generation_id'],evidence)
        self.assertEqual(receipt,self.host.broker.reconcile(self.task_id,row['generation_id'],evidence))
        self.assertEqual(len(self.fixture.requests),1)
        self.fixture.scenario='normal'
        self.assertEqual(self.run_task()['state'],'completed')
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM provider_reconciliations').fetchone()[0],1)

    def test_reconciliation_rejects_mismatched_identity_and_content(self):
        row,evidence=self.unknown()
        for key,value in (('profile_hash','wrong'),('request_hash','wrong'),('task_id','wrong'),('generation_id','wrong'),('provider_request_id','wrong')):
            with self.subTest(key=key),self.assertRaises(TaskError):
                self.host.broker.reconcile(self.task_id,row['generation_id'],{**evidence,key:value})
        receipt=self.host.broker.reconcile(self.task_id,row['generation_id'],evidence)
        with self.assertRaisesRegex(TaskError,'reconciliation_content_conflict'):
            self.host.broker.reconcile(self.task_id,row['generation_id'],{**evidence,'note':'changed'})
        self.assertTrue(receipt['usage_available'])
        self.assertEqual(len(self.fixture.requests),1)

    def test_reconciliation_transaction_failure_preserves_unknown_reservation(self):
        row,evidence=self.unknown()
        def crash():
            raise RuntimeError('trusted transaction fault')
        with self.assertRaises(RuntimeError):
            self.host.broker.reconcile(self.task_id,row['generation_id'],evidence,fault=crash)
        current=self.host.db.execute('SELECT * FROM model_requests WHERE task_id=?',(self.task_id,)).fetchone()
        self.assertEqual(current['state'],'outcome_unknown')
        self.assertEqual(current['held_cost'],row['held_cost'])
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM provider_receipts').fetchone()[0],0)
        self.assertEqual(self.host.db.execute('SELECT COUNT(*) FROM provider_reconciliations').fetchone()[0],0)
        self.assertEqual(len(self.fixture.requests),1)

    def test_missing_usage_does_not_become_zero_cost(self):
        self.fixture.scenario='missing_usage'
        result=self.run_task()
        self.assertEqual(result['state'],'completed',(result,self.host.stderr))
        self.assertFalse(result['model_budget']['usage_available'])
        self.assertEqual(result['model_budget']['unknown_usage'],3)
        self.assertTrue(all(row[0]==row[1]>0 for row in self.host.db.execute('SELECT held_cost,reserved_cost FROM model_requests')))

    def test_unapproved_source_and_request_overrides_never_dispatch(self):
        self.host._state(self.task_id,'running')
        record=self.request()
        for key in ('url','headers','provider','mode'):
            with self.subTest(key=key),self.assertRaises(TaskError):
                self.host.broker.request(self.task_id,{**record,key:'forged'})
        self.host.broker.grant=replace(self.grant,source_sha256='0'*64)
        with self.assertRaisesRegex(TaskError,'provider_source_not_authorized'):
            self.host.broker.request(self.task_id,record)
        self.assertEqual(len(self.fixture.requests),0)
        self.no_effect()


class MiniMaxTests(ProviderTests):
    provider='minimax-cn'


class ProviderPolicyTests(unittest.TestCase):
    def test_private_fake_and_mapped_addresses_are_denied(self):
        for address in ('198.18.0.1','127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:8.8.8.8','2002:0a00:0001::1'):
            with self.subTest(address=address):
                self.assertFalse(public_address(address))
        self.assertTrue(public_address('1.1.1.1'))

    def test_live_profile_cannot_change_domains_or_budget(self):
        values=dict(profile_id='live-test',provider='deepseek',endpoint=ROUTES['deepseek']['endpoint'],mode='model_live',documents=('README.md',),remote_model='deepseek-flash',credential_ref='DEEPSEEK_API_KEY')
        for changes in ({'endpoint':'https://unapproved.invalid/anthropic/v1/messages'},{'max_requests':5},{'max_output_tokens':2048},{'batch_cost_limit':5000000001},{'remote_model':'other'}):
            with self.subTest(changes=changes),self.assertRaises(TaskError):
                ProviderProfile(**{**values,**changes})

    def test_public_dns_only_queries_fixed_resolver_and_rejects_poison(self):
        from atlas_bridge import provider_policy
        with patch.object(provider_policy,'DoHConnection') as connection:
            reply=connection.return_value.getresponse.return_value
            reply.status=200
            reply.read.return_value=b'{"Status":0,"Answer":[{"type":1,"data":"8.8.8.8"}]}'
            self.assertEqual(provider_policy.public_dns('api.deepseek.com'),('8.8.8.8',))
            connection.return_value.request.assert_called_once_with('GET','/dns-query?name=api.deepseek.com&type=A',headers={'Accept':'application/dns-json'})
            reply.read.return_value=b'{"Status":0,"Answer":[{"type":1,"data":"198.18.0.1"}]}'
            with self.assertRaises(TaskError):
                provider_policy.public_dns('api.deepseek.com')
            with self.assertRaises(TaskError):
                provider_policy.public_dns('unapproved.invalid')

    def test_provider_tls_connects_numeric_ip_but_validates_hostname(self):
        from atlas_bridge import provider_policy
        with patch.object(provider_policy,'public_dns',return_value=('8.8.8.8',)),patch.object(provider_policy.socket,'create_connection') as connect,patch.object(provider_policy.ssl,'create_default_context') as context:
            connection=provider_policy.ProviderConnection('api.deepseek.com',timeout=5)
            connection.connect()
            connect.assert_called_once_with(('8.8.8.8',443),timeout=5)
            context.return_value.wrap_socket.assert_called_once_with(connect.return_value,server_hostname='api.deepseek.com')

    def test_fixture_grant_never_falls_back_to_real_key(self):
        route=ROUTES['deepseek']
        profile=ProviderProfile('missing-fake-key','deepseek','http://127.0.0.1:12345/anthropic/v1/messages','model_fixture',('README.md',),remote_model=route['model'],credential_ref=route['key_ref'])
        with patch.dict(os.environ,DEEPSEEK_API_KEY='synthetic-real-key-must-not-be-used'):
            with self.assertRaisesRegex(TaskError,'provider_credential_unavailable'):
                ProviderGrant('deepseek','model_fixture','0'*64,5000000000).secret(profile)


if __name__=='__main__':
    unittest.main(verbosity=2)
