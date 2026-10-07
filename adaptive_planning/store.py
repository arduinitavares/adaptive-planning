from __future__ import annotations

import os
import sqlite3
import stat
import threading
import uuid
from pathlib import Path
from typing import Callable, TypeVar

from adaptive_planning.schema import SCHEMA_VERSION, initialize_schema
from adaptive_planning.types import Clock, DomainError, Epoch

T = TypeVar("T")

# Process-wide lock to serialize temporary umask mutations across threads
_UMASK_LOCK = threading.Lock()


class Store:
    """Atomic SQLite planning authority, epochs, and event projection."""

    def __init__(self, path: Path | str, clock: Clock, host_id: str | None = None) -> None:
        self.path = Path(path).resolve()
        self.clock = clock
        self._requested_host_id = host_id
        self._listeners: list[Callable[[int], None]] = []
        self._lock = threading.Lock()

        # Enforce directory permissions (0700) with umask 0077 under process lock
        self._ensure_storage_environment()

        # Initialize or load installation metadata atomically
        self._epoch: Epoch
        self._host_id: str
        self._schema_version: int
        self._init_installation()

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
        return self._epoch

    @property
    def host_id(self) -> str:
        """Return the host ID."""
        return self._host_id

    @property
    def schema_version(self) -> int:
        """Return the recorded schema version."""
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
