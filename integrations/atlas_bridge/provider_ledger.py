"""Atomic provider receipt and budget settlement, with auditable reconciliation."""
from datetime import datetime,timezone
import hashlib
import sqlite3
import uuid

from .host import TaskError, canonical
from .model_ledger import ModelLedger
from .model_probe import strict_json


def migrate_provider(host):
    with host.lock:
        exists = host.db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_meta'").fetchone()
        if exists:
            if [row[0] for row in host.db.execute('SELECT version FROM provider_meta')] != [1]:
                raise TaskError('provider_schema_not_supported',503)
            return
        backup = host.state/('before-provider-v1-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.sqlite')
        target = sqlite3.connect(backup)
        try:
            host.db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise TaskError('provider_backup_invalid',503)
        finally:
            target.close()
        host.db.execute('BEGIN IMMEDIATE')
        try:
            host.db.execute('CREATE TABLE provider_meta(version INTEGER NOT NULL)')
            host.db.execute('INSERT INTO provider_meta VALUES(1)')
            host.db.execute('''CREATE TABLE provider_receipts(task_id TEXT,generation_id TEXT,provider TEXT,model TEXT,
              response_id TEXT,raw BLOB,sha256 TEXT,PRIMARY KEY(task_id,generation_id))''')
            host.db.execute('''CREATE TABLE provider_reconciliations(task_id TEXT,generation_id TEXT,evidence_hash TEXT,
              receipt TEXT,PRIMARY KEY(task_id,generation_id))''')
            host.db.execute('COMMIT')
        except BaseException:
            host.db.execute('ROLLBACK')
            raise


class ProviderLedger(ModelLedger):
    def receive_provider(self, task_id, generation_id, raw, body, response, remote, request_id, *, evidence=None, fault=None):
        db, profile, usage = self.host.db,self.profile,response.get('usage')
        with self.host.lock:
            row = db.execute('SELECT * FROM model_requests WHERE task_id=? AND generation_id=?',(task_id,generation_id)).fetchone()
            if not row or row['state'] not in ('dispatching','outcome_unknown'):
                raise TaskError('provider_receipt_state_conflict',409)
            violation = usage is not None and (usage['input_tokens'] > row['input_bound'] or usage['output_tokens'] > row['output_bound'])
            inputs = usage['input_tokens'] if usage else row['input_bound']
            outputs = usage['output_tokens'] if usage else row['output_bound']
            cost = inputs*profile.input_price+outputs*profile.output_price
            receipt = dict(task_id=task_id,generation_id=generation_id,provider=profile.provider,remote_model=remote['model'],
                           response_id=remote['id'],response_sha256=hashlib.sha256(raw).hexdigest(),usage_available=usage is not None)
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('''UPDATE model_requests SET state=?,body=?,response_hash=?,response_id=?,provider_request_id=?,usage=?,
                  held_cost=?,input_accounted=?,output_accounted=?,error=? WHERE task_id=? AND generation_id=?''',
                           ('rejected' if violation else 'received',body,hashlib.sha256(body).hexdigest(),remote['id'],request_id,canonical(usage),
                            max(cost,row['reserved_cost']) if violation or usage is None else cost,
                            max(inputs,row['input_bound']) if violation else inputs,max(outputs,row['output_bound']) if violation else outputs,
                            'model_usage_exceeds_reservation' if violation else None,task_id,generation_id))
                db.execute('INSERT INTO provider_receipts VALUES(?,?,?,?,?,?,?)',(task_id,generation_id,profile.provider,remote['model'],remote['id'],raw,receipt['response_sha256']))
                if evidence:
                    db.execute('INSERT INTO provider_reconciliations VALUES(?,?,?,?)',(task_id,generation_id,evidence,canonical(receipt)))
                self.host._event(task_id,'model_usage_rejected' if violation else 'model_reconciled' if evidence else 'model_received',receipt)
                if fault:
                    fault()
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            if violation:
                raise TaskError('model_usage_exceeds_reservation',409)
            return receipt

    def audit(self, task_id):
        with self.host.lock:
            return dict(task=self.host.get(task_id),budget=self.status(task_id),requests=[dict(row) for row in self.host.db.execute('''SELECT generation_id,sequence,profile_hash,request_hash,state,dispatch_count,
              input_bound,output_bound,reserved_cost,held_cost,response_id,provider_request_id,usage,error
              FROM model_requests WHERE task_id=? ORDER BY sequence''',(task_id,))],
                        reconciliations=[dict(row) for row in self.host.db.execute('SELECT generation_id,evidence_hash,receipt FROM provider_reconciliations WHERE task_id=?',(task_id,))])
