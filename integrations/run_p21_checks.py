"""Collect P2.1 acceptance evidence with an allowlisted, credential-free environment."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

root = Path(__file__).resolve().parents[1]
parent = root/'.artifacts/atlas-p21'
parent.mkdir(parents=True,exist_ok=True)
evidence = Path(tempfile.mkdtemp(prefix='verification-',dir=parent))
environment = {key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if key in os.environ}
environment.update(PYTHONIOENCODING='utf-8',PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true',ATLAS_P21_EVIDENCE=str(evidence/'observations.jsonl'))
child = subprocess.Popen([sys.executable,'-u','integrations/tests/test_p2_governance.py'],cwd=root,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
timed_out = threading.Event()
def stop_test():
    timed_out.set()
    child.kill()
timer = threading.Timer(900,stop_test)
timer.start()
lines = []
try:
    with (evidence/'governance.log').open('w',encoding='utf-8') as log:
        for line in child.stdout:
            lines.append(line)
            log.write(line)
            log.flush()
            print(line,end='',flush=True)
    exit_code = child.wait()
finally:
    timer.cancel()
output = ''.join(lines)
pin = json.loads((root/'packages/atlas-runtime/governance-lock.json').read_text('utf-8'))
names = list(pin['files'])+['integrations/tests/test_p2_governance.py','integrations/tests/p21_crash_host.py','integrations/tests/governed_responses.py','integrations/tests/fake_responses.py','integrations/run_p21_checks.py','integrations/lock_governance.py']
count = re.search(r'Ran (\d+) tests',output)
summary = dict(time=datetime.now(timezone.utc).isoformat(),mode='model_fixture',model_connected=False,real_provider_requests=0,
               exit_code=exit_code,test_cases=int(count.group(1)) if count else None,timed_out=timed_out.is_set(),
               credential_environment='allowlist-only; no provider secrets',source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(names))})
(evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print('P2.1 evidence: '+str(evidence))
raise SystemExit(exit_code or int(timed_out.is_set()))
