"""Collect repeatable local-only P1 evidence with individual command exit codes."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
(root/'.artifacts/atlas-p1').mkdir(parents=True,exist_ok=True)
evidence = Path(tempfile.mkdtemp(prefix='verification-',dir=root/'.artifacts/atlas-p1'))
environment = {name:os.environ[name] for name in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if name in os.environ}
environment.update(PYTHONIOENCODING='utf-8',PI_OFFLINE='1',AWS_EC2_METADATA_DISABLED='true')
results = []
for name in ('test_p1.py','test_isolation.py','test_http.py'):
    result = subprocess.run([sys.executable,str(root/'integrations/tests'/name)],cwd=root,env=environment,capture_output=True,text=True,encoding='utf-8',timeout=180)
    output = result.stdout + result.stderr
    (evidence/(name+'.log')).write_text(output,encoding='utf-8')
    print(output,flush=True)
    results.append(dict(test=name,exit_code=result.returncode,log=str(evidence/(name+'.log'))))
summary = dict(time=datetime.now(timezone.utc).isoformat(),results=results,mode='controlled_offline',model_connected=False,
               credentials='provider secrets not inherited',python=sys.version.split()[0])
(evidence/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print('Evidence: '+str(evidence))
raise SystemExit(any(result['exit_code'] for result in results))
