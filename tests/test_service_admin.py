"""Task23 local maintenance contracts, with synthetic authorities only."""
from __future__ import annotations
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from adaptive_planning import admin
from adaptive_planning.config import Config
from adaptive_planning.store import Store
from adaptive_planning.types import DomainError
from tests.service_fixtures import FakeClock, checkpoint, context, make_service, step, workspace

def file_set(directory):
    return {p.name:(p.read_bytes(),p.stat().st_mode,p.stat().st_ino) for p in directory.iterdir() if p.is_file()}


def entry_set(directory):
    """Record source entries without following symbolic links or their targets."""
    result={'.':(directory.lstat().st_mode,directory.lstat().st_dev,directory.lstat().st_ino,None)}
    for entry in directory.iterdir():
        details=entry.lstat()
        content=os.readlink(entry) if entry.is_symlink() else entry.read_bytes() if entry.is_file() else None
        result[entry.name]=(details.st_mode,details.st_dev,details.st_ino,content)
    return result


class TestService(unittest.TestCase):
    def test_reserved_destination_case_aliases_refuse_through_physical_parent_alias(self):
        reserved=('planning.sqlite','planning.sqlite-wal','planning.sqlite-shm','planning.sqlite-journal',
                  'service-token','.planning-operation.lock','.planning-maintenance.json')
        for function in (admin.backup_database,admin.export_database):
            for filename in reserved:
                with self.subTest(operation=function.__name__,reserved=filename),TemporaryDirectory() as name:
                    root=Path(name)
                    with make_service(root/'authority') as f:
                        alias=root/'different-parent-spelling'
                        alias.symlink_to(f.directory,target_is_directory=True)
                        destination=alias/filename.upper()
                        before=entry_set(f.directory)
                        with self.assertRaises(DomainError) as error:
                            function(f.store,destination)
                        self.assertEqual(error.exception.code,'invalid')
                        self.assertEqual(entry_set(f.directory),before)

    def test_reserved_case_basename_in_a_different_directory_is_a_valid_destination(self):
        for function in (admin.backup_database,admin.export_database):
            with self.subTest(operation=function.__name__),TemporaryDirectory() as name:
                root=Path(name)
                destination_directory=root/'separate-backups';destination_directory.mkdir()
                destination=destination_directory/'PLANNING.SQLITE-WAL'
                with make_service(root/'authority') as f:
                    function(f.store,destination)
                self.assertTrue(destination.is_file())
                if function is admin.backup_database:
                    with closing(sqlite3.connect(destination)) as db:
                        self.assertEqual(db.execute('SELECT authority_kind FROM installation').fetchone(),('backup',))
                else:
                    self.assertEqual(json.loads(destination.read_text())['installation']['schema_version'],2)

    def test_public_restore_refuses_dangling_source_sidecars_untouched(self):
        for suffix in ('-wal','-shm','-journal'):
            with self.subTest(suffix=suffix),TemporaryDirectory() as name:
                root=Path(name);directory=root/'installation';source=root/'source.sqlite'
                with make_service(directory) as f:
                    admin.backup_database(f.store,source)
                target=root/'never-created-target'
                Path(str(source)+suffix).symlink_to(target)
                os.chmod(source,0o640)
                before=(entry_set(root),entry_set(directory))
                with self.assertRaises(DomainError) as error:
                    admin.restore_database(Config(directory),source,preserve_current=directory/'preserved.sqlite')
                self.assertEqual(error.exception.code,'invalid')
                self.assertEqual((entry_set(root),entry_set(directory)),before)
                self.assertFalse(target.exists())
                self.assertFalse(target.is_symlink())

    def test_real_cold_backup_refuses_dangling_main_and_all_sidecars_untouched(self):
        from adaptive_planning.store import maintenance_ownership
        # Root R1 ruling permits this actual existing cold backup route under
        # real maintenance ownership; public Store/restore are covered separately.
        for suffix in ('','-wal','-shm','-journal'):
            with self.subTest(suffix=suffix),TemporaryDirectory() as name:
                root=Path(name);directory=root/'installation'
                with make_service(directory) as f:
                    source=f.store.path
                target=root/'never-created-target'
                if not suffix:
                    source.unlink()
                Path(str(source)+suffix).symlink_to(target)
                before=(entry_set(root),entry_set(directory))
                failure=None
                with maintenance_ownership(directory):
                    try:
                        admin._cold_backup(source,root/'cold-copy.sqlite')
                    except Exception as error:
                        failure=error
                self.assertEqual((entry_set(root),entry_set(directory)),before)
                self.assertFalse(target.exists())
                self.assertFalse(target.is_symlink())
                self.assertIsInstance(failure,DomainError)
                self.assertEqual(failure.code,'invalid')

    def test_restore_materializes_the_same_admitted_committed_wal_snapshot_and_cutoff(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            source=directory/'source.sqlite'
            with make_service(directory) as f:
                original_cutoff=admin.backup_database(f.store,source)['restore_cutoff']
            script="""import sqlite3,sys,os
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute("INSERT INTO events(kind,actor,published_at,payload) VALUES('synthetic_committed_backup_wal','synthetic','synthetic','{}')")
db.commit()
os._exit(0)
"""
            result=subprocess.run([sys.executable,'-B','-c',script,str(source)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode,0,result.stdout.decode())
            self.assertGreater(Path(str(source)+'-wal').stat().st_size,0)
            before={key:value for key,value in file_set(directory).items() if key.startswith('source.sqlite')}
            admin.restore_database(Config(directory),source,preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT count(*) FROM events WHERE kind='synthetic_committed_backup_wal'").fetchone()[0]),1,'Restore omitted the row admitted from the committed backup WAL')
                self.assertEqual(f.service.read_service_info().restore_cutoff,original_cutoff)
                origin=f.store.read(lambda db:db.execute('SELECT source_digest,payload FROM restore_origins WHERE new_epoch=?',(f.epoch,)).fetchone())
                entries=[dict(suffix=suffix,bytes=len(before['source.sqlite'+suffix][0]),sha256=hashlib.sha256(before['source.sqlite'+suffix][0]).hexdigest()) for suffix in ('','-journal','-shm','-wal') if 'source.sqlite'+suffix in before]
                encoded=json.dumps(entries,sort_keys=True,separators=(',',':')).encode()
                self.assertEqual(origin[0],hashlib.sha256(b'adaptive-planning-source-file-set-v1\0'+encoded).hexdigest())
                self.assertEqual(json.loads(origin[1])['source_binding'],dict(encoding='file-set-v1',files=entries))
            self.assertEqual({key:value for key,value in file_set(directory).items() if key.startswith('source.sqlite')},before)

    def test_fixture_composition_failure_closes_store_and_retains_external_directory(self):
        made=[]
        def construct(*args,**kwargs):
            store=Store(*args,**kwargs);made.append(store);self.addCleanup(store.close)
            return store
        with TemporaryDirectory() as name:
            directory=Path(name)
            with patch('adaptive_planning.store.Store',side_effect=construct),patch('adaptive_planning.app.PlanningService',side_effect=RuntimeError('synthetic fixture composition failure')):
                with self.assertRaises(RuntimeError):
                    make_service(directory)
            self.assertTrue(made[0]._closed)
            self.assertTrue(directory.is_dir())

    def test_second_native_restore_retains_a_legitimate_historical_verifier_stop(self):
        from dataclasses import replace
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create(steps=(step('a',review_required=True),))
                assigned=f.assign(p);active=f.start(p,assigned);f.submit(p,active,checkpoint())
                rid=f.register(workspace('/synthetic/second-verifier',1760),p)
                use=f.service.execute(f.command('reserve_verification',dict(step_id='a',verifier='reviewer',context=context(rid),handoff_from=None,stopped_reason=None),receipt=p))
                admin.backup_database(f.store,directory/'first.sqlite')
            admin.restore_database(Config(directory),directory/'first.sqlite',preserve_current=directory/'preserved-first.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                self.inventory(f,owner)
                f.service.execute(f.command('release_resource_use',dict(use_id=use.committed['use_id'],resource_ids=(rid,),stopped=True,reason='retained first-restore verifier stop'),receipt=owner))
                admin.backup_database(f.store,directory/'second.sqlite')
            admin.restore_database(Config(directory),directory/'second.sqlite',preserve_current=directory/'preserved-second.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                payload=dict(resolution='release',reason='second-restore original ownership ended',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None)
                f.service.execute(replace(f.command('resolve_attempt',payload,receipt=owner),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                self.assertEqual(f.store.read(lambda db:db.execute('SELECT status FROM resource_uses WHERE use_id=?',(use.committed['use_id'],)).fetchone()[0]),'released')

    def test_native_restore_cannot_replace_missing_assignment_constructor_with_legacy_namespace_fallback(self):
        from adaptive_planning import resources
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create(steps=(step('a'),));f.assign(p)
                admin.backup_database(f.store,directory/'source.sqlite')
            with closing(sqlite3.connect(directory/'source.sqlite')) as db,db:
                raw=db.execute('SELECT payload FROM attempts').fetchone()[0]
                data=json.loads(raw);data.pop(resources._USE_NAMESPACE)
                db.execute('UPDATE attempts SET payload=?',(json.dumps(data,sort_keys=True),))
                db.execute("DELETE FROM events WHERE kind='assign_step'")
            before=file_set(directory)
            with self.assertRaises(DomainError):
                admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            self.assertEqual((directory/'planning.sqlite').read_bytes(),before['planning.sqlite'][0])
            self.assertTrue((directory/'preserved.sqlite').is_file())

    def test_restored_native_proof_keeps_real_revision_retirement_and_verifier_stop_lineage(self):
        from dataclasses import replace
        from adaptive_planning import recovery
        from adaptive_planning.types import RetirementRequest
        for branch in ('retirement','verifier-stop'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                directory=Path(name)
                with make_service(directory) as f:
                    p=f.create(steps=(step('a',review_required=True),))
                    rid=f.register(workspace('/synthetic/restore-real-lifecycle',1758),p)
                    assigned=f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=p))
                    if branch=='verifier-stop':
                        active=f.start(p,assigned);f.submit(p,active,checkpoint())
                        verifier=f.register(workspace('/synthetic/restore-real-verifier',1759),p)
                        reservation=f.service.execute(f.command('reserve_verification',dict(step_id='a',verifier='reviewer',context=context(verifier),handoff_from=None,stopped_reason=None),receipt=p))
                    admin.backup_database(f.store,directory/'source.sqlite')
                admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
                with make_service(directory) as f:
                    owner=self.takeover(f,p.current_metadata.plan_id)
                    attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                    if branch=='retirement':
                        view=f.service.read_plan(p.current_metadata.plan_id)
                        f.service.execute(f.command('revise_plan',dict(draft=view.draft,reason='explicit historical retirement',authorization_change=None,retire_steps=(RetirementRequest('a','original ownership retired'),)),receipt=owner))
                        f.store.read(lambda db:recovery.restored_proof(db,p.current_metadata.plan_id,attempt.attempt_id))
                    else:
                        self.inventory(f,owner)
                        f.service.execute(f.command('release_resource_use',dict(use_id=reservation.committed['use_id'],resource_ids=(verifier,),stopped=True,reason='exact historical verifier stopped'),receipt=owner))
                        payload=dict(resolution='release',reason='original ownership ended after verifier stop',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None)
                        f.service.execute(replace(f.command('resolve_attempt',payload,receipt=owner),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                        f.store.read(lambda db:recovery.restored_proof(db,p.current_metadata.plan_id,attempt.attempt_id))

    def test_hot_journal_newer_schema_refusal_recovers_only_the_disposable_clone(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory):
                pass
            target=directory/'planning.sqlite'
            script="""import sqlite3,sys,os
db=sqlite3.connect(sys.argv[1],isolation_level=None)
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA cache_size=1')
db.execute('UPDATE installation SET schema_version=999')
db.execute('CREATE TABLE synthetic_spill(value BLOB)')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE installation SET epoch='uncommitted-hot-journal'")
for i in range(20): db.execute('INSERT INTO synthetic_spill VALUES(?)',(b'x'*65536,))
os._exit(0)
"""
            result=subprocess.run([sys.executable,'-B','-c',script,str(target)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode,0,result.stdout.decode())
            journal=Path(str(target)+'-journal')
            self.assertGreater(journal.stat().st_size,512)
            self.assertTrue(any(journal.read_bytes()[:8]),'Fixture did not persist a hot rollback journal header')
            before=file_set(directory)
            with self.assertRaises(DomainError) as error:
                Store(target,FakeClock())
            self.assertEqual(error.exception.code,'schema_newer')
            self.assertEqual(file_set(directory),before)

    def test_independent_process_store_lifetime_refuses_cold_maintenance(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory):
                pass
            script="""import sys
from pathlib import Path
from adaptive_planning.store import Store
from tests.service_fixtures import FakeClock
with Store(Path(sys.argv[1]),FakeClock()) as store:
 print('ready',flush=True)
 sys.stdin.readline()
"""
            process=subprocess.Popen([sys.executable,'-B','-c',script,str(directory/'planning.sqlite')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(),'ready')
                before=file_set(directory)
                with self.assertRaises(DomainError) as error:
                    admin.migrate_installation(Config(directory),directory/'unused-backups')
                self.assertEqual(error.exception.code,'busy')
                self.assertEqual(file_set(directory),before)
            finally:
                process.stdin.write('\n');process.stdin.flush()
                process.communicate(timeout=10)
            self.assertEqual(process.returncode,0)
            admin.migrate_installation(Config(directory),directory/'unused-backups')
            self.assertFalse((directory/'unused-backups').exists())

    def test_restore_refuses_symlink_source_before_creating_preserved_artifact(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                admin.backup_database(f.store,directory/'backup.sqlite')
            source=directory/'source-link.sqlite';source.symlink_to(directory/'backup.sqlite')
            before=file_set(directory)
            with self.assertRaises(DomainError):
                admin.restore_database(Config(directory),source,preserve_current=directory/'preserved.sqlite')
            self.assertEqual(file_set(directory),before)

    def test_existing_supported_marker_with_missing_authority_table_is_refused_untouched(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                f.store.write(lambda db:db.execute('DROP TABLE restore_attempt_origins'))
            target=directory/'planning.sqlite'
            os.chmod(target,0o640)
            before=file_set(directory)
            with self.assertRaises(DomainError) as error:
                Store(target,FakeClock())
            self.assertEqual(error.exception.code,'invalid')
            self.assertEqual(file_set(directory),before)

    def test_maintenance_final_directory_sync_failure_retains_a_recoverable_manifest(self):
        from adaptive_planning.store import MANIFEST_FILENAME
        with TemporaryDirectory() as name:
            directory=Path(name)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            actual=admin._fsync_directory
            def fail_final(path):
                # Fail only once after the published manifest is removed, not at
                # earlier copy or phase writes. Real publication has occurred.
                if Path(path).resolve()==directory.resolve() and not (directory/MANIFEST_FILENAME).exists():
                    with closing(sqlite3.connect(target)) as db:
                        current=db.execute('SELECT schema_version FROM installation').fetchone()[0]
                    if current==2 and not failed[0]:
                        failed[0]=True
                        raise OSError('synthetic final directory fsync interruption')
                return actual(path)
            failed=[False]
            with patch('adaptive_planning.admin._fsync_directory',side_effect=fail_final):
                with self.assertRaises(OSError):
                    admin.migrate_installation(Config(directory),directory/'backups')
            self.assertTrue(failed[0])
            self.assertTrue((directory/MANIFEST_FILENAME).is_file(),'Final durability failure lost the pending operation manifest')
            before=file_set(directory)
            with self.assertRaises(DomainError):
                Store(target,FakeClock())
            self.assertEqual(file_set(directory),before)
            manifest=json.loads((directory/MANIFEST_FILENAME).read_text())
            admin.recover_maintenance(Config(directory),operation_id=manifest['operation_id'],decision='finish')
            with Store(target,FakeClock()) as store:
                self.assertEqual(store.schema_version,2)

    def test_stopped_declaration_accepts_exact_nonempty_whole_work_selector(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            rid=f.register(workspace('/synthetic/stopped-explicit-id',1756),owner)
            work=HostWork('stopped-explicit-id','worker-a','stopped',(rid,),(),checkpoint().evidence)
            self.inventory(f,owner,(work,),(ResourceResolution(None,work.work_id,(rid,),'stopped','entire exact named cohort stopped'),))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations,())

    def test_mcp_nested_inventory_refusals_redact_peer_data_and_leave_authority_untouched(self):
        import asyncio,copy
        from adaptive_planning import plans
        from adaptive_planning.transport import build_mcp
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            server=build_mcp(f.service)
            work=HostWork('nested-guarded','worker-a','running',(),(workspace('/synthetic/mcp-guarded',1757),),checkpoint().evidence)
            command=f.command('resolve_store_hold',dict(backup_cutoff=f.service.read_service_info().restore_cutoff,inventory_complete=True,absent_work_inventory=(work,),resource_resolutions=(),reason='typed observations'),receipt=owner)
            wire=plans.to_jsonable(command)
            async def check():
                for branch in ('extra-field','literal','boolean','foreign-host','isolated','cutoff'):
                    with self.subTest(branch=branch):
                        malformed=copy.deepcopy(wire)
                        malformed['key']=branch
                        if branch=='extra-field': malformed['payload']['absent_work_inventory'][0]['PRIVATE_NESTED_SENTINEL']='PRIVATE_NESTED_SENTINEL'
                        elif branch=='literal': malformed['payload']['absent_work_inventory'][0]['state']='PRIVATE_NESTED_SENTINEL'
                        elif branch=='boolean': malformed['payload']['inventory_complete']='PRIVATE_NESTED_SENTINEL'
                        elif branch=='foreign-host': malformed['payload']['absent_work_inventory'][0]['resource_claims'][0]['host_id']='PRIVATE_NESTED_SENTINEL'
                        elif branch=='isolated': malformed['payload']['resource_resolutions']=[plans.to_jsonable(ResourceResolution(None,work.work_id,(),'isolated','PRIVATE_NESTED_SENTINEL'))]
                        else: malformed['payload']['backup_cutoff']='PRIVATE_NESTED_SENTINEL'
                        before=f.store.read(lambda db:tuple(db.iterdump()))
                        result=await server.call_tool('resolve_store_hold',{'command':malformed})
                        self.assertTrue(result.is_error)
                        self.assertNotIn('PRIVATE_NESTED_SENTINEL',str(result))
                        self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)
            asyncio.run(check())

    def test_attached_import_survives_second_restore_and_terminal_proof_retains_decision(self):
        from dataclasses import replace
        from adaptive_planning.types import ExecutionContextClaims
        from adaptive_planning import recovery
        provenance=json.loads((Path(__file__).parent/'fixtures/service_v1_provenance.json').read_text())
        with TemporaryDirectory() as name:
            directory=Path(name)
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',directory/'planning.sqlite')
            admin.migrate_installation(Config(directory),directory/'backups')
            with make_service(directory) as f:
                admin.backup_database(f.store,directory/'first.sqlite')
            admin.restore_database(Config(directory),directory/'first.sqlite',preserve_current=directory/'first-preserved.sqlite')
            with make_service(directory) as f:
                pid=provenance['named_ids']['imported_plans'][1]
                owner=self.takeover(f,pid)
                attempt=f.service.read_plan(pid).coordination.attempts[0]
                payload=dict(resolution='attach-context',reason='known exact historical context',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=ExecutionContextClaims('non-repository',None,workspace('/synthetic/second-attached',1755),False,(),True,None))
                f.service.execute(replace(f.command('resolve_attempt',payload,receipt=owner),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                admin.backup_database(f.store,directory/'second.sqlite')
            admin.restore_database(Config(directory),directory/'second.sqlite',preserve_current=directory/'second-preserved.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,pid)
                attempt=f.service.read_plan(pid).coordination.attempts[0]
                payload=dict(resolution='release',reason='explicit attached imported ownership ended',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None)
                f.service.execute(replace(f.command('resolve_attempt',payload,receipt=owner),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                f.store.read(lambda db:recovery.restored_proof(db,pid,attempt.attempt_id))
                f.store.write(lambda db:db.execute("DELETE FROM events WHERE plan_id=? AND kind='resolve_attempt' AND json_extract(payload,'$.resolution')='release'",(pid,)))
                with self.assertRaises(DomainError):
                    f.store.read(lambda db:recovery.restored_proof(db,pid,attempt.attempt_id))

    def test_inventory_refuses_a_modified_restore_origin_and_unbounded_evidence(self):
        from dataclasses import replace
        from adaptive_planning.types import HostWork
        for branch in ('origin','evidence'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                if branch=='origin':
                    def tamper(db):
                        row=db.execute('SELECT restore_id,payload FROM restore_origins WHERE new_epoch=?',(f.epoch,)).fetchone()
                        data=json.loads(row[1]);data['source_metadata']['sequence']+=1
                        db.execute('UPDATE restore_origins SET payload=? WHERE restore_id=?',(json.dumps(data,sort_keys=True),row[0]))
                    f.store.write(tamper)
                    works=()
                else:
                    ev=replace(checkpoint().evidence[0],summary='x'*(10*1024*1024+1))
                    works=(HostWork('bounded-evidence','worker-a','running',(),(workspace('/synthetic/bounded-evidence',1752),),(ev,)),)
                before=f.store.read(lambda db:tuple(db.iterdump()))
                with self.assertRaises(DomainError):
                    self.inventory(f,owner,works)
                self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_attached_orphan_requires_retained_original_producer_and_identity_reason(self):
        from adaptive_planning.types import HostWork
        for branch in ('producer','reason'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                self.inventory(f,owner,(HostWork('bound-attachment','worker-a','unknown',(),(),checkpoint().evidence),),complete=False)
                original=f.store.read(lambda db:db.execute('SELECT producer_id FROM absent_host_work').fetchone()[0])
                work=HostWork('bound-attachment','worker-a','running',(),(workspace('/synthetic/bound-attachment',1753),),checkpoint().evidence)
                self.inventory(f,owner,(work,))
                if branch=='producer':
                    f.store.write(lambda db:db.execute('DELETE FROM host_work_declarations WHERE producer_id=?',(original,)))
                else:
                    f.store.write(lambda db:db.execute("DELETE FROM store_hold_reasons WHERE kind='absent-identity'"))
                with self.assertRaises(DomainError):
                    f.service.read_plan(owner.current_metadata.plan_id)

    def test_direct_orphan_release_rejects_duplicate_canonical_selectors(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            self.inventory(f,owner,(HostWork('direct-duplicate','worker-a','running',(),(workspace('/synthetic/direct-duplicate',1754),),checkpoint().evidence),))
            use=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
            before=f.store.read(lambda db:tuple(db.iterdump()))
            with self.assertRaises(DomainError):
                f.service.execute(f.command('release_resource_use',dict(use_id=use.use_id,resource_ids=use.resource_ids+use.resource_ids,stopped=True,reason='duplicate selector'),receipt=owner))
            self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_restored_recovery_refuses_altered_physical_cohort_and_origin_generations(self):
        from dataclasses import replace
        for branch in ('deleted-use','different-resource','origin-generation','source-digest'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                directory=Path(name)
                with make_service(directory) as f:
                    p=f.create(steps=(step('a'),))
                    rid=f.register(workspace('/synthetic/restore-cohort',1750),p)
                    other=f.register(workspace('/synthetic/restore-other',1751),p)
                    f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=p))
                    admin.backup_database(f.store,directory/'source.sqlite')
                admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
                with make_service(directory) as f:
                    owner=self.takeover(f,p.current_metadata.plan_id)
                    attempt=f.service.read_plan(owner.current_metadata.plan_id).coordination.attempts[0]
                    def tamper(db):
                        if branch=='deleted-use':
                            db.execute('DELETE FROM resource_uses WHERE attempt_id=?',(attempt.attempt_id,))
                        elif branch=='different-resource':
                            db.execute('UPDATE resource_uses SET resource_id=? WHERE attempt_id=?',(other,attempt.attempt_id))
                        elif branch=='origin-generation':
                            row=db.execute('SELECT restore_id,payload FROM restore_origins WHERE new_epoch=?',(f.epoch,)).fetchone()
                            data=json.loads(row[1]);data['generations'][p.current_metadata.plan_id][0]='invented-source-generation'
                            db.execute('UPDATE restore_origins SET payload=? WHERE restore_id=?',(json.dumps(data,sort_keys=True),row[0]))
                        else:
                            db.execute("UPDATE restore_origins SET source_digest=? WHERE new_epoch=?",('0'*64,f.epoch))
                    f.store.write(tamper)
                    command=f.command('resolve_attempt',dict(resolution='release',reason='exact source required',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                    command=replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version)
                    before=f.store.read(lambda db:tuple(db.iterdump()))
                    with self.assertRaises(DomainError):
                        f.service.execute(command)
                    self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_terminal_restored_proof_requires_its_actual_published_cancellation(self):
        from dataclasses import replace
        from adaptive_planning import recovery
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create(steps=(step('a'),));f.assign(p)
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                attempt=f.service.read_plan(owner.current_metadata.plan_id).coordination.attempts[0]
                command=replace(f.command('resolve_attempt',dict(resolution='release',reason='published end of ownership',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version)
                f.service.execute(command)
                f.store.read(lambda db:recovery.restored_proof(db,p.current_metadata.plan_id,attempt.attempt_id))
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT count(*) FROM events WHERE plan_id=? AND kind='resolve_attempt'",(p.current_metadata.plan_id,)).fetchone()[0]),1)
                f.store.write(lambda db:db.execute("DELETE FROM events WHERE plan_id=? AND kind='resolve_attempt'",(p.current_metadata.plan_id,)))
                with self.assertRaises(DomainError):
                    f.store.read(lambda db:recovery.restored_proof(db,p.current_metadata.plan_id,attempt.attempt_id))

    def test_export_includes_public_aliases_redirects_and_omits_unknown_history_objects(self):
        with make_service() as f:
            p=f.create()
            rid=f.register(workspace('/synthetic/export-public',1749),p)
            def seed(db):
                # Synthetic public alias/redirect topology and an unknown event,
                # not an implementation of export.
                db.execute("INSERT INTO resources(resource_id,host_id,kind,physical_identity,canonical_path,status,created_at) SELECT 'export-retired',host_id,kind,physical_identity,'/synthetic/old-export-public','retired','synthetic' FROM resources WHERE resource_id=?",(rid,))
                db.execute("INSERT INTO resource_redirects VALUES('export-retired',?,'synthetic')",(rid,))
                db.execute("INSERT INTO aliases VALUES('export-alias','synthetic-host','/synthetic/public-alias',?,'synthetic')",(rid,))
                db.execute("INSERT INTO events(plan_id,plan_revision,kind,actor,published_at,payload) VALUES(?,1,'future_private_kind','synthetic','synthetic',?)",(p.current_metadata.plan_id,json.dumps(dict(summary={'token':'EXPORT_PRIVATE_OBJECT'},authorization_note={'grant':'EXPORT_PRIVATE_OBJECT'}))))
            f.store.write(seed)
            target=f.directory/'export-safe.json'
            admin.export_database(f.store,target)
            data=json.loads(target.read_text())
            with self.subTest(branch='history-redaction'):
                self.assertFalse('EXPORT_PRIVATE_OBJECT' in target.read_text())
            with self.subTest(branch='aliases'):
                self.assertTrue(any(item['alias_id']=='export-alias' for item in data.get('aliases',())))
            with self.subTest(branch='redirects'):
                self.assertTrue(any(item['retired_id']=='export-retired' for item in data.get('redirects',())))
            unknown=next(item for item in data['events'] if item['kind']=='future_private_kind')
            self.assertNotIn('payload',unknown)

    def test_server_construction_and_run_exceptions_dispose_the_actual_app(self):
        from adaptive_planning import __main__ as entry
        from adaptive_planning.app import build_app
        for branch in ('config','server','run','returned'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                config=Config(Path(name))
                admin.initialize_installation(config)
                app=build_app(config)
                store=app.state.service._store
                self.addCleanup(store.close)
                with patch.object(entry,'build_app',return_value=app):
                    if branch in ('config','server'):
                        with patch.object(entry.uvicorn,'Config' if branch=='config' else 'Server',side_effect=RuntimeError('synthetic server constructor failure')):
                            with self.assertRaises(RuntimeError):
                                entry.build_server(config)
                    else:
                        with patch.object(entry,'load_config',return_value=config),patch.object(entry.uvicorn.Server,'run',side_effect=RuntimeError('synthetic run failure') if branch=='run' else None):
                            self.assertEqual(entry.main(['serve']),1 if branch=='run' else 0)
                self.assertTrue(store._closed,'Server owner retained app lifetime after construction/run exit')

    def test_nonstarted_app_and_lifespan_exit_release_actual_lifetime(self):
        import asyncio,fcntl
        from adaptive_planning.app import build_app
        for branch in ('nonstarted','shutdown','failed-startup'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                config=Config(Path(name))
                admin.initialize_installation(config)
                app=build_app(config)
                store=app.state.service._store
                self.addCleanup(store.close)
                if branch=='nonstarted':
                    # A deterministic public disposal route must exist; assert the
                    # held descriptor first so missing state attributes are not RED proof.
                    self.assertIsNotNone(store._lease_fd)
                    app.state.close()
                    self.assertTrue(store._closed,'Built but never started app retained lifetime')
                else:
                    async def check():
                        if branch=='failed-startup':
                            from contextlib import asynccontextmanager
                            @asynccontextmanager
                            async def failed():
                                raise RuntimeError('synthetic session manager startup failure')
                                yield
                            with patch.object(app.state.mcp.session_manager,'run',failed):
                                with self.assertRaises(RuntimeError):
                                    async with app.router.lifespan_context(app):
                                        pass
                        else:
                            async with app.router.lifespan_context(app):
                                self.assertFalse(store._closed)
                    asyncio.run(check())
                    self.assertTrue(store._closed,'ASGI lifespan retained lifetime after exit')
                descriptor=os.open(config.data_dir,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
                try:
                    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)

    def test_real_mcp_inventory_invocation_has_nested_declarations_and_resolutions(self):
        import asyncio
        from adaptive_planning import plans
        from adaptive_planning.transport import build_mcp
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            server=build_mcp(f.service)
            work=HostWork('mcp-nested-writer','worker-a','running',(),(workspace('/synthetic/mcp-nested',1748),),checkpoint().evidence)
            async def check():
                command=f.command('resolve_store_hold',dict(backup_cutoff=f.service.read_service_info().restore_cutoff,inventory_complete=True,absent_work_inventory=(work,),resource_resolutions=(),reason='typed complete local observation'),receipt=owner)
                result=await server.call_tool('resolve_store_hold',{'command':plans.to_jsonable(command)})
                self.assertFalse(result.is_error,result.structured_content)
                self.assertFalse(f.service.read_service_info().store_hold)
                tools={tool.name:tool for tool in await server.list_tools()}
                schema=json.dumps(tools['resolve_store_hold'].input_schema)
                for field in ('absent_work_inventory','resource_claims','evidence','resource_resolutions','absent_work_id','resolution'):
                    self.assertIn(field,schema)
                command=f.command('resolve_store_hold',dict(backup_cutoff=f.service.read_service_info().restore_cutoff,inventory_complete=True,absent_work_inventory=(),resource_resolutions=(ResourceResolution(None,work.work_id,(),'stopped','typed whole writer stopped'),),reason='typed stop observation'),receipt=owner)
                result=await server.call_tool('resolve_store_hold',{'command':plans.to_jsonable(command)})
                self.assertFalse(result.is_error,result.structured_content)
                self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations,())
            asyncio.run(check())

    def test_explicit_stopped_declaration_and_normal_unresolved_resolution_use_real_rows(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('stopped-with-proof','worker-a','stopped',(),(workspace('/synthetic/stopped-with-proof',1746),),checkpoint().evidence)
            self.inventory(f,owner,(work,),(ResourceResolution(None,work.work_id,(),'stopped','observed whole writer stopped'),))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT status FROM resource_uses WHERE absent_work_id=?',(work.work_id,)).fetchall()),[('released',)])
            rid=f.register(workspace('/synthetic/native-unresolved',1747),owner)
            f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=owner))
            use=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
            self.inventory(f,owner,resolutions=(ResourceResolution(use.use_id,None,use.resource_ids,'unresolved','owned writer continues'),))
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT status FROM resource_uses WHERE use_id=?',(use.use_id,)).fetchone()[0]),'active')

    def test_inventory_unresolved_observation_covers_known_writer_without_stopping_it(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('known-unresolved','worker-a','running',(),(workspace('/synthetic/known-unresolved',1742),),checkpoint().evidence)
            self.inventory(f,owner,(work,),complete=False)
            self.inventory(f,owner,resolutions=(ResourceResolution(None,work.work_id,(),'unresolved','writer remains physically unresolved'),))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertTrue(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations)

    def test_inventory_cannot_stop_unidentified_work_or_use_stopped_state_without_exact_resolution(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        for branch in ('unidentified-stop','stopped-state'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                work=HostWork('guarded-writer','worker-a','unknown' if branch=='unidentified-stop' else 'stopped',(),() if branch=='unidentified-stop' else (workspace('/synthetic/stopped-state',1743),),checkpoint().evidence)
                if branch=='unidentified-stop':
                    self.inventory(f,owner,(work,),complete=False)
                    args=dict(resolutions=(ResourceResolution(None,work.work_id,(),'stopped','identity cannot be inferred stopped'),))
                else:
                    args=dict(works=(work,))
                before=f.store.read(lambda db:tuple(db.iterdump()))
                with self.assertRaises(DomainError):
                    self.inventory(f,owner,**args)
                self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_inventory_rejects_duplicate_work_labels_and_ambiguous_claim_collapse(self):
        from adaptive_planning.types import HostWork
        for branch in ('work-label','claim-collapse'):
            with self.subTest(branch=branch),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                claim=workspace('/synthetic/duplicate-work',1744)
                work=HostWork('duplicate-work','worker-a','running',(),(claim,claim) if branch=='claim-collapse' else (claim,),checkpoint().evidence)
                before=f.store.read(lambda db:tuple(db.iterdump()))
                with self.assertRaises(DomainError):
                    self.inventory(f,owner,(work,work) if branch=='work-label' else (work,))
                self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_identity_attachment_can_be_redeclared_without_new_physical_uses(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            self.inventory(f,owner,(HostWork('attach-replay','worker-a','unknown',(),(),checkpoint().evidence),),complete=False)
            work=HostWork('attach-replay','worker-a','running',(),(workspace('/synthetic/attach-replay',1745),),checkpoint().evidence)
            self.inventory(f,owner,(work,))
            rows=f.store.read(lambda db:db.execute('SELECT rowid,* FROM resource_uses').fetchall())
            self.inventory(f,owner,(work,))
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT rowid,* FROM resource_uses').fetchall()),rows)

    def test_full_resolution_without_redeclaration_covers_known_inventory_obligation(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('known-resolution-only','worker-a','running',(),(workspace('/synthetic/full-stop-only',1739),),checkpoint().evidence)
            self.inventory(f,owner,(work,),complete=False)
            self.inventory(f,owner,resolutions=(ResourceResolution(None,work.work_id,(),'stopped','explicit whole absent writer stopped'),))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations,())

    def test_duplicate_inventory_stop_ids_refuse_without_mutation(self):
        from adaptive_planning.types import HostWork,ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('known-duplicate-stop','worker-a','running',(),(workspace('/synthetic/duplicate-stop',1740),),checkpoint().evidence)
            self.inventory(f,owner,(work,))
            rid=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0].resource_ids[0]
            before=f.store.read(lambda db:tuple(db.iterdump()))
            with self.assertRaises(DomainError):
                self.inventory(f,owner,(work,),(ResourceResolution(None,work.work_id,(rid,rid),'stopped','duplicate selectors rejected'),))
            self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_inventory_owned_normal_use_composes_its_existing_exact_release_path(self):
        from adaptive_planning.types import ResourceResolution
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            self.inventory(f,owner)
            rid=f.register(workspace('/synthetic/native-inventory-stop',1741),owner)
            f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=owner))
            use=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
            self.inventory(f,owner,resolutions=(ResourceResolution(use.use_id,None,use.resource_ids,'stopped','explicit own native writer stopped'),))
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT status FROM resource_uses WHERE use_id=?',(use.use_id,)).fetchone()[0]),'released')

    def test_complete_unidentified_running_or_unknown_work_retains_a_source_owned_hold(self):
        from adaptive_planning.types import HostWork,ResourceClaim
        for state,claims in (('running',()),('unknown',()),('unknown',(ResourceClaim('synthetic-host','workspace',None,None,'/synthetic/opaque-writer',False,None,None),))):
            with self.subTest(state=state,claims=bool(claims)),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                self.inventory(f,owner,(HostWork('unidentified-writer','worker-a',state,(),claims,checkpoint().evidence),))
                self.assertTrue(f.service.read_service_info().store_hold,'Completeness incorrectly proved writer identity')
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT count(*) FROM store_hold_reasons WHERE kind='absent-identity' AND resolved=0").fetchone()[0]),1)

    def test_complete_inventory_cannot_omit_a_previous_known_running_declaration(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('known-retained','worker-a','running',(),(workspace('/synthetic/omitted-writer',1731),),checkpoint().evidence)
            self.inventory(f,owner,(work,),complete=False)
            before=f.store.read(lambda db:tuple(db.iterdump()))
            with self.assertRaises(DomainError) as error:
                self.inventory(f,owner)
            self.assertEqual(error.exception.code,'held')
            self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)

    def test_same_known_inventory_declaration_is_idempotent_and_preserves_physical_rows(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('known-repeated','worker-a','running',(),(workspace('/synthetic/repeated-writer',1732),),checkpoint().evidence)
            self.inventory(f,owner,(work,),complete=False)
            rows=f.store.read(lambda db:db.execute('SELECT rowid,* FROM resource_uses').fetchall())
            self.inventory(f,owner,(work,))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT rowid,* FROM resource_uses').fetchall()),rows)

    def test_inventory_discharges_proven_restore_ancestry_but_preserves_unexplained_flag(self):
        for inherited in ('restore-inventory','unexplained-flag'):
            with self.subTest(inherited=inherited),TemporaryDirectory() as name:
                directory=Path(name)
                if inherited=='restore-inventory':
                    f,owner=self.restored_empty(directory)
                    admin.backup_database(f.store,directory/'second.sqlite')
                    f.cleanup()
                    source=directory/'second.sqlite'
                else:
                    with make_service(directory) as f:
                        f.store.write(lambda db:db.execute('UPDATE installation SET store_hold=1'))
                        admin.backup_database(f.store,directory/'source.sqlite')
                    source=directory/'source.sqlite'
                admin.restore_database(Config(directory),source,preserve_current=directory/'preserved-again.sqlite')
                with make_service(directory) as f:
                    owner=f.create(creation_id='second-inspection')
                    self.inventory(f,owner)
                    self.assertEqual(f.service.read_service_info().store_hold,inherited=='unexplained-flag')

    def test_known_inventory_domain_route_clears_only_restore_hold_and_keeps_writer_conflict(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            work=HostWork('real-observed-worker','worker-a','running',(),(workspace('/synthetic/known-inventory',1721),),checkpoint().evidence)
            try:
                self.inventory(f,owner,(work,))
            except DomainError as error:
                self.fail(f'Actual known inventory route refused its declared work: {error.code}')
            self.assertFalse(f.service.read_service_info().store_hold)
            orphan=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
            self.assertEqual(orphan.absent_work_id,work.work_id)
            self.assertTrue(orphan.unresolved)
            rid=f.register(work.resource_claims[0],owner)
            with self.assertRaises(DomainError) as error:
                f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=owner))
            self.assertEqual(error.exception.code,'resource_conflict')

    def test_unresolved_historical_reason_holds_service_projection_and_assignment(self):
        for consumer in ('service','plan','assignment'):
            with self.subTest(consumer=consumer),make_service() as f:
                p=f.create()
                f.store.write(lambda db:db.execute("INSERT INTO store_hold_reasons VALUES('independent-history','previous-epoch','material','{}',0,'2026-10-06T00:00:00Z')"))
                if consumer=='service':
                    self.assertTrue(f.service.read_service_info().store_hold,'Unresolved reason vanished across epoch rotation')
                elif consumer=='plan':
                    self.assertTrue(f.service.read_plan(p.current_metadata.plan_id).coordination.store_hold)
                else:
                    with self.assertRaises(DomainError) as error:
                        f.assign(p)
                    self.assertEqual(error.exception.code,'held')

    def test_historical_hold_refuses_writable_verifier_but_retains_immutable_review(self):
        from adaptive_planning.types import Review
        with make_service() as f:
            p=f.create(steps=(step('a',review_required=True),))
            value=checkpoint()
            a=f.assign(p)
            active=f.start(p,a)
            f.submit(p,active,value)
            review=Review('reviewer',value.artifact,'passed',(),value.evidence,None)
            f.service.execute(f.command('record_review',dict(step_id='a',review=review),receipt=p))
            f.accept(p,active,value)
            state=f.service.read_plan(p.current_metadata.plan_id)
            f.service.execute(f.command('invalidate_result',dict(step_id='a',result_version=state.coordination.result_versions['a'],reason='synthetic held validation',evidence=value.evidence),receipt=p))
            rid=f.register(workspace('/synthetic/free-held-verifier',1719),p)
            f.store.write(lambda db:db.execute("INSERT INTO store_hold_reasons VALUES('independent-verifier','previous-epoch','material','{}',0,'2026-10-06T00:00:00Z')"))
            before=f.service.read_plan(p.current_metadata.plan_id)
            events=f.service.read_events(p.current_metadata.plan_id)
            with self.assertRaises(DomainError) as error:
                f.service.execute(f.command('reserve_verification',dict(step_id='a',verifier='reviewer',context=context(rid),handoff_from=None,stopped_reason=None),receipt=p))
            self.assertEqual(error.exception.code,'held')
            self.assertEqual(f.service.read_plan(p.current_metadata.plan_id).metadata,before.metadata)
            self.assertEqual(f.service.read_events(p.current_metadata.plan_id),events)
            f.service.execute(f.command('record_review',dict(step_id='a',review=review),receipt=p))

    def seed_mixed_work(self,f,owner):
        """Synthetic immutable producer rows for projection/release boundary tests."""
        from adaptive_planning.types import ResourceClaim
        from adaptive_planning import plans
        repo=f.register(ResourceClaim('synthetic-host','repository',11,1711,'/synthetic/orphan-repo',False,None,None),owner)
        child=f.register(ResourceClaim('synthetic-host','workspace',11,1712,'/synthetic/orphan-repo/child',False,repo,None),owner)
        external=f.register(ResourceClaim('synthetic-host','external',None,None,None,False,None,'synthetic-mixed-service'),owner)
        ids=(repo,child,external)
        def insert(db):
            epoch,host=db.execute('SELECT epoch,host_id FROM installation').fetchone()
            restore=db.execute('SELECT restore_id FROM restore_origins WHERE new_epoch=?',(epoch,)).fetchone()[0]
            work='synthetic-mixed-work'
            producer='synthetic-inventory-producer'
            created=f.clock().isoformat()
            uses=[dict(use_id='synthetic-member-'+str(index),resource_id=rid,scope=scope,created_at=created) for index,(rid,scope) in enumerate(zip(ids,('repository','workspace','external')))]
            db.execute("INSERT INTO absent_host_work(absent_work_id,host_id,resource_id,scope,declared_identity,status,created_at,anchor_use_id,producer_id) VALUES(?,?,?,'repository','worker-a','unresolved',?,?,?)",(work,host,repo,created,uses[0]['use_id'],producer))
            for member in uses:
                db.execute('INSERT INTO absent_host_work_members VALUES(?,?,?)',(work,member['resource_id'],member['scope']))
                db.execute("INSERT INTO resource_uses(use_id,resource_id,absent_work_id,scope,status,created_at) VALUES(?,?,?,?,'unresolved',?)",(member['use_id'],member['resource_id'],work,member['scope'],created))
            record=dict(format=1,producer='inventory',restore_id=restore,epoch=epoch,work=dict(work_id=work,actor='worker-a',state='running',resource_ids=list(ids),resource_claims=[],evidence=plans.to_jsonable(checkpoint().evidence)),members=uses,identity_reason_id=None)
            raw=json.dumps(record,sort_keys=True)
            db.execute('INSERT INTO host_work_declarations VALUES(?,?,?,?,?,?,?)',(producer,work,'inventory','worker-a','running',raw,created))
            event=dict(producer_id=producer,absent_work_id=work,actor='worker-a',record_digest=hashlib.sha256(raw.encode()).hexdigest())
            db.execute("INSERT INTO events(kind,actor,published_at,payload) VALUES('host_work_declared','local-administrator',?,?)",(created,json.dumps(event,sort_keys=True)))
            return uses[0]['use_id']
        return ids,f.store.write(insert)

    def test_anchored_heterogeneous_orphan_projection_and_full_release(self):
        with TemporaryDirectory() as name:
            f,owner=self.restored_empty(Path(name))
            ids,anchor=self.seed_mixed_work(f,owner)
            orphan=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations
            self.assertEqual(len(orphan),1,'One work was projected as unrelated physical reservations')
            self.assertEqual(orphan[0].use_id,anchor)
            self.assertEqual(set(orphan[0].resource_ids),set(ids))
            f.service.execute(f.command('release_resource_use',dict(use_id=anchor,resource_ids=ids,reason='explicit full cohort stopped',stopped=True),receipt=owner))
            self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations,())
            self.assertEqual(f.store.read(lambda db:db.execute("SELECT count(*) FROM resource_uses WHERE absent_work_id='synthetic-mixed-work'").fetchone()[0]),3)

    def test_orphan_projection_refuses_missing_or_foreign_producer_members(self):
        for mutation in ('declaration','host','member'):
            with self.subTest(mutation=mutation),TemporaryDirectory() as name:
                f,owner=self.restored_empty(Path(name))
                ids,anchor=self.seed_mixed_work(f,owner)
                def corrupt(db):
                    if mutation=='declaration': db.execute('DELETE FROM host_work_declarations')
                    elif mutation=='host': db.execute("UPDATE absent_host_work SET host_id='foreign'")
                    else:
                        db.execute('PRAGMA defer_foreign_keys=ON')
                        db.execute("UPDATE host_work_declarations SET payload='{}'")
                f.store.write(corrupt)
                with self.assertRaises(DomainError):
                    f.service.read_plan(owner.current_metadata.plan_id)

    def test_restored_grant_history_must_remain_present_for_recovery(self):
        from dataclasses import replace
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create()
                a=f.assign(p)
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                f.store.write(lambda db:db.execute("DELETE FROM capabilities WHERE role='attempt'"))
                attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                command=f.command('resolve_attempt',dict(resolution='release',reason='missing source grant',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                with self.assertRaises(DomainError):
                    f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))

    def test_restored_swept_native_discharges_only_owned_sweep_and_restore_holds(self):
        from dataclasses import replace
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create()
                a=f.assign(p)
                f.clock.advance(901)
                f.service.execute(f.command('sweep_stale',{},receipt=p))
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                command=f.command('resolve_attempt',dict(resolution='release',reason='known swept restored owner ended',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                self.assertFalse(any(h.kind=='stale' for h in f.service.read_plan(p.current_metadata.plan_id).coordination.holds))

    def test_restored_unknown_import_attachment_discharges_its_original_reason_epoch(self):
        from dataclasses import replace
        from adaptive_planning.types import ExecutionContextClaims
        with TemporaryDirectory() as name:
            directory=Path(name)
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',directory/'planning.sqlite')
            admin.migrate_installation(Config(directory),directory/'backups')
            with make_service(directory) as f:
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                pid,aid,reason,epoch=f.store.read(lambda db:db.execute("SELECT json_extract(details,'$.plan_id'),json_extract(details,'$.attempt_id'),reason_id,epoch FROM store_hold_reasons WHERE kind='identity' AND resolved=0").fetchone())
                owner=self.takeover(f,pid)
                attempt=f.service.read_plan(pid).coordination.attempts[0]
                claims=ExecutionContextClaims('non-repository',None,workspace('/synthetic/restored-import-attached',1701),False,(),True,None)
                command=f.command('resolve_attempt',dict(resolution='attach-context',reason='explicit full historical identity',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=claims),receipt=owner)
                f.service.execute(replace(command,attempt_id=aid,attempt_version=attempt.version,definition_version=attempt.definition_version))
                self.assertEqual(f.store.read(lambda db:db.execute('SELECT epoch,resolved FROM store_hold_reasons WHERE reason_id=?',(reason,)).fetchone()),(epoch,1))

    def test_restored_full_attempt_row_tampering_cannot_authorize_terminal_recovery(self):
        from dataclasses import replace
        for field,value in (('assignee','forged-worker'),('started_at','2026-10-01T00:00:00Z'),('stopped_at','2026-10-01T00:00:00Z')):
            with self.subTest(field=field),TemporaryDirectory() as name:
                directory=Path(name)
                with make_service(directory) as f:
                    p=f.create()
                    a=f.assign(p)
                    admin.backup_database(f.store,directory/'source.sqlite')
                admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
                with make_service(directory) as f:
                    owner=self.takeover(f,p.current_metadata.plan_id)
                    f.store.write(lambda db:db.execute(f'UPDATE attempts SET {field}=? WHERE attempt_id=?',(value,a.committed['attempt_id'])))
                    attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                    command=f.command('resolve_attempt',dict(resolution='release',reason='refuse altered constructor row',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                    with self.assertRaises(DomainError):
                        f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))

    def test_restore_retains_imported_constructor_state_payload_and_owned_holds(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',directory/'planning.sqlite')
            admin.migrate_installation(Config(directory),directory/'backups')
            with make_service(directory) as f:
                imported=f.store.read(lambda db:db.execute("SELECT plan_id,attempt_id,state,version,payload FROM attempts WHERE assignee LIKE 'legacy-unresolved:%' ORDER BY attempt_id").fetchall())
                holds=f.store.read(lambda db:db.execute("SELECT * FROM holds WHERE plan_id IN (SELECT plan_id FROM imports) ORDER BY hold_id").fetchall())
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT plan_id,attempt_id,state,version,payload FROM attempts WHERE assignee LIKE 'legacy-unresolved:%' ORDER BY attempt_id").fetchall()),imported)
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT * FROM holds WHERE plan_id IN (SELECT plan_id FROM imports) ORDER BY hold_id").fetchall()),holds)

    def test_local_restore_cli_rotates_every_generation_and_retains_immutable_native_rows(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            with make_service(directory) as f:
                p=f.create(steps=(step('a'),),creation_id='first')
                second=f.create(creation_id='second')
                assigned=f.assign(p)
                aid=assigned.committed['attempt_id']
                old=f.store.read(lambda db:db.execute('SELECT * FROM attempts WHERE attempt_id=?',(aid,)).fetchone())
                generations=f.store.read(lambda db:db.execute('SELECT plan_id,generation FROM plans ORDER BY plan_id').fetchall())
                admin.backup_database(f.store,directory/'source.sqlite')
                epoch=f.epoch
            source=(directory/'source.sqlite').read_bytes()
            result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin','restore','--data-dir',str(directory),'--source',str(directory/'source.sqlite'),'--preserve-current',str(directory/'preserved.sqlite')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            with closing(sqlite3.connect(directory/'planning.sqlite')) as db:
                self.assertNotEqual(db.execute('SELECT epoch FROM installation').fetchone()[0],epoch,result.stdout.decode())
                self.assertTrue(db.execute('SELECT store_hold FROM installation').fetchone()[0])
                self.assertTrue(all(db.execute('SELECT generation FROM plans WHERE plan_id=?',(pid,)).fetchone()[0]!=generation for pid,generation in generations))
                row=db.execute('SELECT * FROM attempts WHERE attempt_id=?',(aid,)).fetchone()
                self.assertEqual(row[4:6],('stale',old[5]+1))
                self.assertEqual(json.loads(row[-1])['definition'],json.loads(old[-1])['definition'])
                self.assertEqual(json.loads(row[-1])['epoch'],epoch)
                self.assertEqual(db.execute('SELECT count(*) FROM capabilities WHERE revoked=0').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM restore_attempt_origins WHERE attempt_id=?',(aid,)).fetchone()[0],1)
            self.assertEqual((directory/'source.sqlite').read_bytes(),source)
            self.assertTrue((directory/'preserved.sqlite').is_file())

    def test_v1_orphan_semantic_refusals_retain_prebackup_and_original(self):
        for mutation in ('resource-less','actor','wrong-host','mismatched-member'):
            with self.subTest(mutation=mutation),TemporaryDirectory() as name:
                directory=Path(name)
                target=directory/'planning.sqlite'
                shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
                with closing(sqlite3.connect(target)) as db,db:
                    if mutation=='resource-less': db.execute('UPDATE absent_host_work SET resource_id=NULL')
                    elif mutation=='actor': db.execute("UPDATE absent_host_work SET declared_identity='' ")
                    elif mutation=='wrong-host': db.execute("UPDATE absent_host_work SET host_id='foreign'")
                    else:
                        rid=db.execute("SELECT resource_id FROM resources WHERE kind='workspace' AND resource_id != (SELECT resource_id FROM absent_host_work) LIMIT 1").fetchone()[0]
                        db.execute('UPDATE resource_uses SET resource_id=? WHERE absent_work_id IS NOT NULL',(rid,))
                before=file_set(directory)
                try:
                    admin.migrate_installation(Config(directory),directory/'backups')
                except Exception as error:
                    self.assertIsInstance(error,DomainError)
                    self.assertEqual(error.code,'invalid')
                else:
                    self.fail('Unprovable legacy orphan topology was published')
                self.assertEqual(file_set(directory),before)
                self.assertEqual(len(list((directory/'backups').glob('*.sqlite'))),1)

    def test_local_migration_cli_changes_real_old_schema_and_retains_history(self):
        with TemporaryDirectory() as name:
            directory=Path(name)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            with closing(sqlite3.connect(target)) as db:
                epoch=db.execute('SELECT epoch FROM installation').fetchone()[0]
                rows=db.execute('SELECT rowid,* FROM resource_uses ORDER BY rowid').fetchall()
            result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin','migrate','--data-dir',str(directory),'--backup-directory',str(directory/'backups')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT schema_version FROM installation').fetchone()[0],2,result.stdout.decode())
                self.assertEqual(db.execute('SELECT epoch FROM installation').fetchone()[0],epoch)
                self.assertEqual(db.execute('SELECT rowid,* FROM resource_uses ORDER BY rowid').fetchall(),rows)
                self.assertEqual(db.execute('SELECT count(*) FROM absent_host_work_members').fetchone()[0],1)
            self.assertEqual(len(list((directory/'backups').glob('*.sqlite'))),1)

    def test_backup_deadline_refuses_without_publishing_partial_image(self):
        with make_service() as f:
            target=f.directory/'expired.sqlite'
            with patch('adaptive_planning.admin.BACKUP_SECONDS',0,create=True):
                with self.assertRaises(DomainError) as error:
                    admin.backup_database(f.store,target)
            self.assertEqual(error.exception.code,'busy')
            self.assertFalse(target.exists())

    def test_completed_backup_retained_when_publication_durability_is_uncertain(self):
        with make_service() as f:
            target=f.directory/'uncertain.sqlite'
            with patch('adaptive_planning.admin._fsync_directory',side_effect=OSError('synthetic directory sync failure'),create=True):
                with self.assertRaises(DomainError) as error:
                    admin.backup_database(f.store,target)
            self.assertEqual(error.exception.code,'durability_uncertain')
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(),('ok',))
                self.assertEqual(db.execute('SELECT authority_kind FROM installation').fetchone(),('backup',))

    def test_backup_and_export_destinations_refuse_aliases_and_collisions(self):
        with make_service() as f:
            p=f.create()
            retained=f.directory/'retained'
            retained.write_bytes(b'existing administrator artifact')
            linked=f.directory/'linked'
            linked.symlink_to(f.store.path)
            for function in (admin.backup_database,admin.export_database):
                for destination in (retained,linked,f.store.path,f.directory/'service-token',f.directory/'.planning-operation.lock',f.directory/'.planning-maintenance.json',Path(str(f.store.path)+'-wal')):
                    with self.subTest(function=function.__name__,destination=destination.name):
                        before=file_set(f.directory)
                        with self.assertRaises(DomainError):
                            function(f.store,destination)
                        self.assertEqual(file_set(f.directory),before)

    def test_local_export_cli_creates_one_public_snapshot(self):
        with make_service() as f:
            p=f.create()
            destination=f.directory/'public-only.json'
            result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin','export','--data-dir',str(f.directory),'--destination',str(destination)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode,0,result.stdout.decode())
            self.assertTrue(destination.is_file(),'Public export command created no snapshot')
            value=json.loads(destination.read_text())
            self.assertEqual(value['plans'][0]['metadata']['plan_id'],p.current_metadata.plan_id)
            self.assertNotIn(p.private.orchestrator_token,destination.read_text())

    def test_close_drains_active_operation_and_callback_before_releasing_lease(self):
        import fcntl
        for callback in (False, True):
            with self.subTest(callback=callback), make_service() as f:
                entered=threading.Event()
                release=threading.Event()
                closed=threading.Event()
                def wait(*args):
                    entered.set()
                    release.wait(5)
                if callback:
                    f.store.add_listener(wait)
                    action=lambda:f.store.write(lambda db:db.execute("INSERT INTO events(kind,published_at) VALUES('drain','2026-10-06T00:00:00Z')"))
                else:
                    action=lambda:f.store.read(wait)
                worker=threading.Thread(target=action)
                worker.start()
                self.assertTrue(entered.wait(5))
                closer=threading.Thread(target=lambda:(f.store.close(),closed.set()))
                closer.start()
                descriptor=os.open(f.directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
                try:
                    self.assertFalse(closed.wait(.05),'Lifetime released before owned work drained')
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                    release.set()
                    worker.join(5)
                    closer.join(5)
                self.assertTrue(closed.is_set())
                f.store.close()
                with self.assertRaises(DomainError) as error:
                    f.store.read(lambda db:1)
                self.assertEqual(error.exception.code,'closed')

    def test_reentrant_close_refuses_without_deadlock_or_releasing_lifetime(self):
        with make_service() as f:
            with self.assertRaises(DomainError) as error:
                f.store.read(lambda db:f.store.close())
            self.assertEqual(error.exception.code,'busy')
            self.assertEqual(f.store.read(lambda db:1),1)

    def test_store_lifetime_blocks_independent_directory_exclusive_lock(self):
        import fcntl
        with make_service() as f:
            descriptor=os.open(f.directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                os.close(descriptor)

    def test_read_and_write_refuse_a_busy_operation_gate(self):
        import fcntl
        with make_service() as f:
            gate=f.directory/'.planning-operation.lock'
            descriptor=os.open(gate,os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                for call in (lambda:f.store.read(lambda db:1),lambda:f.store.write(lambda db:1)):
                    with self.assertRaises(DomainError) as error:
                        call()
                    self.assertEqual(error.exception.code,'busy')
            finally:
                os.close(descriptor)

    def test_store_birth_process_is_fenced_before_opening_sqlite(self):
        with make_service() as f:
            with patch('adaptive_planning.store.os.getpid',return_value=os.getpid()+100000):
                with self.assertRaises(DomainError) as error:
                    f.store.read(lambda db:1)
                self.assertEqual(error.exception.code,'fenced')

    def test_pending_malformed_and_foreign_admission_are_untouched(self):
        for mutation in ('pending', 'malformed', 'host'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as name:
                directory=Path(name)
                with make_service(directory) as f:
                    target=f.store.path
                    if mutation == 'malformed':
                        f.store.write(lambda db:db.execute("UPDATE installation SET epoch=''"))
                    if mutation == 'pending':
                        (directory/'.planning-maintenance.json').write_text('{}')
                os.chmod(target,0o640)
                before=file_set(directory)
                with self.assertRaises(DomainError):
                    Store(target,FakeClock(),host_id='foreign' if mutation=='host' else None)
                self.assertEqual(file_set(directory),before)

    def test_restore_owned_hold_list_cannot_claim_an_independent_hold(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            with make_service(directory) as f:
                p=f.create(steps=(step('a'),))
                a=f.assign(p)
                admin.backup_database(f.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                owner=self.takeover(f,p.current_metadata.plan_id)
                independent=json.dumps(dict(kind='stale',step_id='a',source_id='independent-observation',reason='independent hold retained'),sort_keys=True)
                def corrupt(db):
                    db.execute('INSERT INTO holds VALUES(?,?,?,1,?)',('independent-hold',p.current_metadata.plan_id,independent,f.clock().isoformat()))
                    identity,raw=db.execute('SELECT restore_id,payload FROM restore_attempt_origins WHERE attempt_id=?',(a.committed['attempt_id'],)).fetchone()
                    record=json.loads(raw)
                    record['source']['owned_holds'].append(['independent-hold',independent])
                    db.execute('UPDATE restore_attempt_origins SET payload=? WHERE restore_id=?',(json.dumps(record,sort_keys=True),identity))
                f.store.write(corrupt)
                attempt=f.service.read_plan(p.current_metadata.plan_id).coordination.attempts[0]
                command=f.command('resolve_attempt',dict(resolution='release',reason='refuse falsely claimed hold',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                with self.assertRaises(DomainError):
                    f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                self.assertEqual(f.store.read(lambda db:db.execute("SELECT active FROM holds WHERE hold_id='independent-hold'").fetchone()[0]),1)

    def test_service_constructor_failure_does_not_leave_an_unowned_store(self):
        from adaptive_planning.app import build_app
        made=[]
        def construct(*args,**kwargs):
            store=Store(*args,**kwargs)
            made.append(store)
            return store
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            admin.initialize_installation(Config(directory))
            with patch('adaptive_planning.app.Store',side_effect=construct),patch('adaptive_planning.app.PlanningService',side_effect=RuntimeError('synthetic service constructor failure')):
                with self.assertRaises(RuntimeError):
                    build_app(Config(directory))
            self.assertTrue(made[0]._closed,'Service constructor failed after acquiring an unowned Store')

    def test_native_backup_without_retained_grant_refuses_restore_without_import_fallback(self):
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            with make_service(directory) as f:
                p=f.create(steps=(step('a'),))
                f.assign(p)
                admin.backup_database(f.store,directory/'source.sqlite')
            with closing(sqlite3.connect(directory/'source.sqlite')) as db,db:
                db.execute("DELETE FROM capabilities WHERE role='attempt'")
            before=file_set(directory)
            with self.assertRaises(DomainError):
                admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            after=file_set(directory)
            for name,value in before.items(): self.assertEqual(after[name],value,name)
            self.assertTrue((directory/'preserved.sqlite').exists())
            self.assertFalse((directory/'.planning-maintenance.json').exists())

    def test_new_epoch_creation_is_separate_and_old_creation_history_is_retained(self):
        from tests.service_fixtures import draft
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            with make_service(directory) as original:
                payload=dict(creation_id='same-id',draft=draft(steps=(step('a'),)),new_plan_intent='synthetic explicit intent')
                old_command=original.command('create_plan',payload,key='old-creation-key')
                old=original.service.execute(old_command)
                old_history=original.store.read(lambda db:db.execute('SELECT rowid,* FROM creation_ids').fetchall())
                admin.backup_database(original.store,directory/'source.sqlite')
            admin.restore_database(Config(directory),directory/'source.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as restored:
                with self.assertRaises(DomainError) as error:
                    restored.service.execute(old_command)
                self.assertEqual(error.exception.code,'stale_epoch')
                new=restored.service.execute(restored.command('create_plan',payload,key='new-creation-key'))
                self.assertNotEqual(new.current_metadata.plan_id,old.current_metadata.plan_id)
                history=restored.store.read(lambda db:db.execute('SELECT rowid,* FROM creation_ids ORDER BY rowid').fetchall())
                self.assertEqual(history[:len(old_history)],old_history)
                self.assertEqual(len(history),2)

    def test_direct_orphan_release_cannot_bypass_unknown_inventory_identity(self):
        from adaptive_planning.types import HostWork,ResourceClaim
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f,owner=self.restored_empty(directory)
            try:
                opaque=ResourceClaim('synthetic-host','workspace',None,None,'/synthetic/unknown-path-only',False,None,None)
                self.inventory(f,owner,(HostWork('unknown-path-worker','worker-a','unknown',(),(opaque,),checkpoint().evidence),))
                orphan=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
                with self.assertRaises(DomainError) as error:
                    f.service.execute(f.command('release_resource_use',dict(use_id=orphan.use_id,resource_ids=orphan.resource_ids,reason='identity is still unknown',stopped=True),receipt=owner))
                self.assertEqual(error.exception.code,'held')
                self.assertTrue(f.service.read_service_info().store_hold)
            finally:
                f.cleanup()

    def test_backup_uses_completed_copy_metadata_and_retains_valid_uncertain_publication(self):
        with make_service() as f:
            f.create()
            actual=f.store._connect
            class InterleavingSource:
                def __init__(self): self.connection=actual()
                def backup(self,*args,**kwargs):
                    f.store.write(lambda db:db.execute("INSERT INTO events(kind,published_at) VALUES('copy-interleaved','2026-10-06T00:00:00Z')"))
                    return self.connection.backup(*args,**kwargs)
                def close(self): self.connection.close()
            destination=f.directory/'coherent.sqlite'
            # One committed writer lands after the source connection opens.
            with patch.object(f.store,'_connect',side_effect=InterleavingSource):
                # The writer needs the actual Store connection, avoiding proxy recursion.
                original_write=f.store.write
                def write(operation):
                    with patch.object(f.store,'_connect',side_effect=actual):
                        return original_write(operation)
                with patch.object(f.store,'write',side_effect=write):
                    metadata=admin.backup_database(f.store,destination)
            with closing(sqlite3.connect(destination)) as db:
                sequence=db.execute('SELECT max(sequence) FROM events').fetchone()[0]
                self.assertEqual(metadata['sequence'],sequence)
                self.assertIn(f':{sequence}:',metadata['restore_cutoff'])
                self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='copy-interleaved'").fetchone()[0],1)
            before=destination.read_bytes()
            with self.assertRaises(DomainError):
                admin.backup_database(f.store,destination)
            self.assertEqual(destination.read_bytes(),before)
            uncertain=f.directory/'uncertain.sqlite'
            with patch('adaptive_planning.admin._fsync_directory',side_effect=OSError('synthetic fsync failure')):
                with self.assertRaises(DomainError) as error:
                    admin.backup_database(f.store,uncertain)
            self.assertEqual(error.exception.code,'durability_uncertain')
            with closing(sqlite3.connect(uncertain)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('SELECT authority_kind FROM installation').fetchone()[0],'backup')
            expired=f.directory/'expired.sqlite'
            with patch('adaptive_planning.admin.BACKUP_SECONDS',0):
                with self.assertRaises(DomainError) as error:
                    admin.backup_database(f.store,expired)
            self.assertEqual(error.exception.code,'busy')
            self.assertFalse(expired.exists())
            self.assertFalse(tuple(f.directory.glob('.planning-backup-*')))

    def test_postfork_store_is_fenced_and_never_started_app_disposes_lifetime(self):
        from adaptive_planning.app import build_app
        from adaptive_planning.store import maintenance_ownership
        if not hasattr(os,'fork'):
            self.skipTest('POSIX fork required')
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            admin.initialize_installation(Config(directory))
            app=build_app(Config(directory))
            read_fd,write_fd=os.pipe()
            pid=os.fork()
            if pid==0:
                os.close(read_fd)
                try:
                    codes=[]
                    for call in (lambda:app.state.service._store.read(lambda db:1),app.state.close):
                        try: call()
                        except DomainError as error: codes.append(error.code)
                    os.write(write_fd,json.dumps(codes).encode())
                finally:
                    os._exit(0)
            os.close(write_fd)
            try:
                codes=json.loads(os.read(read_fd,1024))
            finally:
                os.close(read_fd)
                os.waitpid(pid,0)
            self.assertEqual(codes,['fenced','fenced'])
            with self.assertRaises(DomainError):
                with maintenance_ownership(directory): pass
            app.state.close()
            app.state.close()
            with maintenance_ownership(directory): pass
            with self.assertRaises(DomainError) as error:
                app.state.service._store.installation_epoch()
            self.assertEqual(error.exception.code,'closed')

    def test_newer_schema_committed_in_live_wal_refuses_without_touching_sidecars(self):
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            with make_service(directory) as f:
                target=f.store.path
            with closing(sqlite3.connect(target)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('UPDATE installation SET schema_version=999')
                db.commit()
                self.assertTrue(Path(str(target)+'-wal').stat().st_size)
                before=file_set(directory)
                with self.assertRaises(DomainError) as error:
                    Store(target,FakeClock())
                self.assertEqual(error.exception.code,'schema_newer')
                self.assertEqual(file_set(directory),before)

    def test_restored_native_retirement_rejection_and_replacement_keep_exact_holds(self):
        from dataclasses import replace
        for resolution,next_state in (('release','retired'),('reject','blocked'),('replace','pending')):
            with self.subTest(resolution=resolution),TemporaryDirectory() as directory:
                directory=Path(directory)
                with make_service(directory) as f:
                    p=f.create(steps=(step('a'),))
                    a=f.assign(p)
                    admin.backup_database(f.store,directory/'backup.sqlite')
                admin.restore_database(Config(directory),directory/'backup.sqlite',preserve_current=directory/'preserved.sqlite')
                with make_service(directory) as f:
                    owner=self.takeover(f,p.current_metadata.plan_id)
                    self.inventory(f,owner)
                    self.assertFalse(f.service.read_service_info().store_hold)
                    attempt=f.service.read_plan(owner.current_metadata.plan_id).coordination.attempts[0]
                    payload=dict(resolution=resolution,reason='synthetic terminal decision',next_step_state=next_state,stopped_resource_ids=(),replacement_assignee='worker-b' if resolution=='replace' else None,replacement_context=context(None,writes=False) if resolution=='replace' else None,attached_context=None)
                    command=f.command('resolve_attempt',payload,receipt=owner)
                    result=f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))
                    self.assertFalse(any(h.kind=='stale' for h in f.service.read_plan(owner.current_metadata.plan_id).coordination.holds))
                    self.assertEqual(f.store.read(lambda db:db.execute('SELECT state FROM attempts WHERE attempt_id=?',(a.committed['attempt_id'],)).fetchone()[0]),'rejected' if resolution=='reject' else 'released')
                    if resolution=='replace':
                        self.assertNotEqual(result.committed['attempt_id'],attempt.attempt_id)
                        self.assertIsNotNone(result.private.attempt_token)

    def takeover(self,f,plan_id):
        from adaptive_planning.types import MutationReceipt
        metadata=f.service.read_plan(plan_id).metadata
        receipt=MutationReceipt({},metadata,metadata,False,None)
        return f.service.execute(f.command('takeover',dict(expected_ownership_version=metadata.ownership_version,reason='synthetic current authority inspection'),receipt=receipt))

    def test_rollback_recovery_itself_can_resume_after_publication_crash(self):
        from adaptive_planning.store import MANIFEST_FILENAME
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            actual_replace=os.replace
            def crash_after(source,destination,*args,**kwargs):
                value=actual_replace(source,destination,*args,**kwargs)
                if Path(destination).resolve()==target.resolve():
                    raise OSError('synthetic publication crash')
                return value
            with patch('adaptive_planning.admin.os.replace',side_effect=crash_after):
                with self.assertRaises(OSError):
                    admin.migrate_installation(Config(directory),directory/'backups')
            operation=json.loads((directory/MANIFEST_FILENAME).read_text())['operation_id']
            # Crash while writing the durable rollback decision, before replace.
            actual_write=admin._manifest_write
            def crash_decision(*args,**kwargs):
                actual_write(*args,**kwargs)
                if args[1].get('resolution_decision')=='rollback':
                    raise OSError('synthetic rollback-decision crash')
            with patch('adaptive_planning.admin._manifest_write',side_effect=crash_decision):
                with self.assertRaises(OSError):
                    admin.recover_maintenance(Config(directory),operation_id=operation,decision='rollback')
            admin.recover_maintenance(Config(directory),operation_id=operation,decision='rollback')
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT schema_version FROM installation').fetchone()[0],1)
            self.assertFalse((directory/MANIFEST_FILENAME).exists())

    def test_genuine_imports_survive_restore_and_unknown_identity_resolves_original_epoch_only(self):
        from dataclasses import replace
        from adaptive_planning.types import ExecutionContextClaims
        provenance=json.loads((Path(__file__).parent/'fixtures/service_v1_provenance.json').read_text())
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',directory/'planning.sqlite')
            admin.migrate_installation(Config(directory),directory/'backups')
            with make_service(directory) as original:
                admin.backup_database(original.store,directory/'restore.sqlite')
            admin.restore_database(Config(directory),directory/'restore.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                old_identity=f.store.read(lambda db:db.execute("SELECT reason_id,epoch FROM store_hold_reasons WHERE kind='identity' AND resolved=0").fetchone())
                self.assertNotEqual(old_identity[1],f.epoch)
                inspection=f.create(creation_id='inspection')
                self.inventory(f,inspection)
                self.assertTrue(f.service.read_service_info().store_hold)
                for index,pid in enumerate(provenance['named_ids']['imported_plans']):
                    owner=self.takeover(f,pid)
                    attempt=f.service.read_plan(pid).coordination.attempts[0]
                    payload=dict(resolution='release',reason='synthetic imported recovery',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None)
                    command=f.command('resolve_attempt',payload,receipt=owner)
                    command=replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version)
                    if index==1:
                        with self.assertRaises(DomainError) as error:
                            f.service.execute(command)
                        self.assertEqual(error.exception.code,'held')
                        claim=ExecutionContextClaims('non-repository',None,workspace('/synthetic/attached-import',912),False,(),True,None)
                        attached=f.service.execute(replace(command,payload=dict(payload,resolution='attach-context',attached_context=claim)))
                        attempt=f.service.read_plan(pid).coordination.attempts[0]
                        command=replace(f.command('resolve_attempt',payload,receipt=attached),attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version)
                    f.service.execute(command)
                    latest=f.service.read_plan(pid)
                    self.assertFalse(any(h.kind in ('identity','stale') for h in latest.coordination.holds))
                    self.assertTrue(latest.coordination.has_unresolved_uses)
                self.assertEqual(f.store.read(lambda db:db.execute('SELECT epoch,resolved FROM store_hold_reasons WHERE reason_id=?',(old_identity[0],)).fetchone()),(old_identity[1],1))
                self.assertFalse(f.service.read_service_info().store_hold)

    def test_restored_import_cannot_fallback_when_its_current_restore_record_is_malformed(self):
        from dataclasses import replace
        provenance=json.loads((Path(__file__).parent/'fixtures/service_v1_provenance.json').read_text())
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',directory/'planning.sqlite')
            admin.migrate_installation(Config(directory),directory/'backups')
            with make_service(directory) as original:
                admin.backup_database(original.store,directory/'restore.sqlite')
            admin.restore_database(Config(directory),directory/'restore.sqlite',preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as f:
                pid=provenance['named_ids']['imported_plans'][0]
                owner=self.takeover(f,pid)
                attempt=f.service.read_plan(pid).coordination.attempts[0]
                f.store.write(lambda db:db.execute("UPDATE restore_attempt_origins SET payload='{}' WHERE plan_id=?",(pid,)))
                command=f.command('resolve_attempt',dict(resolution='release',reason='refuse unproved current restore',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                with self.assertRaises(DomainError):
                    f.service.execute(replace(command,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))

    def test_repeated_restore_refuses_a_tampered_ancestor_origin(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f=make_service(directory)
            p=f.create()
            a=f.assign(p)
            first=directory/'first.sqlite'
            admin.backup_database(f.store,first)
            f.cleanup()
            first_result=admin.restore_database(Config(directory),first,preserve_current=directory/'preserved-first.sqlite')
            one=make_service(directory)
            second=directory/'second.sqlite'
            admin.backup_database(one.store,second)
            one.cleanup()
            admin.restore_database(Config(directory),second,preserve_current=directory/'preserved-second.sqlite')
            with make_service(directory) as two:
                state=two.service.read_plan(p.current_metadata.plan_id)
                command=two.command('takeover',dict(expected_ownership_version=state.metadata.ownership_version,reason='inspect repeated restore'),receipt=p)
                owner=two.service.execute(replace(command,epoch=two.epoch,generation=state.metadata.generation))
                two.store.write(lambda db:db.execute("UPDATE restore_attempt_origins SET payload='{}' WHERE restore_id=?",(first_result['restore_id'],)))
                attempt=state.coordination.attempts[0]
                logical=two.command('resolve_attempt',dict(resolution='release',reason='reject malformed origin',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                with self.assertRaises(DomainError):
                    two.service.execute(replace(logical,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version))

    def test_export_unknown_and_malformed_known_payloads_never_forward_private_objects(self):
        with make_service() as f:
            p=f.create()
            secret='synthetic-private-unknown-object-secret'
            f.store.write(lambda db:db.execute("INSERT INTO events(kind,actor,published_at,payload) VALUES('schema_migrated','local-administrator','2026-10-06T00:00:00Z',?)",(json.dumps(dict(from_schema=dict(private_grants=secret),to_schema=2)),)))
            export=f.directory/'public.json'
            admin.export_database(f.store,export)
            self.assertNotIn(secret,export.read_text())

    def restored_empty(self,directory):
        f=make_service(directory)
        backup=directory/'backup.sqlite'
        admin.backup_database(f.store,backup)
        f.store.close()
        admin.restore_database(Config(directory),backup,preserve_current=directory/'preserved.sqlite')
        restored=make_service(directory)
        self.addCleanup(restored.cleanup)
        return restored,restored.create(creation_id='inspection')

    def inventory(self,f,owner,works=(),resolutions=(),complete=True):
        return f.service.execute(f.command('resolve_store_hold',dict(backup_cutoff=f.service.read_service_info().restore_cutoff,inventory_complete=complete,absent_work_inventory=works,resource_resolutions=resolutions,reason='synthetic explicit complete observation'),receipt=owner))

    def test_unknown_work_requires_same_identity_attachment_and_omissions_cannot_clear(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f,owner=self.restored_empty(directory)
            unknown=HostWork('actual-unknown','worker-a','unknown',(),(),checkpoint().evidence)
            self.inventory(f,owner,(unknown,),complete=False)
            self.assertTrue(f.service.read_service_info().store_hold)
            before=f.store.read(lambda db:tuple(db.iterdump()))
            with self.assertRaises(DomainError) as error:
                self.inventory(f,owner)
            self.assertEqual(error.exception.code,'held')
            self.assertEqual(f.store.read(lambda db:tuple(db.iterdump())),before)
            wrong=HostWork('actual-unknown','other-actor','running',(),(workspace('/synthetic/identified',802),),checkpoint().evidence)
            with self.assertRaises(DomainError):
                self.inventory(f,owner,(wrong,))
            known=HostWork('actual-unknown','worker-a','running',(),(workspace('/synthetic/identified',802),),checkpoint().evidence)
            self.inventory(f,owner,(known,))
            self.assertFalse(f.service.read_service_info().store_hold)
            self.assertEqual(f.store.read(lambda db:db.execute("SELECT resolved FROM store_hold_reasons WHERE kind='absent-identity'").fetchone()[0]),1)
            self.assertTrue(f.service.read_plan(owner.current_metadata.plan_id).coordination.has_unresolved_uses is False)
            self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0].absent_work_id,'actual-unknown')
            f.cleanup()

    def test_heterogeneous_inventory_subset_stop_retains_anchor_and_fences_foreign_cursor(self):
        from adaptive_planning.types import HostWork,ResourceClaim,ResourceResolution
        from tests.service_fixtures import seed_terminal_history
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f,owner=self.restored_empty(directory)
            repo=f.register(ResourceClaim('synthetic-host','repository',7,810,'/synthetic/cohort-repo',False,None,None),owner)
            child=f.register(workspace('/synthetic/cohort-repo/work',811,repository_id=repo),owner)
            external=ResourceClaim('synthetic-host','external',None,None,None,False,None,'synthetic:external-cohort')
            work=HostWork('actual-mixed','worker-a','running',(repo,child),(external,),checkpoint().evidence)
            self.inventory(f,owner,(work,))
            page=f.service.read_plan(owner.current_metadata.plan_id)
            orphan=page.coordination.reservations[0]
            self.assertEqual(len(orphan.resource_ids),3)
            observer=f.create(steps=(step('a'),step('b')),creation_id='observer')
            oid=observer.current_metadata.plan_id
            f.assign(observer,'a')
            f.assign(observer,'b')
            old=f.service.read_plan(oid)
            cursor=f.service.read_coordination(oid,limit=1).next_cursor
            self.assertIsNotNone(cursor)
            self.inventory(f,owner,(work,),(ResourceResolution(None,work.work_id,(repo,),'stopped','explicit repository member stopped'),))
            changed=f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
            self.assertEqual(changed.use_id,orphan.use_id)
            self.assertEqual(len(changed.resource_ids),2)
            self.assertEqual(f.service.read_plan(oid).metadata,old.metadata)
            with self.assertRaises(DomainError) as error:
                f.service.read_coordination(oid,after=cursor,limit=1)
            self.assertEqual(error.exception.code,'conflict')
            with self.assertRaises(DomainError):
                f.service.execute(f.command('release_resource_use',dict(use_id=orphan.use_id,resource_ids=(child,),reason='subset cannot release full work',stopped=True),receipt=owner))
            f.service.execute(f.command('release_resource_use',dict(use_id=orphan.use_id,resource_ids=changed.resource_ids,reason='explicit full remaining work stopped',stopped=True),receipt=owner))
            self.assertEqual(f.service.read_plan(owner.current_metadata.plan_id).coordination.reservations,())
            self.assertEqual(f.store.read(lambda db:db.execute('SELECT count(*) FROM resource_uses WHERE absent_work_id=?',(work.work_id,)).fetchone()[0]),3)
            f.cleanup()

    def test_manifest_crash_windows_require_explicit_exact_recovery(self):
        from adaptive_planning.store import MANIFEST_FILENAME
        for after_replace,decision in ((False,'finish'),(True,'finish'),(True,'rollback')):
            with self.subTest(after_replace=after_replace,decision=decision),TemporaryDirectory() as directory:
                directory=Path(directory)
                target=directory/'planning.sqlite'
                shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
                original=target.read_bytes()
                actual_replace=os.replace
                def fail_replace(source,destination,*args,**kwargs):
                    if Path(destination).resolve()==target.resolve():
                        if after_replace:
                            actual_replace(source,destination,*args,**kwargs)
                        raise OSError('synthetic crash adjacent to replace')
                    return actual_replace(source,destination,*args,**kwargs)
                with patch('adaptive_planning.admin.os.replace',side_effect=fail_replace):
                    with self.assertRaises(OSError):
                        admin.migrate_installation(Config(directory),directory/'backups')
                self.assertTrue((directory/MANIFEST_FILENAME).is_file(),'Publication interruption lost its durable operation record')
                manifest=json.loads((directory/MANIFEST_FILENAME).read_text())
                with self.assertRaises(DomainError) as error:
                    Store(target,FakeClock())
                self.assertEqual(error.exception.code,'maintenance_pending')
                with self.assertRaises(DomainError):
                    admin.recover_maintenance(Config(directory),operation_id='wrong-operation',decision=decision)
                admin.recover_maintenance(Config(directory),operation_id=manifest['operation_id'],decision=decision)
                self.assertFalse((directory/MANIFEST_FILENAME).exists())
                with closing(sqlite3.connect(target)) as db, db:
                    self.assertEqual(db.execute('SELECT schema_version,authority_kind FROM installation').fetchone(),(1 if decision=='rollback' else 2,'authority'))

    def test_lifetime_and_callback_drain_block_cold_replacement(self):
        from adaptive_planning.store import maintenance_ownership
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f=make_service(directory)
            sibling=Store(f.store.path,f.clock)
            with self.assertRaises(DomainError) as error:
                admin.migrate_database(f.store,directory/'backups')
            self.assertEqual(error.exception.code,'busy')
            with self.assertRaises(DomainError) as error:
                f.store.read(lambda db:db.execute('SELECT 1').fetchone())
            self.assertEqual(error.exception.code,'closed')
            entered=threading.Event()
            release=threading.Event()
            closed=threading.Event()
            def callback(sequence):
                entered.set()
                release.wait(5)
            sibling.add_listener(callback)
            writer=threading.Thread(target=lambda:sibling.write(lambda db:db.execute("INSERT INTO events(kind,published_at) VALUES('synthetic-callback','2026-10-06T00:00:00Z')")))
            writer.start()
            self.assertTrue(entered.wait(5))
            closer=threading.Thread(target=lambda:(sibling.close(),closed.set()))
            closer.start()
            self.assertFalse(closed.is_set())
            with self.assertRaises(DomainError):
                with maintenance_ownership(directory):
                    pass
            release.set()
            writer.join(5)
            closer.join(5)
            self.assertTrue(closed.is_set())
            with maintenance_ownership(directory):
                pass

    def test_typed_inventory_clears_hold_without_reconstructing_postbackup_registry(self):
        from adaptive_planning.types import HostWork
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f=make_service(directory)
            backup=directory/'backup.sqlite'
            admin.backup_database(f.store,backup)
            late=f.create(creation_id='postbackup')
            claim=workspace('/synthetic/postbackup-writer',701)
            f.register(claim,late)
            f.store.close()
            admin.restore_database(Config(directory),backup,preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as restored:
                self.assertEqual(restored.store.read(lambda db:db.execute('SELECT count(*) FROM resources').fetchone()[0]),0)
                owner=restored.create(creation_id='inspection-owner')
                work=HostWork('actual-postbackup-worker','worker-a','running',(),(claim,),checkpoint().evidence)
                payload=dict(backup_cutoff=restored.service.read_service_info().restore_cutoff,inventory_complete=True,absent_work_inventory=(work,),resource_resolutions=(),reason='complete synthetic administrator inventory')
                try:
                    restored.service.execute(restored.command('resolve_store_hold',payload,receipt=owner))
                except DomainError as error:
                    self.fail(f'Typed inventory refused a valid current complete declaration: {error.code}')
                self.assertFalse(restored.service.read_service_info().store_hold)
                rid=restored.register(claim,owner)
                with self.assertRaises(DomainError) as error:
                    restored.service.execute(restored.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=owner))
                self.assertEqual(error.exception.code,'resource_conflict')
                reservation=restored.service.read_plan(owner.current_metadata.plan_id).coordination.reservations[0]
                restored.service.execute(restored.command('release_resource_use',dict(use_id=reservation.use_id,resource_ids=reservation.resource_ids,reason='synthetic explicit writer stopped',stopped=True),receipt=owner))
                restored.service.execute(restored.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=owner))

    def test_restore_rotates_capabilities_and_preserves_positive_native_recovery(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f=make_service(directory)
            p=f.create(steps=(step('a'),))
            rid=f.register(workspace('/synthetic/restore-native',750),p)
            a=f.service.execute(f.command('assign_step',dict(step_id='a',assignee='worker-a',context=context(rid)),receipt=p))
            start=f.command('start_step',dict(origin_worker='worker-a',relay=None,observed_activity='started'),receipt=p,attempt=a)
            start=replace(start,expected_revision=None,ownership_version=None)
            f.service.execute(start)
            old=f.service.read_plan(p.current_metadata.plan_id)
            backup=directory/'backup.sqlite'
            admin.backup_database(f.store,backup)
            f.store.close()
            outcome=admin.restore_database(Config(directory),backup,preserve_current=directory/'preserved.sqlite')
            with make_service(directory) as restored:
                self.assertNotEqual(restored.epoch,f.epoch)
                state=restored.service.read_plan(old.plan_id)
                self.assertNotEqual(state.metadata.generation,old.metadata.generation)
                self.assertTrue(restored.service.read_service_info().store_hold)
                with self.assertRaises(DomainError) as error:
                    restored.service.execute(start)
                self.assertEqual(error.exception.code,'stale_epoch')
                takeover=restored.command('takeover',dict(expected_ownership_version=state.metadata.ownership_version,reason='synthetic restore inspection'),receipt=p)
                owner=restored.service.execute(replace(takeover,epoch=restored.epoch,generation=state.metadata.generation,ownership_version=state.metadata.ownership_version))
                attempt=state.coordination.attempts[0]
                self.assertEqual(attempt.state,'stale')
                logical=restored.command('resolve_attempt',dict(resolution='release',reason='synthetic ownership ended',next_step_state='pending',stopped_resource_ids=(),replacement_assignee=None,replacement_context=None,attached_context=None),receipt=owner)
                logical=replace(logical,attempt_id=attempt.attempt_id,attempt_version=attempt.version,definition_version=attempt.definition_version)
                restored.service.execute(logical)
                latest=restored.service.read_plan(old.plan_id)
                self.assertFalse(any(h.kind=='stale' for h in latest.coordination.holds))
                self.assertTrue(latest.coordination.has_unresolved_uses)

    def test_cold_migration_retains_history_rowids_and_real_orphan_anchor(self):
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            with closing(sqlite3.connect(target)) as db, db:
                old={t:db.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid').fetchall() for t in ('attempts','capabilities','requests','checkpoints','reviews','result_versions','imports','resource_uses','events','creation_ids')}
                epoch=db.execute('SELECT epoch FROM installation').fetchone()[0]
            admin.migrate_installation(Config(directory),directory/'backups')
            self.assertEqual(len(list((directory/'backups').glob('*.sqlite'))),1)
            with Store(target,FakeClock()) as store:
                self.assertEqual(store.schema_version,2)
                self.assertEqual(store.installation_epoch(),epoch)
                for table,rows in old.items():
                    now=store.read(lambda db,t=table:db.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid').fetchall())
                    self.assertEqual(now[:len(rows)],rows,table)
                anchor=store.read(lambda db:db.execute('SELECT anchor_use_id FROM absent_host_work').fetchone()[0])
                self.assertEqual(anchor,next(u[1] for u in old['resource_uses'] if u[5]=='seeded-orphan'))
                self.assertEqual(store.read(lambda db:db.execute('SELECT count(*) FROM absent_host_work_members').fetchone()[0]),1)
            before=file_set(directory)
            admin.migrate_installation(Config(directory),directory/'unused-noop-backups')
            self.assertEqual(file_set(directory),before)
            self.assertFalse((directory/'unused-noop-backups').exists())

    def test_ambiguous_v1_orphan_refuses_with_prebackup_and_untouched_authority(self):
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            with closing(sqlite3.connect(target)) as db, db:
                db.execute('UPDATE absent_host_work SET resource_id=NULL')
            before=file_set(directory)
            with self.assertRaises(DomainError) as error:
                admin.migrate_installation(Config(directory),directory/'backups')
            self.assertEqual(error.exception.code,'invalid')
            self.assertEqual(file_set(directory),before)
            backups=list((directory/'backups').glob('*.sqlite'))
            self.assertEqual(len(backups),1)
            with closing(sqlite3.connect(backups[0])) as db, db:
                self.assertEqual(db.execute('SELECT schema_version,authority_kind FROM installation').fetchone(),(1,'backup'))

    def test_genuine_old_source_requires_explicit_migration_untouched(self):
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            target=directory/'planning.sqlite'
            shutil.copyfile(Path(__file__).parent/'fixtures/service_v1.sqlite',target)
            before=file_set(directory)
            try:
                Store(target,FakeClock())
            except DomainError as error:
                self.assertEqual(error.code,'schema_old')
            else:
                self.fail('Genuine v1 source was admitted by normal serving instead of cold migration')
            self.assertEqual(file_set(directory),before)

    def test_app_construction_failure_closes_the_owned_store(self):
        # Mutation caught: composed app failure leaves a replacement-blocking holder.
        from adaptive_planning.app import build_app
        made=[]
        def construct(*args,**kwargs):
            value=Store(*args,**kwargs)
            made.append(value)
            return value
        with TemporaryDirectory() as directory:
            config=Config(Path(directory))
            admin.initialize_installation(config)
            with patch('adaptive_planning.app.Store',side_effect=construct),patch('adaptive_planning.transport.build_mcp',side_effect=RuntimeError('synthetic composition failure')):
                with self.assertRaises(RuntimeError):
                    build_app(config)
            self.assertTrue(made[0]._closed,'Failed app composition retained its Store lifetime')

    def test_local_cli_produces_private_coherent_backup_and_public_export(self):
        # Mutation caught: CLI silently succeeds without performing local copy.
        with TemporaryDirectory() as directory:
            directory=Path(directory)
            f=make_service(directory)
            p=f.create()
            token=p.private.orchestrator_token
            backup=directory/'backup.sqlite'
            result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin','backup','--data-dir',str(directory),'--destination',str(backup)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode,0,result.stdout.decode())
            self.assertTrue(backup.is_file(),'Local backup command did not create a coherent image')
            with closing(sqlite3.connect(backup)) as db, db:
                self.assertEqual(db.execute('SELECT authority_kind FROM installation').fetchone(),('backup',))
                self.assertEqual(db.execute('SELECT count(*) FROM plans').fetchone(),(1,))
            self.assertEqual(backup.stat().st_mode&0o777,0o600)
            exported=directory/'public.json'
            result=subprocess.run([sys.executable,'-B','-m','adaptive_planning.admin','export','--data-dir',str(directory),'--destination',str(exported)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode,0,result.stdout.decode())
            self.assertTrue(exported.is_file(),'Local export command did not create the public snapshot')
            self.assertNotIn(token,exported.read_text())
            self.assertNotIn('private_grants',exported.read_text())
            f.store.close()

    def test_newer_and_backup_admission_leave_every_source_entry_untouched(self):
        # Mutation caught: opening/chmodding the original before admission.
        for field,value,code in [('schema_version',999,'schema_newer'),('authority_kind','backup','backup_source')]:
            with self.subTest(field=field),TemporaryDirectory() as directory:
                directory=Path(directory)
                f=make_service(directory)
                f.store.write(lambda db:db.execute(f'UPDATE installation SET {field}=?',(value,)))
                os.chmod(f.store.path,0o640)
                (directory/'service-token').write_bytes(b'synthetic-credential-kept-exactly-12345678')
                before=file_set(directory)
                try:
                    Store(f.store.path,f.clock)
                except DomainError as error:
                    self.assertEqual(error.code,code)
                else:
                    self.fail('Ordinary serving admitted an unsupported authority image')
                self.assertEqual(file_set(directory),before)

                f.cleanup()

    def test_corrupt_and_empty_admission_are_structured_and_untouched(self):
        for content in (b'',b'synthetic corrupt image'):
            with self.subTest(content=content),TemporaryDirectory() as directory:
                directory=Path(directory)
                target=directory/'planning.sqlite'
                target.write_bytes(content)
                os.chmod(target,0o640)
                before=file_set(directory)
                try:
                    Store(target,FakeClock())
                except Exception as error:
                    self.assertIsInstance(error,DomainError)
                    self.assertEqual(error.code,'invalid')
                else:
                    self.fail('Existing empty/corrupt image became an authority')
                self.assertEqual(file_set(directory),before)
