"""Trusted subprocess fault driver. Synthetic credentials arrive only over stdin."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from atlas_bridge.governed_host import GovernedHost
from atlas_bridge.model_policy import ModelProfile
from atlas_bridge.model_probe import FixtureProfile

config = json.loads(sys.stdin.readline())
profile = config['profile']
profile['documents'] = tuple(profile['documents'])
host = GovernedHost(config['host'],ModelProfile(**profile),FixtureProfile(config['port'],config['fixture_token']))
task_id = config['task_id']
print(json.dumps({'task_id':task_id}),flush=True)
def crash(task_id,generation_id,job):
    os._exit(94)
host.run(task_id,after_model=crash if config['fault']=='after_model_commit' else None)
