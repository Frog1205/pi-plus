import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas_bridge.model_probe import ProbeHost,FixtureProfile
configuration = json.loads(sys.stdin.readline())
host = ProbeHost(configuration['host'])
host.attach_fixture(FixtureProfile(configuration['port'],configuration['fixture_token']))
task_id = host.submit(dict(request_id='crash-probe',project_id='fixture',goal='Exercise fixture model protocol',documents=['README.md']))['task_id']
print(json.dumps({'task_id':task_id}),flush=True)
def crash(task_id,generation_id,job):
    os._exit(93)
host.run_probe(task_id,after_model=crash if configuration['fault']=='after_model_commit' else None)
host.close()
