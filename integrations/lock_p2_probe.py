"""Explicit fixture pin; does not refresh or modify the production P1 engine pin."""
import hashlib
import json
from pathlib import Path
root = Path(__file__).resolve().parents[1]
baseline = json.loads((root/'packages/atlas-runtime/engine-lock.json').read_text('utf-8'))
names = ['packages/atlas-runtime/src/broker-transport.ts','packages/atlas-runtime/test/p2-probe-worker.ts',
         'integrations/atlas_bridge/model_probe.py','integrations/atlas_bridge/host.py','integrations/atlas_bridge/windows.py']
manifest = dict(mode='model_fixture',protocol=2,engine_base_revision=baseline['revision'],files={**baseline['files'],
                  **{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}})
(root/'packages/atlas-runtime/p2-probe-lock.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
print('Pinned fixture-only protocol, '+str(len(manifest['files']))+' files; P1 pin unchanged')
