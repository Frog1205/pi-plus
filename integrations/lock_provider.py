"""Review/pin the explicit provider bridge. Earlier stage pins are immutable."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root/'packages/atlas-runtime/governance-lock.json').read_text('utf-8'))
names = ['integrations/atlas_bridge/provider_policy.py','integrations/atlas_bridge/provider_protocol.py',
         'integrations/atlas_bridge/provider_ledger.py','integrations/atlas_bridge/provider_broker.py','integrations/atlas_bridge/provider_host.py',
         'packages/atlas-runtime/src/provider-transport.ts','packages/atlas-runtime/src/provider-worker.ts',
         'integrations/run_p22_live.py','integrations/reconcile_provider.py','integrations/fixtures/p22-project/README.md']
value = dict(governance=2,protocol=2,engine_base_revision=baseline['engine_base_revision'],
             files={**baseline['files'],**{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}})
(root/'packages/atlas-runtime/provider-lock.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8',newline='\n')
print('Pinned provider runtime: '+str(len(value['files']))+' files; P1/P2.0/P2.1 pins unchanged')
