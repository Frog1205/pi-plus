"""Additive SQLite model ledger, migrations, reservations and immutable provenance."""
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from .host import TaskError, canonical

SCHEMA = '''
CREATE TABLE model_meta(version INTEGER NOT NULL);
INSERT INTO model_meta VALUES(1);
CREATE TABLE model_tasks(task_id TEXT PRIMARY KEY REFERENCES tasks(id), mode TEXT NOT NULL,
 profile_hash TEXT, profile TEXT, runtime_hash TEXT, provenance TEXT NOT NULL);
CREATE TABLE model_batches(id TEXT PRIMARY KEY, hash TEXT NOT NULL, cost_limit INTEGER NOT NULL);
CREATE TABLE model_requests(task_id TEXT NOT NULL, generation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
 profile_hash TEXT NOT NULL, request_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 dispatch_count INTEGER NOT NULL DEFAULT 0, input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
 reserved_cost INTEGER NOT NULL, held_cost INTEGER NOT NULL, input_accounted INTEGER NOT NULL,
 output_accounted INTEGER NOT NULL, batch_id TEXT NOT NULL, body BLOB, response_hash TEXT,
 response_id TEXT, provider_request_id TEXT, usage TEXT, error TEXT,
 PRIMARY KEY(task_id,generation_id), UNIQUE(task_id,sequence));
'''


def migrate(host, *, fault=None):
    with host.lock:
        present = host.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_meta'").fetchone()
        if present:
            rows = host.db.execute('SELECT version FROM model_meta').fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise TaskError('model_schema_not_supported', 503)
            required = {'request_hash','held_cost','response_hash','provider_request_id','dispatch_count'}
            if not required.issubset({row[1] for row in host.db.execute('PRAGMA table_info(model_requests)')}):
                raise TaskError('model_schema_corrupt', 503)
            return
        # SQLite backup includes WAL; do not copy a live database file with filesystem copy.
        backup = host.state / ('before-model-v1-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite')
        target = sqlite3.connect(backup)
        try:
            host.db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise TaskError('model_migration_backup_invalid', 503)
        finally:
            target.close()
        host.db.execute('BEGIN IMMEDIATE')
        try:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    host.db.execute(statement)
            # Never reinterpret P1 or P2.0 tasks as governed model success.
            for row in host.db.execute('SELECT id FROM tasks').fetchall():
                host.db.execute('INSERT INTO model_tasks VALUES(?,?,?,?,?,?)',
                                (row[0], 'controlled_offline', None, None, None, canonical({'source':'legacy-unverified-model'})))
            if fault:
                fault()
            if host.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise TaskError('model_migration_integrity_failed', 503)
            host.db.execute('COMMIT')
        except BaseException:
            host.db.execute('ROLLBACK')
            raise


