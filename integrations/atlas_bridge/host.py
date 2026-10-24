"""Task admission, immutable input snapshots, operation receipts, and Worker broker."""
from __future__ import annotations

import hashlib
import json
import msvcrt
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

from .windows import JobProcess, read_project_file

REPO = Path(__file__).resolve().parents[2]
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class TaskError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def document_name(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(char) < 32 for char in value):
        raise TaskError("invalid_document")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise TaskError("invalid_document")
    return value


def verify_engine():
    manifest = json.loads((REPO / "packages/atlas-runtime/engine-lock.json").read_text("utf-8"))
    for relative, expected in manifest["files"].items():
        if hashlib.sha256((REPO / relative).read_bytes()).hexdigest() != expected:
            raise TaskError("engine_pin_mismatch", 503)
    return manifest


class AtlasTaskHost:
    def __init__(self, configuration, *, after_operation=None):
        self.config = configuration
        if set(configuration) - {"state_dir", "principal", "project_id", "project_root", "documents", "timeout_seconds"}:
            raise TaskError("unknown_configuration")
        self.principal = configuration.get("principal", "local-owner")
        self.project_id = configuration["project_id"]
        if not IDENTIFIER.fullmatch(self.principal) or not IDENTIFIER.fullmatch(self.project_id):
            raise TaskError("invalid_project_identity")
        self.root = Path(configuration["project_root"]).resolve(strict=True)
        self.state = Path(configuration["state_dir"]).resolve()
        if self.state.is_relative_to(self.root):
            raise TaskError("state_directory_must_be_outside_project")
        self.documents = sorted({document_name(value) for value in configuration["documents"]})
        if not self.documents or len(self.documents) > 64:
            raise TaskError("invalid_document_grants")
        self.timeout = configuration.get("timeout_seconds", 30)
        if not isinstance(self.timeout, (int, float)) or not 5 <= self.timeout <= 60:
            raise TaskError("invalid_timeout")
        self.pin = verify_engine()
        self.policy_hash = digest({"principal": self.principal, "project_id": self.project_id, "root": str(self.root),
                                   "documents": self.documents, "engine": digest(self.pin), "policy_version": 1})
        self.node = shutil.which("node")
        if not self.node or subprocess.check_output([self.node, "--version"], text=True).strip() != self.pin["node"]:
            raise TaskError("node_version_not_validated", 503)
        self.state.mkdir(parents=True, exist_ok=True)
        self.owner = open(self.state / "owner.lock", "a+b")
        if os.fstat(self.owner.fileno()).st_size == 0:
            self.owner.write(b"0")
            self.owner.flush()
        self.owner.seek(0)
        try:
            msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            self.owner.close()
            raise TaskError("state_already_owned", 503) from error
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.state / "atlas.sqlite", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, request_id TEXT, principal TEXT, project_id TEXT,
            hash TEXT, policy_hash TEXT, request TEXT, snapshots TEXT, state TEXT, error TEXT, created REAL,
            UNIQUE(principal,project_id,request_id));
          CREATE TABLE IF NOT EXISTS operations(task_id TEXT, operation_id TEXT, tool TEXT, hash TEXT,
            state TEXT, receipt TEXT, PRIMARY KEY(task_id,operation_id));
          CREATE TABLE IF NOT EXISTS artifacts(task_id TEXT PRIMARY KEY, content TEXT, sha256 TEXT);
          CREATE TABLE IF NOT EXISTS events(task_id TEXT, sequence INTEGER, event_id TEXT UNIQUE,
            timestamp REAL, type TEXT, detail TEXT, PRIMARY KEY(task_id,sequence));
        """)
        token_path = self.state / "auth.token"
        if not token_path.exists():
            with token_path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
        self.token = token_path.read_text("utf-8").strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', self.token):
            self.db.close()
            self.owner.close()
            raise TaskError('invalid_state_token', 503)
        self.active = {}
        self.jobs = {}
        self.diagnostics = {}
        self.slot = threading.Semaphore(1)
        self.closing = False
        self.after_operation = after_operation  # trusted in-process fault injection, never part of an HTTP request
        with self.lock:
            for row in self.db.execute("SELECT id FROM tasks WHERE state='running'").fetchall():
                self._state(row[0], 'interrupted', 'host_restarted')
            recover = [row[0] for row in self.db.execute("SELECT id FROM tasks WHERE state IN ('queued','interrupted')")]
        for task_id in recover:
            self._schedule(task_id)

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text("utf-8")))

    def authenticate(self, header):
        return isinstance(header, str) and secrets.compare_digest(header.encode('utf-8'), ("Bearer " + self.token).encode('ascii'))

    def _event(self, task_id, kind, detail=None):
        sequence = self.db.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (task_id, sequence, str(uuid.uuid4()), time.time(), kind, canonical(detail or {})))

    def _state(self, task_id, state, error=None):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("UPDATE tasks SET state=?,error=? WHERE id=?", (state, error, task_id))
                self._event(task_id, state, {"error": error} if error else {})
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"request_id", "project_id", "goal", "documents"}:
            raise TaskError("invalid_request_fields")
        request_id, goal, documents = payload["request_id"], payload["goal"], payload["documents"]
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise TaskError("invalid_request_id")
        if payload["project_id"] != self.project_id:
            raise TaskError("project_not_granted", 403)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise TaskError("invalid_goal")
        if not isinstance(documents, list) or not 1 <= len(documents) <= 4 or any(not isinstance(item, str) for item in documents):
            raise TaskError("invalid_documents")
        documents = [document_name(item) for item in documents]
        if len(set(documents)) != len(documents) or any(item not in self.documents for item in documents):
            raise TaskError("document_not_granted", 403)
        normalized = {**payload, "goal": goal.strip(), "documents": sorted(documents)}
        request_hash = digest({"payload": normalized, "policy": self.policy_hash})
        with self.lock:
            if self.closing:
                raise TaskError("host_closing", 503)
            old = self.db.execute("SELECT id,hash FROM tasks WHERE principal=? AND project_id=? AND request_id=?",
                                  (self.principal, self.project_id, request_id)).fetchone()
            if old:
                if old["hash"] != request_hash:
                    raise TaskError("request_content_conflict", 409)
                return self.get(old["id"])
            if self.db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running','interrupted')").fetchone()[0] >= 32:
                raise TaskError("task_capacity_exceeded", 429)
            snapshots = []
            for index, name in enumerate(normalized["documents"]):
                try:
                    raw = read_project_file(self.root, name)
                    text = raw.decode("utf-8")
                except (OSError, UnicodeError, ValueError) as error:
                    raise TaskError("document_unavailable_or_denied", 403) from error
                snapshots.append({"id": f"d{index+1}", "name": name, "sha256": hashlib.sha256(raw).hexdigest(), "content": text})
            task_id = str(uuid.uuid4())
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)", (task_id, request_id, self.principal, self.project_id,
                    request_hash, self.policy_hash, canonical(normalized), canonical(snapshots), "queued", None, time.time()))
                self._event(task_id, "admitted", {"mode": "controlled_offline", "policy_hash": self.policy_hash})
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._schedule(task_id)
            return self.get(task_id)

    def get(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError("task_not_found", 404)
            artifact = self.db.execute("SELECT sha256 FROM artifacts WHERE task_id=?", (task_id,)).fetchone()
            return {"task_id": row["id"], "run_id": row["id"], "request_id": row["request_id"], "project_id": row["project_id"],
                    "goal": json.loads(row["request"])["goal"], "state": row["state"], "error": row["error"],
                    "mode": "controlled_offline", "model_connected": False, "engine": "pi-durable", "engine_version": "1.4.0",
                    "engine_revision": self.pin["revision"], "policy_hash": row["policy_hash"],
                    "artifact_sha256": artifact[0] if artifact else None}

    def list(self):
        with self.lock:
            return [self.get(row[0]) for row in self.db.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT 100").fetchall()]

    def events(self, task_id):
        self.get(task_id)
        with self.lock:
            return [{**dict(row), "detail": json.loads(row["detail"])} for row in self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY sequence", (task_id,))]

    def operations(self, task_id):
        self.get(task_id)
        with self.lock:
            return [{**dict(row), "receipt": json.loads(row["receipt"]) if row["receipt"] else None} for row in self.db.execute("SELECT * FROM operations WHERE task_id=?", (task_id,))]

    def artifact(self, task_id):
        self.get(task_id)
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise TaskError("artifact_not_found", 404)
            return dict(row)

    def gateway(self, task_id, tool, parameters):
        with self.lock:
            task = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["state"] != "running" or task["policy_hash"] != self.policy_hash:
                raise TaskError("task_not_executable", 403)
            if tool not in ("read_document", "publish_report") or not isinstance(parameters, dict):
                self._event(task_id, "tool_denied", {"code": "tool_not_granted"})
                raise TaskError("tool_not_granted", 403)
            if tool == "read_document":
                if set(parameters) != {"document_id"}:
                    raise TaskError("invalid_tool_parameters", 403)
                matches = [item for item in json.loads(task["snapshots"]) if item["id"] == parameters["document_id"]]
                if not matches:
                    raise TaskError("document_not_granted", 403)
                operation_id = "read:" + matches[0]["id"]
                receipt = matches[0]
            else:
                if set(parameters) != {"content"} or not isinstance(parameters["content"], str) or not 1 <= len(parameters["content"].encode("utf-8")) <= 32768:
                    raise TaskError("invalid_report", 403)
                required = {"read:" + item["id"] for item in json.loads(task["snapshots"])}
                completed = {row[0] for row in self.db.execute("SELECT operation_id FROM operations WHERE task_id=? AND state='completed'", (task_id,))}
                if not required.issubset(completed):
                    raise TaskError("sources_not_read", 403)
                operation_id = "publish:report"
                receipt = {"artifact": "report.md", "sha256": hashlib.sha256(parameters["content"].encode("utf-8")).hexdigest()}
            operation_hash = digest({"tool": tool, "parameters": parameters})
            old = self.db.execute("SELECT * FROM operations WHERE task_id=? AND operation_id=?", (task_id, operation_id)).fetchone()
            if old:
                if old["hash"] != operation_hash:
                    raise TaskError("operation_content_conflict", 409)
                if old["state"] != "completed":
                    raise TaskError("operation_requires_reconciliation", 409)
                return json.loads(old["receipt"])
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)", (task_id, operation_id, tool, operation_hash, "intent", None))
                self._event(task_id, "operation_intent", {"operation_id": operation_id, "tool": tool})
                if tool == "publish_report":
                    self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (task_id, parameters["content"], receipt["sha256"]))
                self.db.execute("UPDATE operations SET state='completed',receipt=? WHERE task_id=? AND operation_id=?", (canonical(receipt), task_id, operation_id))
                self._event(task_id, "operation_completed", {"operation_id": operation_id, "tool": tool})
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return receipt

    def _schedule(self, task_id):
        if task_id in self.active or self.closing:
            return
        cancelled = threading.Event()
        thread = threading.Thread(target=self._execute, args=(task_id, cancelled), daemon=True)
        self.active[task_id] = (thread, cancelled)
        thread.start()

    def _execute(self, task_id, cancelled):
        with self.slot:
            job = None
            try:
                if cancelled.is_set() or self.closing:
                    self._state(task_id, "interrupted" if self.closing else "cancelled")
                    return
                if verify_engine() != self.pin:
                    raise TaskError('engine_pin_changed', 503)
                self._state(task_id, "running")
                with self.lock:
                    task = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task["policy_hash"] != self.policy_hash:
                    raise TaskError("policy_changed", 409)
                runtime = self.state / "workers" / task_id
                runtime.mkdir(parents=True, exist_ok=True)
                environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT") if name in os.environ}
                environment.update({"TEMP": str(runtime), "TMP": str(runtime), "USERPROFILE": str(runtime), "HOME": str(runtime), "PI_OFFLINE": "1", "AWS_EC2_METADATA_DISABLED": "true"})
                arguments = [self.node, "--permission"]
                for path in (REPO / "packages", REPO / "node_modules", REPO / "package.json", REPO / "tsconfig.json", runtime):
                    arguments.append("--allow-fs-read=" + str(path))
                arguments += ["--allow-fs-write=" + str(runtime), "--import", (REPO / "packages/coding-agent/src/experimental/source-resolver.ts").as_uri(),
                              str(REPO / "packages/atlas-runtime/src/worker.ts")]
                job = JobProcess(arguments, runtime, environment)
                with self.lock:
                    self.jobs[task_id] = job
                    self._event(task_id, "worker_started", {"pid": job.pid, "job_process_limit": 1, "node_permission": True})
                records = queue.Queue(maxsize=32)
                protocol_overflow = threading.Event()
                def read_output():
                    try:
                        while line := job.stdout.readline(1_048_577):
                            if len(line) > 1_048_576:
                                protocol_overflow.set()
                                return
                            try:
                                record = json.loads(line)
                            except (ValueError, UnicodeError):
                                record = {"type": "error", "code": "invalid_protocol"}
                            try:
                                records.put_nowait(record)
                            except queue.Full:
                                protocol_overflow.set()
                                return
                    except (OSError, ValueError):
                        pass
                def drain_errors():
                    captured = bytearray()
                    try:
                        while chunk := job.stderr.read(4096):
                            if len(captured) < 16384:
                                captured.extend(chunk[:16384-len(captured)])
                    except (OSError, ValueError):
                        pass
                    self.diagnostics[task_id] = captured.decode("utf-8", errors="replace")  # private host diagnostics, never HTTP
                readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=drain_errors, daemon=True)]
                for reader in readers:
                    reader.start()
                config = {"protocol": 1, "run_id": task_id, "request_id": task["request_id"], "goal": json.loads(task["request"])["goal"],
                          "documents": [{key: item[key] for key in ("id", "name", "sha256")} for item in json.loads(task["snapshots"])], "mode": "controlled_offline"}
                job.stdin.write((canonical({"type": "start", "configuration": config}) + "\n").encode("utf-8"))
                deadline, calls, result = time.monotonic() + self.timeout, 0, None
                while result is None:
                    if protocol_overflow.is_set():
                        raise TaskError("protocol_limit", 503)
                    if self.closing:
                        raise TaskError("host_shutdown", 409)
                    if cancelled.is_set():
                        raise TaskError("cancelled", 409)
                    if time.monotonic() > deadline:
                        raise TaskError("worker_timeout", 504)
                    try:
                        record = records.get(timeout=0.05)
                    except queue.Empty:
                        if job.poll() is not None:
                            raise TaskError("worker_exited", 503)
                        continue
                    if not isinstance(record, dict) or record.get("protocol") != 1:
                        raise TaskError("invalid_protocol", 503)
                    if record.get("type") == "tool_request":
                        calls += 1
                        if calls > 12 or not isinstance(record.get("id"), str):
                            raise TaskError("tool_budget_exceeded", 403)
                        try:
                            receipt = self.gateway(task_id, record.get("tool"), record.get("parameters"))
                            response = {"type": "tool_result", "id": record["id"], "ok": True, "result": receipt}
                            if self.after_operation:
                                self.after_operation(task_id, record.get("tool"), job)
                        except TaskError as error:
                            response = {"type": "tool_result", "id": record["id"], "ok": False, "error": error.code}
                            with self.lock:
                                self._event(task_id, "tool_denied", {"code": error.code})
                        job.stdin.write((canonical(response) + "\n").encode("utf-8"))
                    elif record.get("type") == "result":
                        result = record
                    else:
                        raise TaskError("worker_failed", 503)
                job.stdin.close()
                with self.lock:
                    artifact = self.db.execute("SELECT * FROM artifacts WHERE task_id=?", (task_id,)).fetchone()
                    denied = self.db.execute("SELECT 1 FROM events WHERE task_id=? AND type='tool_denied'", (task_id,)).fetchone()
                if result.get("status") != "done" or not artifact or denied:
                    raise TaskError("task_contract_not_satisfied", 409)
                self._state(task_id, "completed")
            except TaskError as error:
                self._state(task_id, "cancelled" if error.code == "cancelled" else "interrupted" if error.code in ("host_shutdown", "worker_exited", "worker_timeout") else "failed", error.code)
            except (OSError, ValueError, sqlite3.Error):
                self._state(task_id, "cancelled" if cancelled.is_set() and not self.closing else "interrupted", "worker_transport_interrupted")
            finally:
                if job:
                    job.close()
                with self.lock:
                    self.jobs.pop(task_id, None)
                    self.active.pop(task_id, None)

    def cancel(self, task_id):
        with self.lock:
            self.get(task_id)
            if task_id in self.active:
                self.active[task_id][1].set()
                if task_id in self.jobs:
                    self.jobs[task_id].kill()
                else:
                    self._state(task_id, "cancelled")
            return self.get(task_id)

    def resume(self, task_id):
        with self.lock:
            task = self.get(task_id)
            if task["state"] != "interrupted":
                raise TaskError("task_not_resumable", 409)
            if task_id in self.active:
                raise TaskError("task_still_stopping", 409)
            self._schedule(task_id)
            return self.get(task_id)

    def close(self):
        with self.lock:
            self.closing = True
            threads = list(self.active.values())
            for _, cancelled in threads:
                cancelled.set()
            for job in self.jobs.values():
                job.kill()
        for thread, _ in threads:
            thread.join(10)
        if any(thread.is_alive() for thread, _ in threads):
            raise RuntimeError("Workers did not stop; retaining state ownership")
        self.db.close()
        self.owner.close()
