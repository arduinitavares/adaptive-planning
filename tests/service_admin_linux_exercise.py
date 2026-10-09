"""Synthetic named-volume maintenance exercise; never launches a server or Docker.

Mount this tests directory read-only at /exercise/tests. Run exercise, stop that
container, then verify-restart in a separate container on the same owned /data.
Only the coordinator creates/cleans Docker resources. State is synthetic/private.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from adaptive_planning import admin,plans,recovery
from adaptive_planning.config import Config
from adaptive_planning.store import Store
from adaptive_planning.types import DomainError,HostWork,MutationReceipt,parse_command
from tests.service_fixtures import FakeClock,checkpoint,context,make_service,step,workspace


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def maintenance(directory,command,*arguments):
    result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin',command,'--data-dir',str(directory),*map(str,arguments)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
    assert result.returncode==0, f'Local {command} failed (exit={result.returncode})'
    assert not result.stdout, 'Successful maintenance emitted unexpected output'


def denied(call,code):
    try: call()
    except DomainError as error: assert error.code==code, (error.code,code)
    else: raise AssertionError(f'Expected {code} refusal')


def snapshot(directory):
    return {p.name:(digest(p),p.stat().st_mode,p.stat().st_ino) for p in directory.iterdir() if p.is_file()}


def exercise(directory,state_file):
    assert not (directory/'planning.sqlite').exists() and not state_file.exists(), 'Exercise needs a newly owned empty volume'
    admin.initialize_installation(Config(directory))
    credential=digest(directory/'service-token')
    backup=directory/'synthetic-backup.sqlite'
    preserved=directory/'synthetic-preserved.sqlite'
    exported=directory/'synthetic-public.json'
    with make_service(directory) as original:
        owner=original.create(steps=(step('a'),),creation_id='linux-original')
        old_resource=original.register(workspace('/synthetic/linux/original',1201),owner)
        assignment=original.service.execute(original.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(old_resource)),receipt=owner))
        old_command=original.command('start_step',dict(origin_worker='worker-a',relay=None,observed_activity='synthetic old capability'),receipt=owner,attempt=assignment)
        old_command=replace(old_command,expected_revision=None,ownership_version=None)
        original.service.execute(old_command)
        maintenance(directory,'backup','--destination',backup)
        maintenance(directory,'export','--destination',exported)
        assert owner.private.orchestrator_token not in exported.read_text()
        late_owner=original.create(steps=(step('a'),),creation_id='linux-postbackup')
        late_claim=workspace('/synthetic/linux/postbackup',1202)
        late_resource=original.register(late_claim,late_owner)
        original.service.execute(original.command('assign_step',dict(step_id='a',assignee='worker-b',context=context(late_resource)),receipt=late_owner))
        assert original.service.read_service_info().schema_version==2
    backup_hash=digest(backup)
    maintenance(directory,'restore','--source',backup,'--preserve-current',preserved)
    assert digest(backup)==backup_hash
    with make_service(directory) as restored:
        info=restored.service.read_service_info()
        assert info.store_hold and info.epoch!=owner.current_metadata.epoch
        original_plan=restored.service.read_plan(owner.current_metadata.plan_id)
        assert original_plan.metadata.generation!=owner.current_metadata.generation
        denied(lambda:restored.service.execute(old_command),'stale_epoch')
        assert restored.store.read(lambda db:db.execute('SELECT count(*) FROM resources WHERE resource_id=?',(late_resource,)).fetchone()[0])==0
        with closing(sqlite3.connect(preserved)) as db:
            assert db.execute('SELECT count(*) FROM plans WHERE plan_id=?',(late_owner.current_metadata.plan_id,)).fetchone()[0]==1
        inspection=restored.create(steps=(step('a'),),creation_id='linux-inspection')
        work=HostWork('post-backup-worker','worker-b','running',(),(late_claim,),checkpoint().evidence)
        payload=dict(backup_cutoff=info.restore_cutoff,inventory_complete=False,absent_work_inventory=(work,),resource_resolutions=(),reason='synthetic explicit local inventory')
        restored.service.execute(restored.command('resolve_store_hold',payload,receipt=inspection))
        assert restored.service.read_service_info().store_hold
        restored.service.execute(restored.command('resolve_store_hold',dict(payload,inventory_complete=True),receipt=inspection))
        assert not restored.service.read_service_info().store_hold
        resource=restored.register(late_claim,inspection)
        assign=restored.command('assign_step',dict(step_id='a',assignee='worker-c',context=context(resource)),receipt=inspection)
        denied(lambda:restored.service.execute(assign),'resource_conflict')
        orphan=next(r for r in restored.service.read_plan(inspection.current_metadata.plan_id).coordination.reservations if r.absent_work_id==work.work_id)
        restored.service.execute(restored.command('release_resource_use',dict(use_id=orphan.use_id,resource_ids=orphan.resource_ids,reason='synthetic explicit whole writer stopped',stopped=True),receipt=inspection))
        restored.service.execute(restored.command('assign_step',dict(step_id='a',assignee='worker-c',context=context(resource)),receipt=inspection))
        state=dict(format=1,epoch=info.epoch,original_plan_id=original_plan.plan_id,original_generation=original_plan.metadata.generation,
                   inspection_plan_id=inspection.current_metadata.plan_id,inspection_metadata=plans.to_jsonable(restored.service.read_plan(inspection.current_metadata.plan_id).metadata),
                   sequence=restored.store.get_store_sequence(),credential_sha256=credential,backup_sha256=backup_hash,
                   old_command=plans.to_jsonable(old_command),old_attempt_id=assignment.committed['attempt_id'],exercise_container=socket.gethostname())
    # Separate untouched newer-schema probe; the actual volume authority stays2.
    probe=directory/'synthetic-newer-probe'
    probe.mkdir(mode=0o700)
    shutil.copyfile(directory/'planning.sqlite',probe/'planning.sqlite')
    with closing(sqlite3.connect(probe/'planning.sqlite')) as db,db:
        db.execute('UPDATE installation SET schema_version=999')
    (probe/'service-token').write_bytes(b'synthetic-private-newer-token-1234567890')
    os.chmod(probe/'planning.sqlite',0o640)
    before=snapshot(probe)
    denied(lambda:Store(probe/'planning.sqlite',FakeClock()),'schema_newer')
    assert snapshot(probe)==before
    descriptor=os.open(state_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w') as handle:
        json.dump(state,handle,sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    assert digest(directory/'service-token')==credential
    return dict(backup_restore=True,newer_untouched=True,old_capability_fenced=True,inventory_hold_clear_conflict_release_assign=True,source_unchanged=True)


def verify_restart(directory,state_file):
    assert state_file.stat().st_mode&0o777==0o600
    state=json.loads(state_file.read_text())
    assert state['format']==1
    assert state['exercise_container']!=socket.gethostname(), 'Restart verification needs a different owned container'
    with make_service(directory) as restarted:
        info=restarted.service.read_service_info()
        assert info.epoch==state['epoch'] and info.schema_version==2 and not info.store_hold
        plan=restarted.service.read_plan(state['original_plan_id'])
        assert plan.metadata.generation==state['original_generation']
        assert plans.to_jsonable(restarted.service.read_plan(state['inspection_plan_id']).metadata)==state['inspection_metadata']
        assert restarted.store.get_store_sequence()==state['sequence']
        proof=restarted.store.read(lambda db:recovery.restored_proof(db,state['original_plan_id'],state['old_attempt_id']))
        assert proof[0][3]=='stale' and proof[4]=='native'
        denied(lambda:restarted.service.execute(parse_command(state['old_command'])),'stale_epoch')
    assert digest(directory/'service-token')==state['credential_sha256']
    assert digest(directory/'synthetic-backup.sqlite')==state['backup_sha256']
    return dict(distinct_container_identity=True,epoch_generation_history_persisted=True,credential_source_unchanged=True,old_capability_fenced=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('exercise','verify-restart'))
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--state-file',type=Path,required=True)
    args=parser.parse_args()
    assert sys.platform=='linux', 'This exercise records actual Linux only'
    assert os.getuid()==10001 and os.getgid()==10001, 'Exercise must run as the product nonroot user'
    version=sqlite3.sqlite_version_info
    assert version>=(3,51,3) or (3,50,7)<=version<(3,51,0), 'Unqualified SQLite WAL runtime; coordinator must supply a supported runtime'
    details=args.data_dir.stat()
    assert details.st_uid==10001 and details.st_gid==10001 and details.st_mode&0o777==0o700
    assert args.state_file.parent.resolve()==args.data_dir.resolve()
    result=(exercise if args.phase=='exercise' else verify_restart)(args.data_dir,args.state_file)
    print(json.dumps(dict(phase=args.phase,python=platform.python_version(),sqlite=sqlite3.sqlite_version,system=platform.system(),machine=platform.machine(),uid=os.getuid(),gid=os.getgid(),container_hostname=socket.gethostname(),checks=result),sort_keys=True))


if __name__=='__main__': main()
