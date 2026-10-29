"""Collect two-provider P2.2 fixture evidence; real credentials are never inherited."""
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
parent = root/'.artifacts/atlas-p22'
parent.mkdir(parents=True,exist_ok=True)
evidence = Path(tempfile.mkdtemp(prefix='verification-',dir=parent))
environment = {key:os.environ[key] for key in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if key in os.environ}
environment.update(PYTHONIOENCODING='utf-8',PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true',ATLAS_P22_EVIDENCE=str(evidence/'observations.jsonl'))
operator = len(sys.argv)>1 and sys.argv[1]=='--operator'
test_file='integrations/tests/test_p22_operator.py' if operator else 'integrations/tests/test_p22_providers.py'
selectors=sys.argv[2:] if operator else sys.argv[1:]
child = subprocess.Popen([sys.executable,'-u',test_file,*selectors],cwd=root,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
timed_out = threading.Event()
def stop_test():
    timed_out.set()
    child.kill()
timer = threading.Timer(900,stop_test)
timer.start()
lines = []
try:
    with (evidence/'providers.log').open('w',encoding='utf-8') as log:
        for line in child.stdout:
            lines.append(line)
            log.write(line)
            log.flush()
            print(line,end='',flush=True)
    exit_code = child.wait()
finally:
    timer.cancel()
output = ''.join(lines)
pin = json.loads((root/'packages/atlas-runtime/provider-lock.json').read_text('utf-8'))
names = list(pin['files'])+['integrations/tests/test_p22_providers.py','integrations/tests/test_p22_operator.py','integrations/tests/p22_crash_host.py','integrations/tests/provider_server.py','integrations/run_p22_checks.py','integrations/lock_provider.py','integrations/fixtures/p22-project/README.md']
count = re.search(r'Ran (\d+) tests',output)
summary = dict(time=datetime.now(timezone.utc).isoformat(),mode='model_fixture',model_connected=False,real_provider_requests=0,
               exit_code=exit_code,test_cases=int(count.group(1)) if count else None,timed_out=timed_out.is_set(),test_file=test_file,selectors=selectors,
               credential_environment='allowlist-only; no provider secrets',source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(names))})
(evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print('P2.2 fixture evidence: '+str(evidence))
raise SystemExit(exit_code or int(timed_out.is_set()))
