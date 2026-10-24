"""Run an actual Atlas server + SDK + CLI acceptance on the registered project."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

repository = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repository.parent))
from sdk import AtlasClient

config_path = repository/'.artifacts/atlas-p1/atlas-local.json'
config = json.loads(config_path.read_text('utf-8'))
evidence = Path(tempfile.mkdtemp(prefix='acceptance-',dir=repository/'.artifacts/atlas-p1'))
before = {name:hashlib.sha256((repository.parent/name).read_bytes()).hexdigest() for name in config['documents']}
request_id = 'p1-acceptance-'+hashlib.sha256(json.dumps({'sources':before,'engine':json.loads((repository/'packages/atlas-runtime/engine-lock.json').read_text('utf-8'))},sort_keys=True).encode()).hexdigest()[:16]
environment = {name:os.environ[name] for name in ('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP') if name in os.environ}
environment['PYTHONIOENCODING'] = 'utf-8'
log = (evidence/'server.log').open('w',encoding='utf-8')
server = subprocess.Popen([sys.executable,str(repository.parent/'server.py'),'--port','0','--p1-config',str(config_path)],
                          cwd=repository.parent,env=environment,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8')
try:
    line = server.stdout.readline().strip()
    if not line.startswith('Atlas Brain Workbench: http://127.0.0.1:'):
        raise RuntimeError('Atlas server did not start; see server.log')
    url = line.split(': ',1)[1]
    client = AtlasClient(url,(Path(config['state_dir'])/'auth.token').read_text('utf-8').strip())
    task = client.submit_task(request_id,'atlas-brain',
                             '核验 Atlas Brain 项目说明与任务 API 入口，生成带来源摘要的受控报告。',['README.md','server.py'])
    task_id = task['task_id']
    deadline = time.monotonic()+45
    while task['state'] not in ('completed','failed','interrupted','cancelled') and time.monotonic()<deadline:
        time.sleep(.1)
        task = client.task(task_id)
    assert task['state']=='completed',task
    duplicate = client.submit_task(request_id,'atlas-brain',
                                  '核验 Atlas Brain 项目说明与任务 API 入口，生成带来源摘要的受控报告。',['README.md','server.py'])
    assert duplicate['task_id']==task_id
    artifact = client.task(task_id,'artifact')
    operations = client.task(task_id,'operations')
    events = client.task(task_id,'events')
    assert len(operations)==3 and all(item['state']=='completed' for item in operations)
    assert before=={name:hashlib.sha256((repository.parent/name).read_bytes()).hexdigest() for name in config['documents']}
    for sha in before.values():
        assert sha in artifact['content']
    environment.update(ATLAS_BASE_URL=url,ATLAS_TOKEN_FILE=str(Path(config['state_dir'])/'auth.token'))
    cli = subprocess.run([sys.executable,str(repository.parent/'atlas.py'),'task','get',task_id],
                         cwd=repository.parent,env=environment,capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert cli.returncode==0,cli.stderr
    assert json.loads(cli.stdout)['task_id']==task_id
    try:
        urllib.request.urlopen(url+'/api/v1/tasks/'+task_id+'/artifact',timeout=5)
        raise AssertionError('unauthorized read was allowed')
    except urllib.error.HTTPError as error:
        assert error.code==401
    for name,value in [('task',task),('operations',operations),('events',events)]:
        (evidence/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    (evidence/'report.md').write_text(artifact['content'],encoding='utf-8')
    summary = dict(time=datetime.now(timezone.utc).isoformat(),task_id=task_id,state=task['state'],mode=task['mode'],
                   model_connected=False,engine_version=task['engine_version'],source_sha256=before,
                   artifact_sha256=artifact['sha256'],operations=len(operations),events=len(events),
                   request_reused_same_task=True,unauthorized_artifact_status=401,cli_verified=True,source_files_unchanged=True)
    (evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('Evidence: '+str(evidence))
finally:
    server.kill()
    server.communicate(timeout=10)
    log.close()
