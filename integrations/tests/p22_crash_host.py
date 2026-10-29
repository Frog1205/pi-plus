"""Fixture-only subprocess driver for hard Host exits, never real providers."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas_bridge.provider_host import ProviderHost
from atlas_bridge.provider_policy import ProviderProfile,ProviderGrant

config=json.loads(sys.stdin.readline())
profile=config['profile']
profile['documents']=tuple(profile['documents'])
if profile['mode']!='model_fixture':
    raise RuntimeError('fixture driver only')
host=ProviderHost(config['host'],ProviderProfile(**profile),ProviderGrant(**config['grant']))
print(json.dumps({'task_id':config['task_id']}),flush=True)
def kill_host(task_id,generation_id,job):
    os._exit(95)
host.run(config['task_id'],after_model=kill_host if config['fault']=='after_commit' else None)
