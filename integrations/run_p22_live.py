"""Explicit bounded live acceptance. Only the shipped synthetic file can be sent."""
import argparse
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

from atlas_bridge.host import REPO,TaskError,canonical
from atlas_bridge.provider_host import ProviderHost,verify_provider
from atlas_bridge.provider_policy import ProviderProfile,ProviderGrant,ROUTES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',required=True,choices=tuple(ROUTES))
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--request-id',default='p22-live-1')
    options=parser.parse_args()
    route=ROUTES[options.provider]
    base=REPO/'.artifacts/atlas-p22/live-2026-10-26'/options.provider
    project=base/'project'
    project.mkdir(parents=True,exist_ok=True)
    source=(REPO/'integrations/fixtures/p22-project/README.md').read_bytes()
    document=project/'README.md'
    if document.exists() and document.read_bytes()!=source:
        raise TaskError('live_input_snapshot_drift')
    if not document.exists():
        document.write_bytes(source)
    source_sha=hashlib.sha256(source).hexdigest()
    profile=ProviderProfile('p22-live-'+options.provider,options.provider,route['endpoint'],'model_live',('README.md',),
                            remote_model=route['model'],credential_ref=route['key_ref'],input_price=route['input_price'],output_price=route['output_price'],
                            batch_id='p22-live-2026-10-26-'+options.provider)
    grant=ProviderGrant(options.provider,'model_live',source_sha,5000000000)
    config=dict(state_dir=str(base/'state'),project_root=str(project),project_id='fixture',documents=['README.md'],timeout_seconds=60)
    envelope=dict(configuration=config,profile=asdict(profile),grant=asdict(grant),
                  authorization=dict(date='2026-10-26',source='synthetic fixture only',combined_cap_cny=10,provider_cap_cny=5,
                                     keys='DEEPSEEK_API_KEY / MINIMAX_API_KEY; Host only'))
    (base/'approved-profile.json').write_text(json.dumps(envelope,ensure_ascii=False,indent=2),encoding='utf-8')
    print(canonical(dict(stage='live_preflight',provider=profile.provider,model=profile.remote_model,endpoint=profile.endpoint,
                         source_sha256=source_sha,provider_budget_cny=5,runtime_pin=verify_provider())),flush=True)
    host=ProviderHost(config,profile,grant)
    try:
        payload=dict(request_id=options.request_id,project_id='fixture',goal='仅使用合成来源 d1，生成中文报告，以“事实”和“建议”分段。至少两条事实，保留 read_document、publish_report 英文标识，每条事实引用 d1 及其原样 SHA-256。报告标注 model_live。先读文档，下一轮只发布一次报告，再简短结束。',documents=['README.md'])
        task_id=host.submit(payload)['task_id']
        result=host.run(task_id)
        first=host.broker.ledger.audit(task_id)
        semantic=False
        artifact=None
        if result['state']=='completed':
            artifact=host.artifact(task_id)
            semantic=all(term in artifact['content'] for term in ('事实','建议','read_document','publish_report',source_sha,'model_live'))
            assert host.submit(payload)['task_id']==task_id
            host.run(task_id)
            assert host.broker.ledger.audit(task_id)==first
        public=dict(time=datetime.now(timezone.utc).isoformat(),provider=profile.provider,requested_model=profile.remote_model,
                    mode='model_live',model_connected=result['model_connected'],state=result['state'],error=result['error'],
                    acceptance_passed=result['state']=='completed' and semantic,source_sha256=source_sha,
                    report_sha256=artifact['sha256'] if artifact else None,model_budget=result['model_budget'],
                    dispatch_attempts=result['model_budget']['dispatches'],confirmed_http_sent=sum(event['type']=='provider_http_sent' for event in host.events(task_id)),
                    complete_provider_responses=host.db.execute('SELECT COUNT(*) FROM provider_receipts WHERE task_id=?',(task_id,)).fetchone()[0],
                    fee_cny_upper_estimate=result['model_budget']['held_cost']/1000000000,
                    bill_verified=False,task_id=task_id,request_id=options.request_id,profile_hash=profile.fingerprint,runtime_pin=host.runtime_hash,
                    no_extra_dispatch_on_duplicate=result['state']=='completed',limitations=['Local upper-rate estimate, not a provider bill','No remote exactly-once or cancellation guarantee'])
        private=dict(public,ledger=first,source_sha256={name:hashlib.sha256((REPO/name).read_bytes()).hexdigest() for name in json.loads((REPO/'packages/atlas-runtime/provider-lock.json').read_text('utf-8'))['files']})
        credential=host.broker._credential
        for file in (host.state/'provider-workers').rglob('*'):
            if file.is_file() and credential.encode('utf-8') in file.read_bytes():
                raise TaskError('credential_in_worker_state',503)
        if credential in canonical(private)+host.stderr+canonical(host.trace)+canonical(host.events(task_id)):
            raise TaskError('credential_in_public_evidence',503)
        host.close()
        host=ProviderHost(config,profile,grant)
        assert host.broker.ledger.audit(task_id)==first
        public['reopen_preserves_ledger']=True
        (base/'acceptance.json').write_text(json.dumps(public,ensure_ascii=False,indent=2),encoding='utf-8')
        (base/'private-evidence.json').write_text(json.dumps(private,ensure_ascii=False,indent=2),encoding='utf-8')
        if artifact:
            (base/'report.md').write_text(artifact['content'],encoding='utf-8')
        assert document.read_bytes()==source
        print(json.dumps(public,ensure_ascii=False,indent=2),flush=True)
        return 0 if public['acceptance_passed'] else 1
    finally:
        host.close()


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except TaskError as error:
        print(canonical(dict(error=error.code,real_model_acceptance=False)),flush=True)
        raise SystemExit(1) from None
