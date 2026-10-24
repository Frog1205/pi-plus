"""Synthetic parent-death fixture; never loaded by the HTTP host."""
import json
import os
from pathlib import Path
import shutil
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas_bridge.windows import JobProcess
job = JobProcess([shutil.which('node'), str(Path(__file__).with_name('isolation_probe.mjs')), 'wait'], Path(sys.argv[1]),
                 {name:os.environ[name] for name in ('SystemRoot','WINDIR') if name in os.environ})
print(job.stdout.readline().decode().strip(), flush=True)
time.sleep(60)
