"""Repeatable P2.0 loopback-only protocol/fault verification; no real providers."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
parent = root/'.artifacts/atlas-p2'
parent.mkdir(parents=True,exist_ok=True)
evidence = Path(tempfile.mkdtemp(prefix='verification-',dir=parent))
environment = {key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if key in os.environ}
environment.update(PYTHONIOENCODING='utf-8',PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true',ATLAS_P2_EVIDENCE=str(evidence/'observations.jsonl'))
result = subprocess.run([sys.executable,'integrations/tests/test_p2_protocol.py'],cwd=root,env=environment,capture_output=True,text=True,encoding='utf-8',timeout=240)
output = result.stdout+result.stderr
print(output,flush=True)
(evidence/'protocol.log').write_text(output,encoding='utf-8')
pin = json.loads((root/'packages/atlas-runtime/p2-probe-lock.json').read_text('utf-8'))
names = list(pin['files'])+['integrations/tests/test_p2_protocol.py','integrations/tests/fake_responses.py','integrations/tests/p2_crash_host.py','integrations/run_p2_probe.py','integrations/lock_p2_probe.py']
count = re.search(r'Ran (\d+) tests',output)
summary = dict(time=datetime.now(timezone.utc).isoformat(),mode='model_fixture',model_connected=False,real_provider_requests=0,
               exit_code=result.returncode,test_cases=int(count.group(1)) if count else None,
               credential_environment='allowlist only, no provider secrets',endpoint='127.0.0.1 ephemeral fixture port; no external endpoint',
               source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(names))})
(evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print('P2.0 evidence: '+str(evidence))
raise SystemExit(result.returncode)
