from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
import uuid
import time
import fcntl
import hashlib
import json
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, TypeVar

from adaptive_planning.schema import SCHEMA_VERSION, initialize_schema
from adaptive_planning.types import Clock, DomainError, Epoch

T = TypeVar("T")

# Process-wide lock to serialize temporary umask mutations across threads
_UMASK_LOCK = threading.Lock()
GATE_FILENAME = ".planning-operation.lock"
MANIFEST_FILENAME = ".planning-maintenance.json"
LOCK_SECONDS = 2.0


def store_is_held(connection: sqlite3.Connection) -> bool:
    """Every unresolved durable reason applies, including historical epochs."""
    return bool(connection.execute('SELECT store_hold OR EXISTS(SELECT 1 FROM store_hold_reasons WHERE resolved=0) FROM installation WHERE id=1').fetchone()[0])


def _descriptor(path: Path, *, directory: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
            os.close(descriptor)
            raise DomainError("invalid", "Coordination inode has the wrong type")
        return descriptor
    except OSError as error:
        raise DomainError("invalid", "Installation coordination is unavailable") from error


def _flock(descriptor: int, mode: int, seconds: float = LOCK_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise DomainError("busy", "Installation coordination is busy") from error
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        except OSError as error:
            raise DomainError("unsupported_filesystem", "Filesystem does not support required flock coordination") from error


@contextmanager
def maintenance_ownership(directory: Path):
    """Cold maintenance owns the same installation inode as every Store."""
    directory = Path(directory).resolve()
    descriptor = _descriptor(directory, directory=True)
    gate_fd = None
    try:
        _flock(descriptor, fcntl.LOCK_EX, seconds=0)
        info = directory.stat()
        held = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
            raise DomainError("fenced", "Installation directory changed")
        gate = directory / GATE_FILENAME
        if gate.exists():
            gate_fd = _descriptor(gate)
            _flock(gate_fd, fcntl.LOCK_EX, seconds=0)
        yield
    finally:
        if gate_fd is not None:
            os.close(gate_fd)
        os.close(descriptor)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_clone(clone: Path, versions, authority_kind, host_id) -> dict:
    with closing(sqlite3.connect(clone)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise DomainError("invalid", "Installation integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise DomainError("invalid", "Installation foreign key check failed")
        rows = db.execute("SELECT id,epoch,schema_version,host_id,authority_kind,"
                          "store_hold,restore_cutoff FROM installation").fetchall()
        if (len(rows) != 1 or rows[0][0] != 1 or
                not all(isinstance(rows[0][i], str) and rows[0][i] for i in (1, 3)) or
                type(rows[0][2]) is not int or rows[0][5] not in (0, 1)):
            raise DomainError("invalid", "Installation singleton metadata is malformed")
        _, epoch, version, host, kind, hold, cutoff = rows[0]
        if version not in versions:
            code = "schema_newer" if version > max(versions) else "schema_old"
            raise DomainError(code, "Installation schema is unsupported")
        if kind != authority_kind:
            raise DomainError("backup_source" if kind == "backup" else "invalid",
                              "Installation image has the wrong authority kind")
        if host_id is not None and host != host_id:
            raise DomainError("invalid", "Installation host does not match")
        if version == SCHEMA_VERSION:
            # A supported marker cannot authorize rebuilding missing authority
            # tables during ordinary startup.
            with closing(sqlite3.connect(':memory:')) as reference:
                initialize_schema(reference)
                for (table,) in reference.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    quoted = '"' + table.replace('"', '""') + '"'
                    if db.execute(f'PRAGMA table_info({quoted})').fetchall() != reference.execute(f'PRAGMA table_info({quoted})').fetchall():
                        raise DomainError('invalid', 'Installation authority layout is incomplete or unsupported')
        sequence = db.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0]
        return dict(epoch=epoch, schema_version=version, host_id=host,
                    authority_kind=kind, store_hold=bool(hold), restore_cutoff=cutoff,
                    sequence=sequence)


@contextmanager
def admitted_image(path: Path, *, versions: tuple[int, ...] = (SCHEMA_VERSION,),
                   authority_kind: str = "authority", host_id: str | None = None):
    """Retain the same recovered, admitted private snapshot for materialization.

    The caller must quiesce the original file set. SHM is bound as original
    evidence but never copied as authority: SQLite rebuilds its private cache.
    Source bindings describe the bytes before recovery touches the clone.
    """
    path = Path(path)
    with TemporaryDirectory(prefix="planning-admission-") as name:
        directory = Path(name)
        clone = directory / "image.sqlite"
        try:
            os.chmod(directory, 0o700)
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise DomainError("invalid", "Installation image is not a nonempty regular file")
            files = []
            for suffix in ('', '-journal', '-shm', '-wal'):
                source = Path(str(path) + suffix)
                if not source.exists():
                    continue
                if source.is_symlink() or not source.is_file():
                    raise DomainError("invalid", "Installation sidecar is not regular")
                if suffix != '-shm':
                    target = Path(str(clone) + suffix)
                    shutil.copyfile(source, target)
                    os.chmod(target, 0o600)
                else:
                    target = source
                files.append(dict(suffix=suffix, bytes=target.stat().st_size, sha256=_file_digest(target)))
            if len(files) == 1:
                source_digest = files[0]['sha256']
                binding = dict(encoding='raw-main-sha256', files=files)
            else:
                encoded = json.dumps(files, sort_keys=True, separators=(',', ':')).encode()
                source_digest = hashlib.sha256(b'adaptive-planning-source-file-set-v1\0' + encoded).hexdigest()
                binding = dict(encoding='file-set-v1', files=files)
            metadata = _inspect_clone(clone, versions, authority_kind, host_id)
        except (OSError, sqlite3.DatabaseError) as error:
            raise DomainError("invalid", "Installation image cannot be admitted") from error
        yield clone, metadata, source_digest, binding


def inspect_image(path: Path, *, versions: tuple[int, ...] = (SCHEMA_VERSION,),
                  authority_kind: str = "authority", host_id: str | None = None) -> dict:
    """Inspect a disposable quiesced clone; never open SQLite on the source."""
    with admitted_image(path, versions=versions, authority_kind=authority_kind, host_id=host_id) as (_, metadata, _, _):
        return metadata


class Store:
    """Atomic SQLite planning authority, epochs, and event projection."""

    def __init__(self, path: Path | str, clock: Clock, host_id: str | None = None) -> None:
        if Path(path).is_symlink():
            raise DomainError("invalid", "Installation main image cannot be a symlink")
        self.path = Path(path).resolve()
        self.clock = clock
        self._requested_host_id = host_id
        self._listeners: list[Callable[[int], None]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._active = 0
        self._lifecycle = threading.Condition()
        self._operation_local = threading.local()
        self._birth_pid = os.getpid()
        self._lease_fd = None
        if not self.path.parent.exists():
            self.path.parent.mkdir(mode=0o700, parents=True)
        try:
            # Bootstrap is serialized on the existing directory inode. Refuse
            # existing invalid images before introducing even the gate entry.
            bootstrap_deadline = time.monotonic() + LOCK_SECONDS
            while True:
                descriptor = _descriptor(self.path.parent, directory=True)
                self._lease_fd = descriptor
                gate = self.path.parent / GATE_FILENAME
                if not gate.exists():
                    try:
                        _flock(descriptor, fcntl.LOCK_EX, seconds=0)
                    except DomainError as error:
                        if error.code != 'busy':
                            raise
                        os.close(descriptor)
                        self._lease_fd = None
                        # Another bootstrap may have published the gate and
                        # already retained a shared lifetime lease. Restart
                        # admission instead of waiting behind that live Store.
                        if gate.exists():
                            continue
                        if time.monotonic() >= bootstrap_deadline:
                            raise
                        time.sleep(min(0.01, max(0, bootstrap_deadline - time.monotonic())))
                        continue
                    if not gate.exists():
                        self._admit_source()
                        fd = os.open(gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
                        os.close(fd)
                    os.close(descriptor)
                    self._lease_fd = None
                    continue
                _flock(descriptor, fcntl.LOCK_SH)
                self._directory_identity = (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                gate_fd = _descriptor(gate)
                try:
                    _flock(gate_fd, fcntl.LOCK_EX)
                    self._check_identity()
                    self._admit_source()
                    self._ensure_storage_environment()
                    self._init_installation()
                finally:
                    os.close(gate_fd)
                break
        except BaseException:
            if self._lease_fd is not None:
                os.close(self._lease_fd)
                self._lease_fd = None
            self._closed = True
            raise

    def _admit_source(self) -> None:
        if (self.path.parent / ".planning-maintenance.json").exists():
            raise DomainError("maintenance_pending", "Explicit maintenance recovery is required")
        if self.path.exists():
            inspect_image(self.path, host_id=self._requested_host_id)
        elif any(Path(str(self.path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise DomainError("invalid", "Installation main image is missing")

    def _check_open(self) -> None:
        if os.getpid() != self._birth_pid:
            raise DomainError("fenced", "Store belongs to another process")
        if self._closed or (self._closing and not getattr(self._operation_local, "depth", 0)):
            raise DomainError("closed", "Store is closed")

    def _check_identity(self) -> None:
        info = self.path.parent.stat()
        if (info.st_dev, info.st_ino) != self._directory_identity:
            raise DomainError("fenced", "Installation directory identity changed")

    @contextmanager
    def operation(self):
        with self._lifecycle:
            self._check_open()
            self._active += 1
            self._operation_local.depth = getattr(self._operation_local, "depth", 0) + 1
        descriptor = None
        try:
            self._check_identity()
            descriptor = _descriptor(self.path.parent / GATE_FILENAME)
            _flock(descriptor, fcntl.LOCK_SH)
            self._check_open()
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with self._lifecycle:
                self._active -= 1
                self._operation_local.depth -= 1
                self._lifecycle.notify_all()

    def close(self) -> None:
        if os.getpid() != self._birth_pid:
            raise DomainError("fenced", "Store belongs to another process")
        if getattr(self._operation_local, "depth", 0):
            raise DomainError("busy", "Cannot close a Store from its own operation or callback")
        with self._lifecycle:
            if self._closed:
                return
            self._closing = True
            while self._active:
                self._lifecycle.wait()
            if self._closed:
                return
            self._closed = True
            os.close(self._lease_fd)
            self._lease_fd = None

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()

    def _ensure_storage_environment(self) -> None:
        with _UMASK_LOCK:
            old_umask = os.umask(0o077)
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    os.chmod(self.path.parent, 0o700)
                except OSError as e:
                    raise DomainError(
                        "invalid",
                        f"Failed to enforce private storage directory mode 0700 on {self.path.parent}: {e}",
                    ) from e
                if stat.S_IMODE(os.stat(self.path.parent).st_mode) != 0o700:
                    raise DomainError(
                        "invalid",
                        f"Directory permissions {oct(stat.S_IMODE(os.stat(self.path.parent).st_mode))} do not match 0700",
                    )
            finally:
                os.umask(old_umask)

    def _enforce_file_permissions(self) -> None:
        try:
            if self.path.parent.exists():
                os.chmod(self.path.parent, 0o700)
                if stat.S_IMODE(os.stat(self.path.parent).st_mode) != 0o700:
                    raise DomainError("invalid", f"Directory {self.path.parent} mode is not 0700")
        except OSError as e:
            raise DomainError("invalid", f"Failed to set directory mode 0700 on {self.path.parent}: {e}") from e

        for p in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                    if stat.S_IMODE(os.stat(p).st_mode) != 0o600:
                        raise DomainError("invalid", f"File {p} mode is not 0600")
                except OSError as e:
                    raise DomainError("invalid", f"Failed to set file mode 0600 on {p}: {e}") from e

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        with _UMASK_LOCK:
            old_umask = os.umask(0o077)
            try:
                conn = sqlite3.connect(str(self.path), timeout=2.0, isolation_level=None)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                return conn
            except sqlite3.OperationalError as e:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    raise DomainError("busy", f"Database is busy during connection setup: {e}") from e
                raise
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise
            finally:
                os.umask(old_umask)

    def _init_installation(self) -> None:
        conn = self._connect()
        committed = False
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    raise DomainError("busy", f"Database is busy during initialization: {e}") from e
                raise

            table_check = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='installation'"
            ).fetchone()[0]

            if table_check == 0:
                # Atomically initialize schema and seed installation singleton
                initialize_schema(conn)
                epoch = f"epoch-{uuid.uuid4().hex}"
                actual_host_id = self._requested_host_id or str(uuid.uuid4())
                created_at = self.clock().isoformat()
                conn.execute(
                    "INSERT INTO installation (id, epoch, schema_version, host_id, authority_kind, created_at) "
                    "VALUES (1, ?, ?, ?, 'authority', ?)",
                    (epoch, SCHEMA_VERSION, actual_host_id, created_at),
                )
                self._enforce_file_permissions()
                conn.execute("COMMIT")
                committed = True
                self._epoch = epoch
                self._host_id = actual_host_id
                self._schema_version = SCHEMA_VERSION
            else:
                row = conn.execute(
                    "SELECT epoch, schema_version, host_id, authority_kind FROM installation WHERE id = 1"
                ).fetchone()
                if not row:
                    conn.execute("ROLLBACK")
                    raise DomainError("invalid", "Corrupt installation table: missing singleton row")
                epoch, schema_ver, stored_host_id, authority_kind = row
                if self._requested_host_id is not None and self._requested_host_id != stored_host_id:
                    conn.execute("ROLLBACK")
                    raise DomainError(
                        "invalid",
                        f"Host ID mismatch: stored {stored_host_id!r} != requested {self._requested_host_id!r}",
                    )
                self._enforce_file_permissions()
                conn.execute("COMMIT")
                committed = True
                self._epoch = epoch
                self._host_id = stored_host_id
                self._schema_version = schema_ver
        except Exception:
            if not committed and conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def installation_epoch(self) -> Epoch:
        """Return the immutable installation epoch."""
        self._check_open()
        return self._epoch

    @property
    def host_id(self) -> str:
        """Return the host ID."""
        self._check_open()
        return self._host_id

    @property
    def schema_version(self) -> int:
        """Return the recorded schema version."""
        self._check_open()
        return self._schema_version

    def add_listener(self, listener: Callable[[int], None]) -> None:
        """Register a post-commit sequence listener."""
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[int], None]) -> None:
        """Unregister a post-commit sequence listener."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify_listeners(self, sequence: int) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(sequence)
            except Exception:
                pass

    def get_store_sequence(self, connection: sqlite3.Connection | None = None) -> int:
        """Return MAX(events.sequence), or 0 for an empty store."""
        if connection is not None:
            row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()
            return row[0] if row else 0
        return self.read(lambda db: self.get_store_sequence(db))

    def write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self.operation():
            return self._write(operation)

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a short write operation in an exclusive transaction."""
        conn: sqlite3.Connection | None = None
        committed = False
        try:
            # Enforce file permissions before mutation
            self._enforce_file_permissions()
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    raise DomainError("busy", f"Database is busy: {e}") from e
                raise

            # Refuse newer schema before any mutation
            ver_row = conn.execute("SELECT schema_version FROM installation WHERE id = 1").fetchone()
            current_ver = ver_row[0] if ver_row else self._schema_version
            if current_ver > SCHEMA_VERSION:
                conn.execute("ROLLBACK")
                raise DomainError(
                    "schema_newer",
                    f"Database schema version {current_ver} is newer than supported {SCHEMA_VERSION}",
                )

            result = operation(conn)

            # Enforce file permissions (for any newly created WAL/SHM files) before COMMIT
            self._enforce_file_permissions()

            # Read latest event sequence inside the transaction before COMMIT
            latest_seq = self.get_store_sequence(conn)

            conn.execute("COMMIT")
            committed = True
        except Exception as e:
            if conn is not None and not committed:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
            if not committed:
                if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e).lower() or "busy" in str(e).lower()):
                    raise DomainError("busy", f"Database is busy: {e}") from e
                raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        # Post-commit sequence-only notification; post-commit cleanup/notification failures cannot replace result
        try:
            self._notify_listeners(latest_seq)
        except Exception:
            pass

        return result

    def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self.operation():
            return self._read(operation)

    def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a read operation in a single consistent SQLite snapshot."""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN DEFERRED")
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    raise DomainError("busy", f"Database is busy: {e}") from e
                raise

            result = operation(conn)
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            return result
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                raise DomainError("busy", f"Database is busy: {e}") from e
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
