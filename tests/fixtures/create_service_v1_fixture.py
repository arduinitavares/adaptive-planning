"""Reproduce the synthetic v1 image with the *unchanged* accepted22 checkout.

Run with that checkout's locked uv environment, passing --source and --output.
No historic production package is vendored. UUID/token entropy is deterministic
test entropy; all rows otherwise come from the actual old normal producers.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

BASE = 'b199c13c220660312269389c467b9f428ee14c9c'
SCHEMA_SHA = '2923e8359416fa4e176a97507605c190b3a1ced7c55e67b1c9bfffcb6e8873de'
PRODUCERS = ('schema', 'store', 'config', 'admin', 'app', 'plans', 'ownership',
             'attempts', 'checkpoints', 'reviews', 'resources', 'recovery', 'imports', 'types', 'core')

def generate(source: Path, output: Path) -> None:
    source = source.resolve()
    head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if head != BASE or hashlib.sha256((source/'adaptive_planning/schema.py').read_bytes()).hexdigest() != SCHEMA_SHA:
        raise RuntimeError('Fixture requires the unchanged named accepted22 schema1 producer.')
    for module in PRODUCERS:
        actual = (source/f'adaptive_planning/{module}.py').read_bytes()
        committed = subprocess.check_output(['git', '-C', str(source), 'show', f'{BASE}:adaptive_planning/{module}.py'])
        if actual != committed:
            raise RuntimeError(f'Old producer has changed: {module}')
    sys.path.insert(0, str(source))
    from adaptive_planning.core import prepare_publication
    from adaptive_planning.schema import SCHEMA_VERSION
    from adaptive_planning.types import DocumentRef, ExecutionContextClaims, ResourceClaim, Review
    from tests.service_fixtures import checkpoint, context, legacy_proposal, make_service, seed_orphan_use, step, workspace
    if SCHEMA_VERSION != 1:
        raise RuntimeError('Actual old producer must be schema1.')
    output.mkdir(parents=True, exist_ok=True)
    target = output/'service_v1.sqlite'
    if target.exists():
        raise FileExistsError(target)
    identifiers = itertools.count(1)
    tokens = itertools.count(1)
    with TemporaryDirectory(prefix='service-v1-generator-') as directory, \
            patch('uuid.uuid4', side_effect=lambda: uuid.UUID(int=next(identifiers))), \
            patch('secrets.token_urlsafe', side_effect=lambda n: f'synthetic-only-token-{next(tokens):032d}'):
        f = make_service(Path(directory))
        accepted = f.create(steps=(step('a', review_required=True),), creation_id='v1-accepted')
        value = checkpoint()
        assignment = f.assign(accepted)
        f.start(accepted, assignment)
        f.submit(accepted, assignment, value)
        f.service.execute(f.command('record_review', {'step_id':'a', 'review':Review('synthetic-reviewer', value.artifact, 'passed', (), value.evidence, None)}, receipt=accepted))
        f.accept(accepted, assignment, value)
        active = f.create(steps=(step('a'),), creation_id='v1-active')
        repo = f.register(ResourceClaim('synthetic-host','repository',7,901,'/synthetic/v1-repo',False,None,None), active)
        work = f.register(workspace('/synthetic/v1-repo/work',902,repository_id=repo),active)
        external = f.register(ResourceClaim('synthetic-host','external',None,None,None,False,None,'synthetic:v1-external'), active)
        a = f.service.execute(f.command('assign_step',{'step_id':'a','assignee':'worker-a','context':context(work,repository_id=repo,external_ids=(external,))},receipt=active))
        f.start(active,a)
        stale = f.create(steps=(step('a'),),creation_id='v1-stale')
        stale_assignment = f.assign(stale)
        f.clock.advance(7200)
        f.service.execute(f.command('sweep_stale',{},receipt=stale))
        # Test-only orphan topology. The normal registry producer supplies identity.
        orphan_resource=f.register(workspace('/synthetic/v1-orphan',903),active)
        orphan=seed_orphan_use(f,orphan_resource)
        def extra_history(db):
            db.execute("INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at) VALUES('v1-orphan-released',?,NULL,NULL,'seeded-orphan','workspace','released',?)",(orphan_resource,f.clock().isoformat()))
            db.execute("INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at) VALUES('v1-orphan-second',?,NULL,NULL,'seeded-orphan','workspace','unresolved',?)",(orphan_resource,f.clock().isoformat()))
        f.store.write(extra_history)
        proposal=legacy_proposal()
        proposal['status']='running'
        proposal['steps'][0]['status']='in-progress'
        snapshot=prepare_publication(proposal,None,timestamp='2026-10-06T00:00:00Z',generation='00000000-0000-4000-8000-000000000001',expected_revision=0,expected_generation=None,authorization_note='synthetic legacy authorization',max_file_bytes=10*1024*1024,max_history=100)
        imported=[]
        for known, inode in ((True,904),(False,906)):
            payload=dict(host_id='synthetic-host',source_directory_id=None,source_directory_claim=workspace(f'/synthetic/v1-import-source-{known}',inode+1),original_task_id=snapshot['taskId'],original_snapshot=snapshot,provenance=DocumentRef('synthetic-legacy.json',None,None,'v1',None),writers_stopped='synthetic publisher stopped; worker unconfirmed',known_contexts={'step-1':ExecutionContextClaims('non-repository',None,workspace('/synthetic/v1-import-writer',inode),False,(),True,None)} if known else {})
            imported.append(f.service.execute(f.command('import_plan',payload,key=f'v1-import-{known}')).current_metadata.plan_id)
        descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
        os.close(descriptor)
        src=sqlite3.connect(f.store.path)
        dst=sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.commit()
            assert dst.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
            assert dst.execute('PRAGMA foreign_key_check').fetchall()==[]
            tables=[r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            identities={t:[list(row) for row in dst.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid')] for t in tables}
        finally:
            dst.close()
            src.close()
            f.cleanup()
    hashes={f'adaptive_planning/{m}.py':hashlib.sha256((source/f'adaptive_planning/{m}.py').read_bytes()).hexdigest() for m in PRODUCERS}
    hashes['tests/service_fixtures.py']=hashlib.sha256((source/'tests/service_fixtures.py').read_bytes()).hexdigest()
    provenance=dict(format=1,source_commit=BASE,source_schema_version=1,source_sha256=hashes,uv_lock_sha256=hashlib.sha256((source/'uv.lock').read_bytes()).hexdigest(),python=sys.version,sqlite=sqlite3.sqlite_version,image_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),normal_producers=True,synthetic_only=True,test_only_seeds=['seeded-orphan singleton','same-resource multiple orphan uses and released history'],named_ids=dict(accepted_plan=accepted.current_metadata.plan_id,active_plan=active.current_metadata.plan_id,active_attempt=a.committed['attempt_id'],stale_plan=stale.current_metadata.plan_id,stale_attempt=stale_assignment.committed['attempt_id'],imported_plans=imported,orphan_use=orphan.use_id),identity_history=identities)
    provenance_path=output/'service_v1_provenance.json'
    fd=os.open(provenance_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as handle:
        json.dump(provenance,handle,indent=2,sort_keys=True)
        handle.write('\n')
    print(json.dumps({'schema':1,'source_commit':BASE,'image_sha256':provenance['image_sha256'],'table_counts':{t:len(rows) for t,rows in identities.items()}}))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    generate(args.source,args.output)
