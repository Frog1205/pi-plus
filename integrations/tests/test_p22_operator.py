"""Operator CLI acceptance; model dispatch is forbidden throughout these tests."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_p22_providers as fixtures
from atlas_bridge.host import REPO,canonical
from atlas_bridge.provider_host import ProviderHost
from atlas_bridge.provider_policy import ROUTES


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.ProviderTests('test_normal_actual_pi_adapter_with_provider_protocol')
        self.case.setUp()
        self.case.host._state(self.case.task_id,'running')
        request=self.case.request()
        response=self.case.fixture.answer(self.case.host.broker.remote_payload(self.case.task_id,json.loads(request['body'])))
        def stop_before_network():
            raise RuntimeError('trusted pre-network fault')
        with self.assertRaises(RuntimeError):
            self.case.host.broker.request(self.case.task_id,request,dispatched=stop_before_network)
        self.case.reopen()
        self.row=self.case.host.db.execute('SELECT * FROM model_requests WHERE task_id=?',(self.case.task_id,)).fetchone()
        self.evidence=dict(task_id=self.case.task_id,generation_id=self.row['generation_id'],profile_hash=self.row['profile_hash'],request_hash=self.row['request_hash'],
                           provider_request_id=None,response=response,note='Trusted synthetic receipt for CLI acceptance')
        self.profile_path=self.case.directory/'approved-profile.json'
        self.envelope=dict(configuration=self.case.config,profile=asdict(self.case.profile),grant=asdict(self.case.grant))
        self.profile_path.write_text(canonical(self.envelope),encoding='utf-8')
        self.evidence_path=self.case.directory/'evidence.json'
        self.evidence_path.write_text(canonical(self.evidence),encoding='utf-8')

    def tearDown(self):
        self.assertEqual(len(self.case.fixture.requests),0)
        self.case.host.close()
        self.case.fixture.close()

    def command(self,action):
        self.case.host.close()
        environment={key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if key in os.environ}
        environment['PYTHONIOENCODING']='utf-8'
        args=[sys.executable,str(REPO/'integrations/reconcile_provider.py'),action,'--task-id',self.case.task_id]
        args += ['--state',str(self.case.host.state)] if action=='status' else ['--profile',str(self.profile_path),'--evidence',str(self.evidence_path),'--generation-id',self.row['generation_id']]
        result=subprocess.run(args,env=environment,capture_output=True,text=True,encoding='utf-8',timeout=90)
        self.case.host=ProviderHost(self.case.config,self.case.profile,self.case.grant)
        self.assertNotIn(self.case.fixture.token,result.stdout+result.stderr)
        return result

    def test_status_is_read_only_and_keeps_reservation(self):
        before=self.case.host.broker.ledger.audit(self.case.task_id)
        result=self.command('status')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['requests'][0]['state'],'outcome_unknown')
        self.assertEqual(self.case.host.broker.ledger.audit(self.case.task_id),before)

    def test_apply_is_atomic_idempotent_and_never_resumes(self):
        first=self.command('apply')
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertFalse(json.loads(first.stdout)['automatic_resume'])
        self.assertEqual(json.loads(first.stdout)['network_requests'],0)
        second=self.command('apply')
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertEqual(json.loads(first.stdout)['receipt'],json.loads(second.stdout)['receipt'])
        self.assertEqual(self.case.host.get(self.case.task_id)['state'],'interrupted')
        self.assertEqual(self.case.host.db.execute('SELECT COUNT(*) FROM provider_reconciliations').fetchone()[0],1)

    def test_wrong_evidence_does_not_release_budget(self):
        self.evidence_path.write_text(canonical({**self.evidence,'request_hash':'wrong'}),encoding='utf-8')
        before=self.case.host.broker.ledger.status(self.case.task_id)
        result=self.command('apply')
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['error'],'reconciliation_identity_conflict')
        self.assertEqual(self.case.host.broker.ledger.status(self.case.task_id),before)

    def test_live_apply_without_key_cannot_fall_back_to_fixture_credential(self):
        self.envelope['profile'].update(mode='model_live',endpoint=ROUTES['deepseek']['endpoint'])
        self.envelope['grant'].update(mode='model_live',fixture_token='')
        self.profile_path.write_text(canonical(self.envelope),encoding='utf-8')
        result=self.command('apply')
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['error'],'provider_credential_unavailable')


if __name__=='__main__':
    unittest.main(verbosity=2)
