"""Explicitly refresh the reviewed Atlas engine pin after dependency/source review."""
from pathlib import Path
import hashlib
import json
import subprocess

root = Path(__file__).resolve().parents[1]
paths = ["packages/durable/src", "packages/ai/src", "packages/chord/src", "packages/telemetry/src"]
files = subprocess.check_output(["git", "ls-files", "-z", "--", *paths], cwd=root).decode().split("\0")
files = [name for name in files if name]
files += ["packages/durable/package.json", "packages/ai/package.json", "packages/chord/package.json", "packages/telemetry/package.json",
          "packages/coding-agent/src/experimental/source-resolver.ts", "packages/atlas-runtime/src/worker.ts", "package-lock.json", "tsconfig.json"]
manifest = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "durable_version": "1.4.0", "node": subprocess.check_output(["node", "--version"], text=True).strip(),
            "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(files)}}
(root / "packages/atlas-runtime/engine-lock.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"Pinned {len(manifest['files'])} reviewed files")
