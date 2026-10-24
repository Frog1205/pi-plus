"""Reproduce the reviewed adapter in the sibling Atlas workbench, fail on drift."""
import argparse
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
bundle = root/'integrations/workbench'
manifest = json.loads((bundle/'manifest.json').read_text('utf-8'))
parser = argparse.ArgumentParser()
parser.add_argument('--check',action='store_true')
args = parser.parse_args()
changes = []
for name,record in manifest.items():
    target = root.parent/name
    source = bundle/name
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if hashlib.sha256(source.read_bytes()).hexdigest()!=record['installed_sha256']:
        raise SystemExit('Bundle checksum mismatch: '+name)
    if actual==record['installed_sha256']:
        continue
    if args.check or actual!=record['baseline_sha256']:
        raise SystemExit('Workbench has different content; review before replacing: '+name)
    changes.append((target,source))
for target,source in changes:
    backup = target.with_name(target.name+'.before-p1.bak')
    with backup.open('xb') as stream:
        stream.write(target.read_bytes())
    target.write_bytes(source.read_bytes())
print('Atlas workbench adapter verified' if not changes else f'Installed {len(changes)} reviewed files with backups')
