"""Explicitly review/pin P2.1 sources without modifying P1 or P2.0 locks."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root/'packages/atlas-runtime/p2-probe-lock.json').read_text('utf-8'))
names = ['integrations/atlas_bridge/model_policy.py','integrations/atlas_bridge/model_ledger.py',
         'integrations/atlas_bridge/model_broker.py','integrations/atlas_bridge/governed_host.py',
         'packages/atlas-runtime/src/governed-worker.ts']
manifest = dict(mode='model_fixture',protocol=2,governance=1,engine_base_revision=baseline['engine_base_revision'],
                files={**baseline['files'],**{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}})
(root/'packages/atlas-runtime/governance-lock.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
print('Pinned governed fixture runtime, '+str(len(manifest['files']))+' files; P1/P2.0 pins unchanged')
