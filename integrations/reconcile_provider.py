"""Local operator reconciliation. Status is read-only; apply never sends a request."""
import argparse
import json
from pathlib import Path
import sqlite3

from atlas_bridge.host import TaskError,canonical
from atlas_bridge.model_probe import strict_json
from atlas_bridge.provider_host import ProviderHost
from atlas_bridge.provider_policy import ProviderProfile,ProviderGrant


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','apply'))
    parser.add_argument('--state',type=Path)
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--generation-id')
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--evidence',type=Path)
    args=parser.parse_args()
    if args.action=='status':
        if args.state is None:
            parser.error('--state required for status')
        database=(args.state/'atlas.sqlite').resolve(strict=True)
        db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            task=db.execute('SELECT id,state,error FROM tasks WHERE id=?',(args.task_id,)).fetchone()
            if not task:
                raise TaskError('task_not_found',404)
            records=[dict(row) for row in db.execute('''SELECT generation_id,sequence,profile_hash,request_hash,state,dispatch_count,reserved_cost,held_cost,response_id,provider_request_id,usage,error
              FROM model_requests WHERE task_id=? ORDER BY sequence''',(args.task_id,))]
            print(canonical(dict(task=dict(task),requests=records,bill_verified=False,automatic_retry=False)))
        finally:
            db.close()
        return
    if not args.profile or not args.evidence or not args.generation_id:
        parser.error('--profile, --evidence and --generation-id required for apply')
    configuration=strict_json(args.profile.read_text('utf-8'))
    profile=configuration['profile']
    profile['documents']=tuple(profile['documents'])
    host=ProviderHost(configuration['configuration'],ProviderProfile(**profile),ProviderGrant(**configuration['grant']))
    try:
        evidence=strict_json(args.evidence.read_text('utf-8'))
        receipt=host.broker.reconcile(args.task_id,args.generation_id,evidence)
        print(canonical(dict(receipt=receipt,automatic_resume=False,network_requests=0)))
    finally:
        host.close()


if __name__=='__main__':
    try:
        main()
    except TaskError as error:
        print(canonical(dict(error=error.code)))
        raise SystemExit(1) from None
