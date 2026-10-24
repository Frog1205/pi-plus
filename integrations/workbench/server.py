from __future__ import annotations

import json
import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from runtime import runtime

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'pi-plus/integrations'))
from atlas_bridge.host import AtlasTaskHost
from atlas_bridge.http_api import handle_tasks


class Handler(BaseHTTPRequestHandler):
    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if handle_tasks(self, 'GET', parsed.path):
            return
        if parsed.path == "/api/v1/status":
            return self._json(runtime.status())
        if parsed.path == "/api/v1/brain":
            return self._json(runtime.brain_model())
        if parsed.path == "/api/v1/brain/architecture":
            return self._json(runtime.brain_model()["architecture"])
        if parsed.path == "/api/v1/events":
            return self._json(runtime.events_recent())
        if parsed.path == "/api/v1/memories":
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self._json(runtime.memory_search(query))
        if parsed.path == "/api/v1/runs":
            return self._json(list(runtime.runs.values()))
        if parsed.path.startswith("/api/v1/runs/") and parsed.path.endswith("/timeline"):
            run_id = parsed.path[len("/api/v1/runs/"):-len("/timeline")].strip("/")
            timeline = runtime.run_timeline(run_id)
            return self._json(timeline or {"error": "run_not_found"}, 200 if timeline else 404)
        static_files = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
        if parsed.path in static_files:
            filename, mime = static_files[parsed.path]
            body = (ROOT / "static" / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if handle_tasks(self, 'POST', urlparse(self.path).path):
            return
        if self.path == "/api/v1/runs":
            try:
                body = self._body()
                return self._json(runtime.run(str(body.get("goal", "")), str(body.get("actor", "api"))), 201)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        self._json({"error": "not_found"}, 404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--p1-config', type=Path)
    args = parser.parse_args()
    task_host = AtlasTaskHost.from_file(args.p1_config) if args.p1_config else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.task_host = task_host
    print(f"Atlas Brain Workbench: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if task_host:
            task_host.close()
