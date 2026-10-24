"""Minimal Python SDK for Atlas Brain HTTP API."""
from __future__ import annotations

import json
import urllib.request


class AtlasClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, method: str = "GET", payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=body, method=method, headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header('Authorization', 'Bearer ' + self.token)
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())

    def status(self):
        return self._request("/api/v1/status")

    def run(self, goal: str, actor: str = "sdk"):
        return self._request("/api/v1/runs", "POST", {"goal": goal, "actor": actor})

    def timeline(self, run_id: str):
        from urllib.parse import quote
        return self._request("/api/v1/runs/" + quote(run_id, safe="") + "/timeline")

    def memories(self, query: str = ""):
        from urllib.parse import quote
        return self._request("/api/v1/memories?q=" + quote(query))

    def submit_task(self, request_id: str, project_id: str, goal: str, documents: list[str]):
        return self._request('/api/v1/tasks', 'POST', dict(request_id=request_id, project_id=project_id, goal=goal, documents=documents))

    def task(self, task_id: str, resource: str = ''):
        from urllib.parse import quote
        if resource not in ('', 'events', 'operations', 'artifact'):
            raise ValueError('invalid task resource')
        return self._request('/api/v1/tasks/' + quote(task_id, safe='') + ('/' + resource if resource else ''))

    def task_action(self, task_id: str, action: str):
        from urllib.parse import quote
        if action not in ('cancel', 'resume'):
            raise ValueError('invalid task action')
        return self._request('/api/v1/tasks/' + quote(task_id, safe='') + '/' + action, 'POST', {})
