from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get('ATLAS_BASE_URL', 'http://127.0.0.1:8765')


def request(path: str, method: str = "GET", data: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == 'task':
        from sdk import AtlasClient
        token_file = os.environ.get('ATLAS_TOKEN_FILE')
        if not token_file:
            raise SystemExit('Set ATLAS_TOKEN_FILE to the host auth.token path')
        client = AtlasClient(BASE, Path(token_file).read_text('utf-8').strip())
        if len(sys.argv) >= 7 and sys.argv[2] == 'submit':
            result = client.submit_task(sys.argv[3], sys.argv[4], ' '.join(sys.argv[6:]), [sys.argv[5]])
        elif len(sys.argv) >= 4 and sys.argv[2] == 'get':
            result = client.task(sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else '')
        elif len(sys.argv) == 4 and sys.argv[2] in ('cancel','resume'):
            result = client.task_action(sys.argv[3], sys.argv[2])
        else:
            raise SystemExit('task submit <request_id> <project_id> <document> <goal> | get <task_id> [events|operations|artifact] | cancel|resume <task_id>')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif command == "status":
        print(json.dumps(request("/api/v1/status"), ensure_ascii=False, indent=2))
    elif command == "run":
        print(json.dumps(request("/api/v1/runs", "POST", {"goal": " ".join(sys.argv[2:]), "actor": "cli"}), ensure_ascii=False, indent=2))
    elif command == "timeline":
        if len(sys.argv) < 3:
            raise SystemExit("usage: python atlas.py timeline <run_id>")
        run_id = urllib.parse.quote(sys.argv[2], safe="")
        print(json.dumps(request("/api/v1/runs/" + run_id + "/timeline"), ensure_ascii=False, indent=2))
    elif command == "memories":
        query = urllib.parse.quote(" ".join(sys.argv[2:]))
        print(json.dumps(request("/api/v1/memories?q=" + query), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: python atlas.py status|run <goal>|timeline <run_id>|memories [query]")


if __name__ == "__main__":
    main()
