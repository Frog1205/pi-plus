import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlas_bridge.host import AtlasTaskHost


def crash(task_id, tool, job):
    if tool == 'publish_report':
        print(json.dumps({'task_id':task_id,'worker_pid':job.pid}), flush=True)
        os._exit(93)


host = AtlasTaskHost.from_file(sys.argv[1])
host.after_operation = crash
host.submit(dict(request_id='hard-crash',project_id='fixture',goal='crash after host commit',documents=['README.md']))
time.sleep(45)
os._exit(94)
