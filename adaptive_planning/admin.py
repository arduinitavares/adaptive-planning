"""Explicit local installation: the only place a service credential is ever generated."""
from __future__ import annotations

import os
import re
import secrets
import stat
import json
import sqlite3
import uuid
import tempfile
import time
import shutil
import hashlib
from contextlib import contextmanager
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from adaptive_planning.config import Config
from adaptive_planning.types import DomainError

TOKEN_FILENAME = "service-token"
_MAX_TOKEN_FILE_BYTES = 1024
_TOKEN = re.compile(rb"[\x21-\x7e]{32,512}")


def _token_path(config: Config) -> Path:
    return config.data_dir / TOKEN_FILENAME


def initialize_installation(config: Config) -> None:
    """Create the data directory and a private random credential if none exists.

    An existing credential is validated and preserved byte for byte, never regenerated.
    """
    try:
        load_service_token(config)
        return
    except DomainError as error:
        if error.code != "not_initialized":
            raise
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _publish_token(config.data_dir, _token_path(config))
    except OSError:
        raise DomainError("init_failed", "The service data directory could not be initialized.") from None
    load_service_token(config)


def _publish_token(directory: Path, path: Path) -> None:
    # The complete private file is created under a temporary name and hard-linked into place:
    # readers never see a partial credential and a concurrent initializer cannot overwrite it.
    temporary = directory / f".{TOKEN_FILENAME}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_urlsafe(32).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def load_service_token(config: Config) -> str:
    """Read the existing credential; never creates one. Errors never include path or content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(_token_path(config), flags)
    except FileNotFoundError:
        raise DomainError("not_initialized", "Service credential is not initialized; run explicit initialization.") from None
    except OSError:
        raise _insecure() from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise _insecure()
        if os.name != "nt" and (details.st_uid != os.geteuid() or details.st_mode & 0o177):
            raise _insecure()
        if details.st_size > _MAX_TOKEN_FILE_BYTES:
            raise _invalid()
        content = b""
        while len(content) <= _MAX_TOKEN_FILE_BYTES:
            chunk = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1 - len(content))
            if not chunk:
                break
            content += chunk
    except OSError:
        raise _insecure() from None
    finally:
        os.close(descriptor)
    token = content.removesuffix(b"\n").removesuffix(b"\r")
    if len(content) > _MAX_TOKEN_FILE_BYTES or _TOKEN.fullmatch(token) is None:
        raise _invalid()
    return token.decode("ascii")


def _insecure() -> DomainError:
    return DomainError(
        "insecure_credential", "Service credential must be a regular private file owned by the service user."
    )


def _invalid() -> DomainError:
    return DomainError("invalid_credential", "Service credential content is not valid.")


BACKUP_SECONDS = 10.0
MANIFEST_FILENAME = ".planning-maintenance.json"


def _destination(authority: Path, destination: Path) -> Path:
    from adaptive_planning.store import GATE_FILENAME
    destination = Path(destination)
    destination = destination.parent.resolve() / destination.name
    reserved = {authority, authority.parent / TOKEN_FILENAME,
                authority.parent / GATE_FILENAME, authority.parent / MANIFEST_FILENAME}
    reserved.update(Path(str(authority) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    if destination in reserved or destination.exists() or destination.is_symlink():
        raise DomainError("invalid", "Administrator destination is reserved or already exists")
    if not destination.parent.is_dir():
        raise DomainError("invalid", "Administrator destination parent does not exist")
    return destination


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _publish(stage: Path, destination: Path) -> None:
    _fsync_file(stage)
    try:
        os.link(stage, destination)
    except FileExistsError as error:
        raise DomainError("invalid", "Administrator destination was claimed concurrently") from error
    try:
        _fsync_directory(destination.parent)
    except OSError as error:
        raise DomainError("durability_uncertain", "Valid artifact was published; directory durability is uncertain") from error


def _stage(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    return Path(name)


@contextmanager
def _clone_image(authority: Path):
    with tempfile.TemporaryDirectory(prefix="planning-cold-copy-") as name:
        directory = Path(name)
        os.chmod(directory,0o700)
        clone = directory / "image.sqlite"
        for suffix in ("", "-wal", "-journal"):
            source = Path(str(authority) + suffix)
            if source.exists():
                if source.is_symlink() or not source.is_file():
                    raise DomainError("invalid", "Installation sidecar is not regular")
                target = Path(str(clone) + suffix)
                shutil.copyfile(source,target)
                os.chmod(target,0o600)
        yield clone


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _logical_identity(path: Path) -> str:
    """Bind a quiesced main/WAL image across SQLite journal normalization."""
    with _clone_image(path) as clone,closing(sqlite3.connect(clone)) as db:
        schema=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        data=[]
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted='"'+name.replace('"','""')+'"'
            data.append([name,db.execute(f'SELECT rowid,* FROM {quoted} ORDER BY rowid').fetchall()])
        return hashlib.sha256(json.dumps([schema,data],sort_keys=True).encode()).hexdigest()


def _manifest_write(directory: Path, manifest: dict) -> None:
    temporary=_stage(directory,".planning-manifest-")
    try:
        with temporary.open('w') as handle:
            json.dump(manifest,handle,sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,directory/MANIFEST_FILENAME)
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_target(authority: Path) -> None:
    with closing(sqlite3.connect(authority,timeout=2,isolation_level=None)) as db:
        db.execute('PRAGMA busy_timeout=2000')
        busy,*_=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy:
            raise DomainError('busy','Target checkpoint could not drain')
        if db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':
            raise DomainError('busy','Target journal normalization failed')
    _fsync_file(authority)
    _fsync_directory(authority.parent)


def _complete_manifest(directory: Path, manifest: dict) -> None:
    """Remove the serving fence only after durable completion; retain it on uncertainty."""
    try:
        (directory/MANIFEST_FILENAME).unlink()
        _fsync_directory(directory)
    except BaseException:
        _manifest_write(directory,manifest)
        raise


def _replace_authority(authority: Path, stage: Path, preserved: Path, metadata: dict, kind: str) -> dict:
    from adaptive_planning.store import inspect_image
    manifest=dict(format=1,operation_id=uuid.uuid4().hex,kind=kind,host_id=metadata['host_id'],
                  target=str(authority),stage=str(stage),preserved=str(preserved),phase='prepared',
                  original_metadata=metadata,original_identity=_logical_identity(authority),
                  original_file_identity=[authority.stat().st_dev,authority.stat().st_ino],
                  preserved_sha256=_digest(preserved),stage_sha256=_digest(stage),
                  stage_metadata=inspect_image(stage),preserved_metadata=inspect_image(preserved,versions=(1,2),authority_kind='backup'))
    _fsync_file(stage)
    _manifest_write(authority.parent,manifest)
    _normalize_target(authority)
    manifest['phase']='normalized'
    _manifest_write(authority.parent,manifest)
    os.replace(stage,authority)
    _fsync_directory(authority.parent)
    manifest['phase']='published'
    _manifest_write(authority.parent,manifest)
    _complete_manifest(authority.parent,manifest)
    return manifest


def recover_maintenance(config: Config, *, operation_id: str, decision: str) -> dict:
    from adaptive_planning.store import inspect_image,maintenance_ownership
    authority=config.data_dir.resolve()/'planning.sqlite'
    with maintenance_ownership(authority.parent):
        manifest_path=authority.parent/MANIFEST_FILENAME
        try:
            manifest=json.loads(manifest_path.read_text())
            if (manifest['format']!=1 or manifest['operation_id']!=operation_id or
                    manifest['target']!=str(authority) or decision not in ('finish','rollback') or
                    manifest['phase'] not in ('prepared','normalized','published') or
                    manifest.get('resolution_decision',decision)!=decision):
                raise ValueError('Operation mismatch')
            stage=Path(manifest['stage'])
            preserved=Path(manifest['preserved'])
            if stage.parent!=authority.parent or preserved.is_symlink() or _digest(preserved)!=manifest['preserved_sha256']:
                raise ValueError('Image mismatch')
            inspect_image(preserved,versions=(1,2),authority_kind='backup',host_id=manifest['host_id'])
            inspect_image(authority,versions=(1,2),host_id=manifest['host_id'])
            current=_logical_identity(authority)
            is_original=current==manifest['original_identity']
            is_published=_digest(authority)==manifest['stage_sha256']
            resolution=manifest.get('resolution_stage')
            is_rollback=bool(resolution and _digest(authority)==manifest['resolution_sha256'])
            if not (is_original or is_published or is_rollback):
                raise ValueError('Current authority is not a recorded adjacent phase')
            if decision=='finish':
                if not is_published:
                    if stage.is_symlink() or _digest(stage)!=manifest['stage_sha256']:
                        raise ValueError('Stage mismatch')
                    inspect_image(stage,host_id=manifest['host_id'])
                    _normalize_target(authority)
                    manifest['resolution_decision']='finish'
                    _manifest_write(authority.parent,manifest)
                    os.replace(stage,authority)
                    _fsync_directory(authority.parent)
            elif not is_rollback:
                if resolution is None:
                    rollback=_stage(authority.parent,'.planning-rollback-')
                    shutil.copyfile(preserved,rollback)
                    with closing(sqlite3.connect(rollback)) as db,db:
                        db.execute("UPDATE installation SET authority_kind='authority',restore_cutoff=?",(manifest['original_metadata']['restore_cutoff'],))
                    inspect_image(rollback,versions=(1,2),host_id=manifest['host_id'])
                    if _logical_identity(rollback)!=manifest['original_identity']:
                        raise ValueError('Preserved authority does not match original')
                    _fsync_file(rollback)
                    manifest.update(resolution_decision='rollback',resolution_stage=str(rollback),resolution_sha256=_digest(rollback))
                    _manifest_write(authority.parent,manifest)
                else:
                    rollback=Path(resolution)
                    if rollback.parent!=authority.parent or rollback.is_symlink() or _digest(rollback)!=manifest['resolution_sha256']:
                        raise ValueError('Rollback stage mismatch')
                _normalize_target(authority)
                os.replace(rollback,authority)
                _fsync_directory(authority.parent)
            _complete_manifest(authority.parent,manifest)
            stage.unlink(missing_ok=True)
            return dict(operation_id=operation_id,decision=decision)
        except (KeyError,ValueError,TypeError,OSError,sqlite3.DatabaseError) as error:
            if isinstance(error,OSError) and not isinstance(error,FileNotFoundError):
                raise
            raise DomainError('invalid','Maintenance operation or image proof is invalid') from error


def _copy(source, stage: Path, *, retain_cutoff: bool = False) -> dict:
    from adaptive_planning.store import inspect_image
    deadline = time.monotonic() + BACKUP_SECONDS
    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise DomainError("busy", "Coherent backup deadline expired")
    progress(0, 0, 0)
    with closing(sqlite3.connect(stage)) as target:
        source.backup(target, pages=128, progress=progress, sleep=0.01)
        epoch, sequence, original_cutoff = target.execute("SELECT epoch,(SELECT coalesce(max(sequence),0) FROM events),restore_cutoff FROM installation").fetchone()
        cutoff = original_cutoff if retain_cutoff else f"{epoch}:{sequence}:{uuid.uuid4().hex}"
        target.execute("UPDATE installation SET authority_kind='backup',restore_cutoff=?", (cutoff,))
        target.commit()
        target.execute("PRAGMA journal_mode=DELETE")
    return inspect_image(stage, versions=(1, 2), authority_kind="backup")


def _cold_backup(authority: Path, destination: Path) -> dict:
    """Copy a quiesced image through SQLite recovery in a disposable clone."""
    destination = _destination(authority, destination)
    stage = _stage(destination.parent, ".planning-backup-")
    try:
        with _clone_image(authority) as clone:
            with closing(sqlite3.connect(clone)) as db:
                metadata = _copy(db, stage)
        _publish(stage, destination)
        return metadata
    finally:
        stage.unlink(missing_ok=True)


def _migrate_stage(stage: Path, metadata: dict) -> None:
    from adaptive_planning.schema import SCHEMA_STATEMENTS, initialize_schema
    from adaptive_planning import resources
    with closing(sqlite3.connect(stage, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            parents = db.execute("SELECT rowid,* FROM absent_host_work ORDER BY rowid").fetchall()
            uses = db.execute("SELECT rowid,* FROM resource_uses ORDER BY rowid").fetchall()
            for parent in parents:
                _, work_id, host, resource, scope, actor, status, created = parent
                members = [row for row in uses if row[5] == work_id]
                if not resource or host != metadata['host_id'] or not isinstance(actor,str) or not actor.strip() or not members:
                    raise DomainError("invalid", "Legacy orphan declaration cannot prove its cohort")
                original = resources.resolve_resource_id(db, resource)
                for row in members:
                    info = db.execute("SELECT host_id,kind FROM resources WHERE resource_id=?",(row[2],)).fetchone()
                    if (info != (host, scope) or row[6] != scope or resources.resolve_resource_id(db,row[2]) != original):
                        raise DomainError("invalid", "Legacy orphan members contradict their declaration")
            for table in ("absent_host_work", "resource_uses"):
                statement = next(s for s in SCHEMA_STATEMENTS if s.startswith(f"CREATE TABLE IF NOT EXISTS {table} ("))
                db.execute(statement.replace(f"EXISTS {table} (", f"EXISTS _migration_{table} (", 1))
            db.execute("INSERT INTO _migration_absent_host_work(rowid,absent_work_id,host_id,resource_id,scope,declared_identity,status,created_at) SELECT rowid,* FROM absent_host_work")
            db.execute("INSERT INTO _migration_resource_uses(rowid,use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at) SELECT rowid,* FROM resource_uses")
            db.execute("DROP TABLE resource_uses")
            db.execute("DROP TABLE absent_host_work")
            db.execute("ALTER TABLE _migration_absent_host_work RENAME TO absent_host_work")
            db.execute("ALTER TABLE _migration_resource_uses RENAME TO resource_uses")
            initialize_schema(db)
            for parent in parents:
                _, work_id, host, resource, scope, actor, status, created = parent
                members = [row for row in uses if row[5] == work_id]
                producer = "migration:" + work_id
                anchor = members[0][1] if members else None
                for row in members:
                    db.execute("INSERT OR IGNORE INTO absent_host_work_members VALUES(?,?,?)", (work_id, row[2], row[6]))
                declaration = dict(format=1, producer="migration", original_parent=list(parent[1:]),
                                   members=[dict(use_id=row[1], resource_id=row[2], scope=row[6], created_at=row[8]) for row in members])
                db.execute("UPDATE absent_host_work SET anchor_use_id=?,producer_id=? WHERE absent_work_id=?", (anchor, producer, work_id))
                db.execute("INSERT INTO host_work_declarations VALUES(?,?,?,?,?,?,?)",
                           (producer, work_id, "migration", actor, "unknown", json.dumps(declaration, sort_keys=True), created))
            db.execute("UPDATE installation SET schema_version=2,authority_kind='authority',restore_cutoff=?", (metadata["restore_cutoff"],))
            db.execute("INSERT INTO events(kind,actor,published_at,payload) VALUES('schema_migrated','local-administrator',?,?)",
                       (datetime.now(timezone.utc).isoformat(), json.dumps(dict(from_schema=1,to_schema=2), sort_keys=True)))
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise DomainError("invalid", "Migration foreign key validation failed")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=DELETE")


def migrate_installation(config: Config, backup_directory: Path) -> None:
    from adaptive_planning.store import inspect_image, maintenance_ownership
    authority = config.data_dir.resolve() / "planning.sqlite"
    with maintenance_ownership(authority.parent):
        if (authority.parent / MANIFEST_FILENAME).exists():
            raise DomainError("maintenance_pending", "Explicit recovery is required")
        metadata = inspect_image(authority, versions=(1,2))
        if metadata['schema_version'] == 2:
            return
        backup_directory = Path(backup_directory)
        backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = backup_directory / f"pre-migration-{uuid.uuid4().hex}.sqlite"
        _cold_backup(authority, backup)
        stage = _stage(authority.parent, ".planning-migration-")
        try:
            shutil.copyfile(backup, stage)
            _migrate_stage(stage, metadata)
            inspect_image(stage)
            _replace_authority(authority,stage,backup,metadata,'migration')
        finally:
            if not (authority.parent/MANIFEST_FILENAME).exists():
                stage.unlink(missing_ok=True)


def migrate_database(store, backup_directory: Path) -> None:
    """Consuming compatibility entry; supplied Store remains closed on refusal."""
    if store.path.name != 'planning.sqlite' or not isinstance(backup_directory,Path):
        raise DomainError('invalid','Cold migration requires the fixed installation and a local directory')
    directory=store.path.parent
    store.installation_epoch()
    store.close()
    migrate_installation(Config(directory),backup_directory)


def _restore_stage(stage: Path, source_metadata: dict, source_digest: str, source_binding: dict) -> str:
    from adaptive_planning import recovery,attempts,plans
    restore_id=uuid.uuid4().hex
    new_epoch='epoch-'+uuid.uuid4().hex
    now=datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(stage)) as db,db:
        generations={pid:[generation,'generation-'+uuid.uuid4().hex] for pid,generation in db.execute('SELECT plan_id,generation FROM plans')}
        previous=db.execute('SELECT restore_id,payload FROM restore_origins WHERE new_epoch=?',(source_metadata['epoch'],)).fetchone()
        inherited=[]
        lineage=[]
        if previous is not None:
            prior=json.loads(previous[1])
            lineage=[previous[0],*prior.get('lineage_restore_ids',[])]
            inherited=['restore-inventory:'+identity for identity in lineage]
        origin=dict(format=1,restore_id=restore_id,source_metadata=source_metadata,
                    source_digest=source_digest,source_binding=source_binding,generations=generations,created_at=now,inherited_inventory=inherited,lineage_restore_ids=lineage)
        explained=previous is not None and db.execute("SELECT 1 FROM store_hold_reasons WHERE reason_id=? AND epoch=? AND kind='restore-inventory' AND resolved=0",('restore-inventory:'+previous[0],source_metadata['epoch'])).fetchone()
        if source_metadata['store_hold'] and not explained:
            db.execute('INSERT INTO store_hold_reasons VALUES(?,?,?,?,0,?)',('inherited-flag:'+restore_id,source_metadata['epoch'],'restore-source',json.dumps(dict(restore_id=restore_id,source_epoch=source_metadata['epoch'],source_digest=source_digest,prior_flag=True),sort_keys=True),now))
        db.execute('INSERT INTO restore_origins VALUES(?,?,?,?,?,?,?)',
                   (restore_id,source_metadata['epoch'],new_epoch,source_metadata['host_id'],source_digest,source_metadata['restore_cutoff'],json.dumps(origin,sort_keys=True)))
        for complete in db.execute("SELECT * FROM attempts WHERE state IN ('assigned','active','submitted','stale')").fetchall():
            pid,aid,sid,actor,state,version,definition,started,stopped,raw=complete
            data=json.loads(raw)
            row=attempts._attempt_row(db,pid,aid)
            plan=plans._load_plan(db,pid)
            if recovery.has_restored_origin(db,pid,aid):
                _,_,prior_record,prior_holds,kind=recovery.restored_proof(db,pid,aid)
                prior_restore_id=prior_record['transition']['restore_id']
            else:
                prior_restore_id=None
                prior_holds=[]
                try:
                    recovery._native_origin(db,plan,row,data)
                    kind='native'
                    if state=='stale':
                        marker=data.get('stale_source')
                        recovery.linked_sweep(db,pid,row,data,marker)
                        expected=dict(kind='stale',step_id=sid,source_id=marker['source_id'],reason='Recorded inactivity requires an explicit ownership decision')
                        held=db.execute('SELECT reason,active FROM holds WHERE hold_id=? AND plan_id=?',(marker['hold_id'],pid)).fetchone()
                        if held is None or held[1]!=1 or json.loads(held[0])!=expected:
                            raise DomainError('invalid','Native sweep has no exact source-owned hold')
                        prior_holds=[[marker['hold_id'],held[0]]]
                except DomainError:
                    recovery._imported_proof(db,pid,aid)
                    kind='imported'
            record=dict(format=1,source=dict(attempt_row=list(complete),
                        capabilities=db.execute('SELECT * FROM capabilities WHERE plan_id=? AND json_valid(payload) AND json_extract(payload,\'$.attempt_id\')=?',(pid,aid)).fetchall(),
                        uses=db.execute('SELECT rowid,* FROM resource_uses WHERE plan_id=? AND attempt_id=? ORDER BY rowid',(pid,aid)).fetchall(),
                        owned_holds=prior_holds if kind=='native' else []),kind=kind,prior_restore_id=prior_restore_id)
            if state!='stale':
                data.update(prior_state=state,recovery_required=True)
                version+=1
            if kind=='imported':
                hold=data['import_source']['stale_hold_id']
                reason=db.execute('SELECT reason FROM holds WHERE hold_id=?',(hold,)).fetchone()[0]
                source_id=json.loads(reason)['source_id']
            else:
                hold=uuid.uuid4().hex
                source_id=f'restore:{restore_id}:{pid}:{aid}'
                reason=json.dumps(dict(kind='stale',step_id=sid,source_id=source_id,reason='Restored ownership requires reconciliation.'),sort_keys=True)
                db.execute('INSERT INTO holds VALUES(?,?,?,1,?)',(hold,pid,reason,now))
            db.execute("UPDATE attempts SET state='stale',version=?,payload=? WHERE plan_id=? AND attempt_id=?",(version,json.dumps(data,sort_keys=True),pid,aid))
            transition=dict(restore_id=restore_id,attempt_id=aid,step_id=sid,from_state=state,from_version=complete[5],to_state='stale',to_version=version,hold_id=hold,source_id=source_id)
            record['restored_row']=list(db.execute('SELECT * FROM attempts WHERE plan_id=? AND attempt_id=?',(pid,aid)).fetchone())
            record['hold']=[hold,reason]
            transition['record_digest']=hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()
            event=db.execute("INSERT INTO events(plan_id,kind,actor,published_at,payload) VALUES(?,'restore_attempt','local-administrator',?,?)",(pid,now,json.dumps(transition,sort_keys=True))).lastrowid
            record['transition']=dict(transition,event_id=event)
            db.execute('INSERT INTO restore_attempt_origins VALUES(?,?,?,?)',(restore_id,pid,aid,json.dumps(record,sort_keys=True)))
        for pid,(_,generation) in generations.items():
            db.execute('UPDATE plans SET generation=? WHERE plan_id=?',(generation,pid))
        db.execute('UPDATE capabilities SET revoked=1')
        db.execute("UPDATE resource_uses SET status='unresolved' WHERE status='active'")
        db.execute("UPDATE installation SET epoch=?,authority_kind='authority',store_hold=1,restore_cutoff=?",(new_epoch,source_metadata['restore_cutoff']))
        reason='restore-inventory:'+restore_id
        db.execute('INSERT INTO store_hold_reasons VALUES(?,?,?,?,0,?)',(reason,new_epoch,'restore-inventory',json.dumps(dict(restore_id=restore_id,backup_cutoff=source_metadata['restore_cutoff']),sort_keys=True),now))
        db.execute("INSERT INTO events(kind,actor,published_at,payload) VALUES('database_restored','local-administrator',?,?)",(now,json.dumps(dict(restore_id=restore_id,epoch=new_epoch,backup_cutoff=source_metadata['restore_cutoff'],origin_digest=hashlib.sha256(json.dumps(origin,sort_keys=True).encode()).hexdigest()),sort_keys=True)))
    return restore_id


def restore_database(config: Config, source: Path, *, preserve_current: Path) -> dict:
    from adaptive_planning.store import admitted_image,inspect_image,maintenance_ownership
    authority=config.data_dir.resolve()/'planning.sqlite'
    source=Path(source)
    if source.is_symlink():
        raise DomainError('invalid','Restore source must be a regular admitted image')
    source=source.resolve()
    with maintenance_ownership(authority.parent):
        if (authority.parent/MANIFEST_FILENAME).exists():
            raise DomainError('maintenance_pending','Explicit maintenance recovery is required')
        metadata=inspect_image(authority)
        with admitted_image(source,versions=(1,2),authority_kind='backup',host_id=metadata['host_id']) as (clone,source_metadata,source_digest,source_binding):
            preserve_current=_destination(authority,preserve_current)
            _cold_backup(authority,preserve_current)
            stage=_stage(authority.parent,'.planning-restore-')
            try:
                # Admission and bounded backup use the same private recovered
                # snapshot; never recopy the raw main file after admission.
                with closing(sqlite3.connect(clone)) as db:
                    _copy(db,stage,retain_cutoff=True)
                if source_metadata['schema_version']==1:
                    _migrate_stage(stage,source_metadata)
                restore_id=_restore_stage(stage,source_metadata,source_digest,source_binding)
                inspect_image(stage)
                _replace_authority(authority,stage,preserve_current,metadata,'restore')
                return dict(restore_id=restore_id,restore_cutoff=source_metadata['restore_cutoff'])
            finally:
                if not (authority.parent/MANIFEST_FILENAME).exists():
                    stage.unlink(missing_ok=True)


def backup_database(store, destination: Path) -> dict:
    """Create a private coherent recovery image with completed-copy metadata."""
    destination = _destination(store.path, destination)
    stage = _stage(destination.parent, ".planning-backup-")
    try:
        with store.operation(), closing(store._connect()) as source:
            metadata = _copy(source, stage)
        _publish(stage, destination)
        return metadata
    finally:
        stage.unlink(missing_ok=True)


def export_database(store, destination: Path) -> None:
    """Export only public projections, excluding grants and private origins."""
    from adaptive_planning import plans
    destination = _destination(store.path, destination)
    def snapshot(db):
        installation = dict(zip(("epoch", "schema_version", "host_id", "store_hold", "restore_cutoff"),
                                db.execute("SELECT epoch,schema_version,host_id,store_hold,restore_cutoff FROM installation").fetchone()))
        events = [dict(zip(("sequence", "plan_id", "plan_revision", "kind", "actor", "published_at"), row))
                  for row in db.execute("SELECT sequence,plan_id,plan_revision,kind,actor,published_at FROM events ORDER BY sequence")]
        public_plans=plans.to_jsonable(plans.list_plans(db, True))
        # The ordinary renderer's legacy history accepts arbitrary summary values.
        # Export instead reconstructs its explicit public history fields, admitting
        # text only from the two known summary producers.
        for view in public_plans:
            history=[]
            for revision,published,kind,raw in db.execute('SELECT plan_revision,published_at,kind,payload FROM events WHERE plan_id=? AND plan_revision BETWEEN 1 AND ? ORDER BY sequence DESC LIMIT 100',(view['plan_id'],view['metadata']['revision'])).fetchall()[::-1]:
                entry=dict(revision=revision,timestamp=published,summary=kind)
                if kind in ('create_plan','revise_plan'):
                    data=json.loads(raw)
                    if isinstance(data,dict):
                        if isinstance(data.get('summary'),str):
                            entry['summary']=data['summary']
                        if isinstance(data.get('authorization_note'),str):
                            entry['authorizationNote']=data['authorization_note']
                history.append(entry)
            view['legacy_snapshot']['history']=history
            view['legacy_snapshot']['changeSummary']=history[-1]['summary'] if history else ''
        return dict(format_version=1, installation=installation,
                    plans=public_plans, events=events,
                    aliases=[dict(zip(('alias_id','host_id','canonical_path','resource_id','created_at'),row)) for row in db.execute('SELECT alias_id,host_id,canonical_path,resource_id,created_at FROM aliases ORDER BY alias_id')],
                    redirects=[dict(zip(('retired_id','survivor_id','created_at'),row)) for row in db.execute('SELECT retired_id,survivor_id,created_at FROM resource_redirects ORDER BY retired_id')],
                    registry=[dict(zip(("resource_id", "host_id", "kind", "repository_id", "canonical_path"), row))
                              for row in db.execute("SELECT resource_id,host_id,kind,repository_id,canonical_path FROM resources")])
    with plans.service_context("http://127.0.0.1:8790", store.clock):
        data = store.read(snapshot)
    stage = _stage(destination.parent, ".planning-export-")
    try:
        with stage.open("w") as handle:
            json.dump(data, handle, sort_keys=True)
        _publish(stage, destination)
    finally:
        stage.unlink(missing_ok=True)


def _declare_inventory_work(db,work,restore_id,epoch,ts):
    from adaptive_planning import resources,plans
    ids=[resources.resolve_resource_id(db,rid) for rid in work.resource_ids]
    claimed=[]
    for claim in sorted(work.resource_claims,key=lambda claim:claim.kind!='repository'):
        rid=resources.register_claim(db,claim)
        if rid in claimed:
            raise DomainError('invalid','Inventory claims collapse to an ambiguous duplicate canonical identity')
        claimed.append(rid)
        ids.append(rid)
    ids=list(dict.fromkeys(ids))
    registry=resources._Registry(db)
    known=bool(ids) and all(any((attestation:=registry.attestations[member.resource_id]) is not None and (attestation.physical is not None or attestation.external_identity is not None) for member in registry.members(rid)) for rid in ids)
    existing=db.execute('SELECT 1 FROM absent_host_work WHERE absent_work_id=?',(work.work_id,)).fetchone()
    if existing:
        cohort=resources.orphan_cohort(db,work.work_id)
        if cohort['actor']!=work.actor or cohort['record']['producer']!='inventory':
            raise DomainError('invalid','Repeated work contradicts its original actor or producer')
        historical={resources.resolve_resource_id(db,row[1]) for row in cohort['rows']}
        if not historical<=set(ids):
            raise DomainError('held','Repeated work cannot omit its original physical identities')
        identity=cohort['identity_reason_id']
        identity_resolved=identity is not None and db.execute('SELECT resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()[0]==1
        if set(ids)==historical and (identity is None or identity_resolved or not known):
            return known
        if not known or not work.evidence:
            raise DomainError('held','Work identity attachment requires a complete cohort and evidence')
        producer=uuid.uuid4().hex
        members=list(cohort['record']['members'])
        for rid in ids:
            if rid in historical:
                continue
            kind=db.execute('SELECT kind FROM resources WHERE resource_id=?',(rid,)).fetchone()[0]
            uid=uuid.uuid4().hex
            db.execute('INSERT INTO absent_host_work_members VALUES(?,?,?)',(work.work_id,rid,kind))
            db.execute("INSERT INTO resource_uses(use_id,resource_id,absent_work_id,scope,status,created_at) VALUES(?,?,?,?,'unresolved',?)",(uid,rid,work.work_id,kind,ts))
            members.append(dict(use_id=uid,resource_id=rid,scope=kind,created_at=ts))
        if identity is not None:
            previous=cohort['record']
            reason=db.execute('SELECT epoch,kind,details,resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()
            expected=json.dumps(dict(restore_id=previous['restore_id'],work_id=work.work_id,actor=work.actor,producer_id=cohort['parent'][8]),sort_keys=True)
            if reason is None or reason[:3]!=(previous['epoch'],'absent-identity',expected):
                raise DomainError('invalid','Identity reason is not this work original producer obligation')
            db.execute('UPDATE store_hold_reasons SET resolved=1 WHERE reason_id=? AND epoch=?',(identity,reason[0]))
        record=dict(format=1,producer='inventory',restore_id=restore_id,epoch=epoch,work=plans.to_jsonable(work),members=members,identity_reason_id=identity,prior_producer_id=cohort['parent'][8])
        raw=json.dumps(record,sort_keys=True)
        db.execute('INSERT INTO host_work_declarations VALUES(?,?,?,?,?,?,?)',(producer,work.work_id,'inventory',work.actor,work.state,raw,ts))
        db.execute('UPDATE absent_host_work SET producer_id=?,anchor_use_id=coalesce(anchor_use_id,?) WHERE absent_work_id=?',(producer,members[0]['use_id'],work.work_id))
        plans._insert_event(db,None,None,'host_work_declared','local-administrator',ts,dict(producer_id=producer,absent_work_id=work.work_id,actor=work.actor,record_digest=hashlib.sha256(raw.encode()).hexdigest()))
        return True
    producer=uuid.uuid4().hex
    members=[]
    host=db.execute('SELECT host_id FROM installation').fetchone()[0]
    primary=ids[0] if ids else None
    scope=db.execute('SELECT kind FROM resources WHERE resource_id=?',(primary,)).fetchone()[0] if ids else 'workspace'
    db.execute('INSERT INTO absent_host_work(absent_work_id,host_id,resource_id,scope,declared_identity,status,created_at,producer_id) VALUES(?,?,?,?,?,?,?,?)',
               (work.work_id,host,primary,scope,work.actor,'unresolved',ts,producer))
    for rid in ids:
        kind=db.execute('SELECT kind FROM resources WHERE resource_id=?',(rid,)).fetchone()[0]
        uid=uuid.uuid4().hex
        db.execute('INSERT INTO absent_host_work_members VALUES(?,?,?)',(work.work_id,rid,kind))
        db.execute("INSERT INTO resource_uses(use_id,resource_id,absent_work_id,scope,status,created_at) VALUES(?,?,?,?,'unresolved',?)",(uid,rid,work.work_id,kind,ts))
        members.append(dict(use_id=uid,resource_id=rid,scope=kind,created_at=ts))
    if members:
        db.execute('UPDATE absent_host_work SET anchor_use_id=? WHERE absent_work_id=?',(members[0]['use_id'],work.work_id))
    identity=None
    if not known:
        identity='absent-identity:'+producer
        db.execute('INSERT INTO store_hold_reasons VALUES(?,?,?,?,0,?)',(identity,epoch,'absent-identity',json.dumps(dict(restore_id=restore_id,work_id=work.work_id,actor=work.actor,producer_id=producer),sort_keys=True),ts))
    record=dict(format=1,producer='inventory',restore_id=restore_id,epoch=epoch,work=plans.to_jsonable(work),members=members,identity_reason_id=identity)
    raw=json.dumps(record,sort_keys=True)
    db.execute('INSERT INTO host_work_declarations VALUES(?,?,?,?,?,?,?)',(producer,work.work_id,'inventory',work.actor,work.state,raw,ts))
    event=dict(producer_id=producer,absent_work_id=work.work_id,actor=work.actor,record_digest=hashlib.sha256(raw.encode()).hexdigest())
    plans._insert_event(db,None,None,'host_work_declared','local-administrator',ts,event)
    return known


def resolve_store_hold(db,command):
    from adaptive_planning import checkpoints,plans,resources,recovery,reviews
    from adaptive_planning.types import MutationReceipt
    plan=checkpoints._owner_target(db,command,'resolve_store_hold')
    payload=command.payload
    checkpoints._event_limits(plans.to_jsonable(payload))
    checkpoints._text(payload['reason'],'Inventory reason')
    epoch,cutoff=db.execute('SELECT epoch,restore_cutoff FROM installation').fetchone()
    origins=db.execute('SELECT restore_id FROM restore_origins WHERE new_epoch=? AND source_cutoff=?',(epoch,cutoff)).fetchall()
    if len(origins)!=1 or payload['backup_cutoff']!=cutoff:
        raise DomainError('invalid','Inventory does not identify the active restore cutoff')
    restore_id=origins[0][0]
    origin=recovery.restore_origin(db,restore_id)
    allowed={restore_id,*origin['lineage_restore_ids']}
    previous=plans.project_plan(db,plan.plan_id)
    ts=plans._timestamp(plan.updated_at)
    declared={work.work_id for work in payload['absent_work_inventory']}
    if len(declared)!=len(payload['absent_work_inventory']):
        raise DomainError('invalid','Inventory work identities must be distinct')
    for work in payload['absent_work_inventory']:
        if work.state=='stopped' and not any(resolution.absent_work_id==work.work_id and resolution.use_id is None and resolution.resolution=='stopped' for resolution in payload['resource_resolutions']):
            raise DomainError('invalid','Stopped work declaration needs an explicit exact whole-work stop resolution')
    covered=set(declared)
    for resolution in payload['resource_resolutions']:
        canonical=[resources.resolve_resource_id(db,rid) for rid in resolution.resource_ids]
        if len(set(canonical))!=len(canonical):
            raise DomainError('invalid','Inventory resource selectors must be distinct canonical identities')
        if resolution.absent_work_id is not None and resolution.use_id is None and resolution.resolution in ('stopped','unresolved'):
            if resolution.absent_work_id in declared:
                continue
            cohort=resources.orphan_cohort(db,resolution.absent_work_id)
            if not canonical or set(canonical)==set(cohort['resource_ids']):
                covered.add(resolution.absent_work_id)
    required=set()
    for work_id, in db.execute('SELECT absent_work_id FROM absent_host_work'):
        cohort=resources.orphan_cohort(db,work_id)
        record=cohort['record']
        identity=record.get('identity_reason_id')
        unidentified=identity is not None and db.execute('SELECT resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()[0]==0
        if record['producer']=='inventory' and record['restore_id'] in allowed and (cohort['remaining'] or unidentified):
            required.add(work_id)
    if payload['inventory_complete'] and not required<=covered:
        raise DomainError('held','Complete inventory omitted a retained declared writer obligation')
    known=True
    for work in payload['absent_work_inventory']:
        checkpoints._text(work.work_id,'Host work identity')
        checkpoints._text(work.actor,'Host work actor')
        known=_declare_inventory_work(db,work,restore_id,epoch,ts) and known
        if work.state=='stopped':
            cohort=resources.orphan_cohort(db,work.work_id)
            if not any(resolution.absent_work_id==work.work_id and resolution.use_id is None and resolution.resolution=='stopped' and
                       (not resolution.resource_ids or {resources.resolve_resource_id(db,rid) for rid in resolution.resource_ids}==set(cohort['resource_ids']))
                       for resolution in payload['resource_resolutions']):
                raise DomainError('invalid','Stopped work must resolve its complete exact cohort')
    for resolution in payload['resource_resolutions']:
        checkpoints._text(resolution.reason,'Inventory resolution reason')
        if resolution.absent_work_id is not None and resolution.use_id is None and resolution.resolution=='unresolved':
            cohort=resources.orphan_cohort(db,resolution.absent_work_id)
            selected={resources.resolve_resource_id(db,rid) for rid in resolution.resource_ids}
            if selected and not selected<=set(cohort['resource_ids']):
                raise DomainError('invalid','Inventory observation selected an unrelated resource')
            identity=cohort['identity_reason_id']
            known=known and (identity is None or db.execute('SELECT resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()[0]==1)
            plans._insert_event(db,None,None,'orphan_inventory_unresolved',command.actor,ts,dict(absent_work_id=resolution.absent_work_id,resource_ids=sorted(selected or set(cohort['resource_ids'])),reason=resolution.reason))
            continue
        if resolution.use_id is not None and resolution.absent_work_id is None and resolution.resolution in ('stopped','unresolved'):
            reviews.release_resource_use(db,recovery._internal_command(db,command,'release_resource_use',dict(use_id=resolution.use_id,resource_ids=resolution.resource_ids,stopped=resolution.resolution=='stopped',reason=resolution.reason)))
            plan=plans._load_plan(db,plan.plan_id)
            previous=plans.project_plan(db,plan.plan_id)
            continue
        if resolution.absent_work_id is None or resolution.use_id is not None or resolution.resolution!='stopped':
            raise DomainError('unsupported','This inventory resolution is not an explicit orphan stop')
        cohort=resources.orphan_cohort(db,resolution.absent_work_id)
        identity=cohort['identity_reason_id']
        if identity is not None and db.execute('SELECT resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()[0]!=1:
            raise DomainError('held','Unidentified work cannot be discharged by a stopped assertion')
        selected=resolution.resource_ids or cohort['resource_ids']
        chosen={resources.resolve_resource_id(db,rid) for rid in selected}
        if not chosen<=set(cohort['resource_ids']):
            raise DomainError('invalid','Inventory stop selected an unrelated resource')
        for row in cohort['remaining']:
            if resources.resolve_resource_id(db,row[1]) in chosen:
                db.execute("UPDATE resource_uses SET status='released' WHERE use_id=?",(row[0],))
        plans._insert_event(db,None,None,'orphan_inventory_stopped',command.actor,ts,dict(absent_work_id=resolution.absent_work_id,resource_ids=sorted(chosen),reason=resolution.reason))
    if payload['inventory_complete'] and known:
        for reason in ['restore-inventory:'+restore_id,*origin['inherited_inventory']]:
            linked=reason.removeprefix('restore-inventory:')
            source_origin=db.execute('SELECT new_epoch,source_cutoff FROM restore_origins WHERE restore_id=?',(linked,)).fetchone()
            if source_origin is None:
                raise DomainError('invalid','Inventory reason has no exact restore ancestor')
            expected=json.dumps(dict(restore_id=linked,backup_cutoff=source_origin[1]),sort_keys=True)
            if db.execute("UPDATE store_hold_reasons SET resolved=1 WHERE reason_id=? AND epoch=? AND kind='restore-inventory' AND details=?",(reason,source_origin[0],expected)).rowcount!=1:
                raise DomainError('invalid','Inventory reason contradicts its retained restore producer')
        db.execute('UPDATE installation SET store_hold=0')
    revision=plan.revision+1
    plans._insert_event(db,plan.plan_id,revision,'resolve_store_hold',command.actor,ts,dict(inventory_complete=payload['inventory_complete'],work_ids=[work.work_id for work in payload['absent_work_inventory']],backup_cutoff=cutoff,reason=payload['reason']))
    db.execute('UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?',(revision,ts,plan.plan_id))
    view=plans._commit_projection(db,plan.plan_id,previous)
    return MutationReceipt(dict(inventory_complete=payload['inventory_complete']),view.metadata,view.metadata,False,None)


def main(argv=None) -> int:
    import argparse
    from adaptive_planning.store import Store
    parser = argparse.ArgumentParser(description="Local planning database administration")
    parser.add_argument("command", choices=("backup", "export", "migrate", "restore", "recover"))
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--backup-directory", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--preserve-current", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--decision",choices=('finish','rollback'))
    args = parser.parse_args(argv)
    if args.command=='restore':
        if args.source is None or args.preserve_current is None:
            parser.error('restore requires --source and --preserve-current')
        restore_database(Config(args.data_dir),args.source,preserve_current=args.preserve_current)
        return 0
    if args.command=='recover':
        if args.operation_id is None or args.decision is None:
            parser.error('recover requires --operation-id and --decision')
        recover_maintenance(Config(args.data_dir),operation_id=args.operation_id,decision=args.decision)
        return 0
    if args.command == "migrate":
        if args.backup_directory is None:
            parser.error("migrate requires --backup-directory")
        migrate_installation(Config(args.data_dir), args.backup_directory)
        return 0
    if args.destination is None:
        parser.error("backup/export require --destination")
    with Store(args.data_dir / "planning.sqlite", lambda: datetime.now(timezone.utc)) as store:
        if args.command == "backup":
            backup_database(store, args.destination)
        else:
            export_database(store, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