class ModelLedger:
    def __init__(self, host, profile):
        self.host, self.profile = host, profile
        with host.lock:
            row = host.db.execute('SELECT * FROM model_batches WHERE id=?', (profile.batch_id,)).fetchone()
            if row and row['hash'] != profile.batch_fingerprint:
                raise TaskError('model_batch_policy_conflict', 409)
            host.db.execute('INSERT OR IGNORE INTO model_batches VALUES(?,?,?)', (profile.batch_id, profile.batch_fingerprint, profile.batch_cost_limit))
            host.db.execute("UPDATE model_requests SET state='outcome_unknown',error='host_restarted_unknown' WHERE state='dispatching'")

    def blocked(self, task_id):
        return self.host.db.execute("SELECT 1 FROM model_requests WHERE task_id=? AND state IN ('dispatching','outcome_unknown','rejected')", (task_id,)).fetchone() is not None

    def prepare(self, task_id, generation_id, request_hash, payload, input_bound):
        profile, db = self.profile, self.host.db
        with self.host.lock:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?', (task_id,generation_id)).fetchone()
                if row:
                    if row['profile_hash'] != profile.fingerprint or row['request_hash'] != request_hash:
                        raise TaskError('model_request_content_conflict',409)
                    if row['state'] not in ('prepared','received'):
                        raise TaskError(row['error'] or 'model_outcome_unknown',409)
                    # A later unknown generation blocks replay too, never evades reconciliation.
                    if self.blocked(task_id):
                        raise TaskError('model_requires_reconciliation',409)
                    db.execute('COMMIT')
                    return row
                if self.blocked(task_id):
                    raise TaskError('model_requires_reconciliation',409)
                if db.execute("SELECT 1 FROM model_requests WHERE task_id=? AND state='prepared'",(task_id,)).fetchone():
                    raise TaskError('model_prepared_identity_required',409)
                count, inputs, outputs, cost = db.execute('SELECT COUNT(*),COALESCE(SUM(input_accounted),0),COALESCE(SUM(output_accounted),0),COALESCE(SUM(held_cost),0) FROM model_requests WHERE task_id=?',(task_id,)).fetchone()
                reservation = input_bound * profile.input_price + profile.max_output_tokens * profile.output_price
                batch_cost = db.execute('SELECT COALESCE(SUM(held_cost),0) FROM model_requests WHERE batch_id=?',(profile.batch_id,)).fetchone()[0]
                if count >= profile.max_requests or inputs + input_bound > profile.task_input_tokens or outputs + profile.max_output_tokens > profile.task_output_tokens or cost + reservation > profile.task_cost_limit or batch_cost + reservation > profile.batch_cost_limit:
                    raise TaskError('model_budget_exceeded',429)
                db.execute('''INSERT INTO model_requests(task_id,generation_id,sequence,profile_hash,request_hash,payload,state,
                  input_bound,output_bound,reserved_cost,held_cost,input_accounted,output_accounted,batch_id)
                  VALUES(?,?,?,?,?,?,'prepared',?,?,?,?,?,?,?)''',
                           (task_id,generation_id,count+1,profile.fingerprint,request_hash,canonical(payload),input_bound,profile.max_output_tokens,reservation,reservation,input_bound,profile.max_output_tokens,profile.batch_id))
                self.host._event(task_id,'model_prepared',{'generation_id':generation_id,'sequence':count+1,'reserved_cost':reservation,'currency':profile.currency})
                row = db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,generation_id)).fetchone()
                db.execute('COMMIT')
                return row
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def dispatch(self, task_id, generation_id):
        with self.host.lock:
            self.host.db.execute('BEGIN IMMEDIATE')
            try:
                changed = self.host.db.execute("UPDATE model_requests SET state='dispatching',dispatch_count=dispatch_count+1 WHERE task_id=? AND generation_id=? AND state='prepared' AND dispatch_count=0",(task_id,generation_id)).rowcount
                if changed != 1:
                    raise TaskError('model_dispatch_conflict',409)
                self.host._event(task_id,'model_dispatching',{'generation_id':generation_id})
                self.host.db.execute('COMMIT')
            except BaseException:
                self.host.db.execute('ROLLBACK')
                raise

    def fail(self, task_id, generation_id, state, code, request_id=None):
        with self.host.lock:
            self.host.db.execute('BEGIN IMMEDIATE')
            try:
                self.host.db.execute('UPDATE model_requests SET state=?,error=?,provider_request_id=? WHERE task_id=? AND generation_id=?',(state,code,request_id,task_id,generation_id))
                self.host._event(task_id,state,{'generation_id':generation_id,'code':code,'reservation_retained':True})
                self.host.db.execute('COMMIT')
            except BaseException:
                self.host.db.execute('ROLLBACK')
                raise

    def receive(self, task_id, generation_id, body, response, response_hash, request_id):
        usage, db, profile = response.get('usage'), self.host.db, self.profile
        with self.host.lock:
            row = db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,generation_id)).fetchone()
            violation = usage is not None and (usage['input_tokens'] > row['input_bound'] or usage['output_tokens'] > row['output_bound'])
            inputs = usage['input_tokens'] if usage else row['input_bound']
            outputs = usage['output_tokens'] if usage else row['output_bound']
            cost = inputs*profile.input_price + outputs*profile.output_price
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('''UPDATE model_requests SET state=?,body=?,response_hash=?,response_id=?,provider_request_id=?,usage=?,
                  held_cost=?,input_accounted=?,output_accounted=?,error=? WHERE task_id=? AND generation_id=?''',
                           ('rejected' if violation else 'received',body,response_hash,response['id'],request_id,canonical(usage),
                            max(cost,row['reserved_cost']) if violation or usage is None else cost,
                            max(inputs,row['input_bound']) if violation else inputs,max(outputs,row['output_bound']) if violation else outputs,
                            'model_usage_exceeds_reservation' if violation else None,task_id,generation_id))
                self.host._event(task_id,'model_usage_rejected' if violation else 'model_received',{'generation_id':generation_id,'response_hash':response_hash,'usage_available':usage is not None,'reservation_retained':usage is None or violation})
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            if violation:
                raise TaskError('model_usage_exceeds_reservation',409)

    def status(self, task_id):
        with self.host.lock:
            rows = self.host.db.execute('SELECT * FROM model_requests WHERE task_id=? ORDER BY sequence',(task_id,)).fetchall()
            return dict(requests=len(rows), dispatches=sum(row['dispatch_count'] for row in rows),
                        held_cost=sum(row['held_cost'] for row in rows), currency=self.profile.currency,
                        usage_available=bool(rows) and all(row['usage'] and json.loads(row['usage']) is not None for row in rows),
                        requires_reconciliation=any(row['state'] in ('dispatching','outcome_unknown') for row in rows),
                        unknown_usage=sum(row['usage'] is None or json.loads(row['usage']) is None for row in rows))
