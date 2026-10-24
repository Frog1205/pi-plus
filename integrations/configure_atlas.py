"""Generate a trusted single-project configuration; never prints its API token."""
import json
from pathlib import Path

repository = Path(__file__).resolve().parents[1]
configuration = dict(principal='local-owner',project_id='atlas-brain',project_root=str(repository.parent),
                     state_dir=str(Path.home()/'.atlas-brain/p1'),documents=['README.md','server.py'],timeout_seconds=30)
destination = repository/'.artifacts/atlas-p1/atlas-local.json'
destination.parent.mkdir(parents=True,exist_ok=True)
if destination.exists() and json.loads(destination.read_text('utf-8')) != configuration:
    raise SystemExit('Existing configuration differs; review it manually instead of overwriting')
destination.write_text(json.dumps(configuration,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(destination)
