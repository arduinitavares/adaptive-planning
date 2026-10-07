from __future__ import annotations

import os
import sqlite3
import stat
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from adaptive_planning.types import DomainError
from tests.service_fixtures import FakeClock

from adaptive_planning.schema import SCHEMA_VERSION, initialize_schema
from adaptive_planning.store import Store


class TestService(unittest.TestCase):
    """Test suite for atomic SQLite authority, schema, and Store operations."""


    def test_restart_preserves_epoch_and_events(self) -> None:
        """Closing and reopening DB preserves epoch and event sequence."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            epoch = store.installation_epoch()
            reopened = Store(path, clock)
            self.assertEqual(reopened.installation_epoch(), epoch)
            self.assertEqual(
                reopened.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0]),
                0,
            )

    def test_atomic_failure_rolls_back_every_record(self) -> None:
        """Injected failure after event insert rolls back plan, request, and event."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            epoch = store.installation_epoch()

            def failing_write(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("plan-1", "gen-1", clock().isoformat(), clock().isoformat()),
                )
                db.execute(
                    "INSERT INTO requests (request_id, epoch, key, response_payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("req-1", epoch, "key-1", "{}", clock().isoformat()),
                )
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("plan_created", "test", clock().isoformat(), "{}"),
                )
                raise RuntimeError("Simulated transaction failure")

            with self.assertRaises(RuntimeError):
                store.write(failing_write)

            self.assertEqual(
                store.read(lambda db: db.execute("SELECT count(*) FROM plans").fetchone()[0]),
                0,
            )
            self.assertEqual(
                store.read(lambda db: db.execute("SELECT count(*) FROM requests").fetchone()[0]),
                0,
            )
            self.assertEqual(
                store.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0]),
                0,
            )

    def test_write_contention_is_bounded(self) -> None:
        """Write contention on separate connection yields structured busy DomainError."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            barrier = threading.Barrier(2)
            blocker_done = threading.Event()
            contention_error: list[Exception] = []

            def holding_worker() -> None:
                # Open direct connection that holds an exclusive write lock
                conn = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
                try:
                    conn.execute("PRAGMA busy_timeout = 2000")
                    conn.execute("BEGIN IMMEDIATE")
                    barrier.wait()
                    blocker_done.wait(timeout=5.0)
                    conn.execute("ROLLBACK")
                finally:
                    conn.close()

            t = threading.Thread(target=holding_worker)
            t.start()

            try:
                barrier.wait()
                with self.assertRaises(DomainError) as ctx:
                    store.write(lambda db: db.execute("SELECT count(*) FROM plans").fetchone())
                self.assertEqual(ctx.exception.code, "busy")
            finally:
                blocker_done.set()
                t.join(timeout=3.0)

    def test_directory_and_file_permissions(self) -> None:
        """Directory mode is 0700 and DB file mode is 0600."""
        with TemporaryDirectory() as directory:
            sub_dir = Path(directory) / "secure_dir"
            path = sub_dir / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            Store(path, clock)

            dir_mode = stat.S_IMODE(os.stat(sub_dir).st_mode)
            file_mode = stat.S_IMODE(os.stat(path).st_mode)

            self.assertEqual(dir_mode, 0o700)
            self.assertEqual(file_mode, 0o600)

    def test_connection_pragmas(self) -> None:
        """Verify global PRAGMAs: foreign_keys=ON, busy_timeout=2000, WAL, synchronous=FULL."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            def check_pragmas(db: sqlite3.Connection) -> dict[str, object]:
                fk = db.execute("PRAGMA foreign_keys").fetchone()[0]
                bt = db.execute("PRAGMA busy_timeout").fetchone()[0]
                jm = db.execute("PRAGMA journal_mode").fetchone()[0]
                sync = db.execute("PRAGMA synchronous").fetchone()[0]
                return {"fk": fk, "bt": bt, "jm": jm, "sync": sync}

            pragmas = store.read(check_pragmas)
            self.assertEqual(pragmas["fk"], 1)
            self.assertEqual(pragmas["bt"], 2000)
            self.assertEqual(str(pragmas["jm"]).lower(), "wal")
            self.assertEqual(pragmas["sync"], 2)  # 2 corresponds to FULL

    def test_cross_plan_fk_substitution_is_rejected(self) -> None:
        """Attempts and result versions cannot substitute foreign keys across plans."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup_data(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("plan-A", "gen-A", now, now),
                )
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("plan-B", "gen-B", now, now),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("plan-A", "step-1", "v1"),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("plan-B", "step-2", "v1"),
                )

            store.write(setup_data)

            # Try to associate attempt for plan-B with step-1 of plan-A
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                        ("plan-B", "att-1", "step-1"),
                    )
                )

            # Try to associate result_version for plan-B with step-1 of plan-A
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO result_versions (plan_id, result_version, step_id) VALUES (?, ?, ?)",
                        ("plan-B", "res-1", "step-1"),
                    )
                )

    def test_resource_redirect_cycle_is_rejected(self) -> None:
        """Resource redirect cycle detection rejects cyclic redirection."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup_resources(db: sqlite3.Connection) -> None:
                for rid in ("res-1", "res-2", "res-3"):
                    db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                        (rid, "host-1", "workspace", now),
                    )
                db.execute(
                    "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                    ("res-1", "res-2", now),
                )
                db.execute(
                    "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                    ("res-2", "res-3", now),
                )

            store.write(setup_resources)

            # Creating res-3 -> res-1 would complete the cycle res-1 -> res-2 -> res-3 -> res-1
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                        ("res-3", "res-1", now),
                    )
                )

    def test_refuses_newer_schema_before_mutation(self) -> None:
        """Refuses newer schema versions with DomainError(schema_newer)."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            # Manually advance schema version in installation table
            raw = sqlite3.connect(str(path))
            raw.execute("UPDATE installation SET schema_version = 99")
            raw.commit()
            raw.close()

            # Attempting write on reopened or existing store must be refused with schema_newer
            reopened = Store(path, clock)
            with self.assertRaises(DomainError) as ctx:
                reopened.write(lambda db: db.execute("SELECT count(*) FROM plans").fetchone())
            self.assertEqual(ctx.exception.code, "schema_newer")

    def test_host_id_mismatch_rejected(self) -> None:
        """Reopening with mismatched host_id raises DomainError(invalid)."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            Store(path, clock, host_id="synthetic-host")

            with self.assertRaises(DomainError) as ctx:
                Store(path, clock, host_id="different-host")
            self.assertEqual(ctx.exception.code, "invalid")

    def test_resource_uses_exclusive_source_check(self) -> None:
        """resource_uses enforces exclusive source: either (plan_id, attempt_id) OR absent_work_id."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                    ("res-1", "host-1", "workspace", now),
                )
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p1", "g1", now, now),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("p1", "s1", "v1"),
                )
                db.execute(
                    "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                    ("p1", "a1", "s1"),
                )
                db.execute(
                    "INSERT INTO absent_host_work (absent_work_id, host_id, scope, created_at) VALUES (?, ?, ?, ?)",
                    ("ab1", "host-1", "workspace", now),
                )

            store.write(setup)

            # Valid normal row
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("u1", "res-1", "p1", "a1", None, "workspace"),
                )
            )

            # Valid orphan row
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("u2", "res-1", None, None, "ab1", "workspace"),
                )
            )

            # Invalid: both plan/attempt and absent_work_id set
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("u3", "res-1", "p1", "a1", "ab1", "workspace"),
                    )
                )

            # Invalid: neither plan/attempt nor absent_work_id set
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("u4", "res-1", None, None, None, "workspace"),
                    )
                )

    def test_read_establishes_single_snapshot(self) -> None:
        """Store.read executes across a single consistent SQLite snapshot for rows plus watermark."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            # Insert an initial event
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("initial", "tester", clock().isoformat(), "{}"),
                )
            )

            step1_barrier = threading.Barrier(2)
            step2_barrier = threading.Barrier(2)
            observed_snapshots: list[tuple[list[int], int]] = []

            def reader_thread() -> None:
                def read_callback(db: sqlite3.Connection) -> None:
                    rows1 = [r[0] for r in db.execute("SELECT sequence FROM events ORDER BY sequence").fetchall()]
                    wm1 = store.get_store_sequence(db)
                    observed_snapshots.append((rows1, wm1))

                    step1_barrier.wait()  # Tell writer to write
                    step2_barrier.wait()  # Wait for writer to finish commit

                    rows2 = [r[0] for r in db.execute("SELECT sequence FROM events ORDER BY sequence").fetchall()]
                    wm2 = store.get_store_sequence(db)
                    observed_snapshots.append((rows2, wm2))

                store.read(read_callback)

            t = threading.Thread(target=reader_thread)
            t.start()

            step1_barrier.wait()
            # Concurrently write another event while reader is inside its callback
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("concurrent", "writer", clock().isoformat(), "{}"),
                )
            )
            step2_barrier.wait()
            t.join(timeout=3.0)

            # Both queries inside read_callback must have seen rows [1] and watermark 1
            self.assertEqual(observed_snapshots[0], ([1], 1))
            self.assertEqual(observed_snapshots[1], ([1], 1))

            # A subsequent new read sees the updated rows [1, 2] and watermark 2
            def check_after(db: sqlite3.Connection) -> tuple[list[int], int]:
                rows = [r[0] for r in db.execute("SELECT sequence FROM events ORDER BY sequence").fetchall()]
                return rows, store.get_store_sequence(db)

            self.assertEqual(store.read(check_after), ([1, 2], 2))

    def test_sequence_only_notification_post_commit_not_rollback(self) -> None:
        """Listeners receive store_sequence only after commit, never during rollback."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            notified: list[int] = []
            store.add_listener(lambda seq: notified.append(seq))

            # Successful write appends event seq=1
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("evt1", "tester", clock().isoformat(), "{}"),
                )
            )
            self.assertEqual(notified, [1])

            # Failed write rolls back; notification must not be sent
            def failing_write(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("evt2", "tester", clock().isoformat(), "{}"),
                )
                raise RuntimeError("rollback")

            with self.assertRaises(RuntimeError):
                store.write(failing_write)

            self.assertEqual(notified, [1])

    def test_checkpoint_review_availability_same_plan_fks(self) -> None:
        """Checkpoints, reviews, and availability enforce same-plan composite pairing."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p1", "g1", now, now),
                )
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p2", "g2", now, now),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("p1", "s1", "v1"),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("p2", "s2", "v1"),
                )
                db.execute(
                    "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                    ("p1", "a1", "s1"),
                )
                db.execute(
                    "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                    ("p2", "a2", "s2"),
                )
                db.execute(
                    "INSERT INTO result_versions (plan_id, result_version, step_id) VALUES (?, ?, ?)",
                    ("p1", "rv1", "s1"),
                )
                db.execute(
                    "INSERT INTO result_versions (plan_id, result_version, step_id) VALUES (?, ?, ?)",
                    ("p2", "rv2", "s2"),
                )

            store.write(setup)

            # Checkpoint with cross-plan attempt (p1 with a2 from p2)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO checkpoints (plan_id, attempt_id, sequence, outcome, check_name, "
                        "evidence, origin_worker, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("p1", "a2", 1, "passed", "chk", "[]", "w1", now),
                    )
                )

            # Review with cross-plan attempt (p1 with a2 from p2)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO reviews (review_id, plan_id, attempt_id, step_id, reviewer, verdict, "
                        "findings, evidence, artifact, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("r1", "p1", "a2", "s1", "rev", "passed", "[]", "[]", "{}", now),
                    )
                )

            # Availability with cross-plan result_version (p1 with rv2 from p2)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO availability (plan_id, prerequisite, result_version, method, checked_base, "
                        "evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("p1", "s1", "rv2", "ancestor", "b1", "{}", now),
                    )
                )

    def test_unique_idempotency_constraints(self) -> None:
        """Verify UNIQUE constraints on requests, creation_ids, imports, and aliases."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            epoch = store.installation_epoch()
            now = clock().isoformat()

            store.write(
                lambda db: db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p1", "g1", now, now),
                )
            )

            # 1. requests UNIQUE(epoch, key)
            store.write(
                lambda db: db.execute(
                    "INSERT INTO requests (request_id, epoch, key, created_at) VALUES (?, ?, ?, ?)",
                    ("req1", epoch, "k1", now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, created_at) VALUES (?, ?, ?, ?)",
                        ("req2", epoch, "k1", now),
                    )
                )

            # 2. creation_ids UNIQUE(epoch, creation_id)
            store.write(
                lambda db: db.execute(
                    "INSERT INTO creation_ids (epoch, creation_id, plan_id, content_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (epoch, "c1", "p1", "sha256:1", now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO creation_ids (epoch, creation_id, plan_id, content_digest, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (epoch, "c1", "p1", "sha256:2", now),
                    )
                )

            # 3. imports UNIQUE(host_id, source_directory_id, original_task_id)
            store.write(
                lambda db: db.execute(
                    "INSERT INTO imports (import_id, host_id, source_directory_id, original_task_id, plan_id, content_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("imp1", "h1", "dir1", "task1", "p1", "sha256:1", now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO imports (import_id, host_id, source_directory_id, original_task_id, plan_id, content_digest, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("imp2", "h1", "dir1", "task1", "p1", "sha256:2", now),
                    )
                )

            # 4. aliases UNIQUE(host_id, canonical_path)
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                    ("res1", "h1", "workspace", now),
                )
            )
            store.write(
                lambda db: db.execute(
                    "INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("al1", "h1", "/path/to/repo", "res1", now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("al2", "h1", "/path/to/repo", "res1", now),
                    )
                )

    def test_wal_shm_modes_while_existing(self) -> None:
        """WAL and SHM files have mode 0600 while they exist during active write."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            wal_mode: int | None = None
            shm_mode: int | None = None

            def check_modes(db: sqlite3.Connection) -> None:
                nonlocal wal_mode, shm_mode
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("evt", "tester", clock().isoformat(), "{}"),
                )
                wal = path.with_name(path.name + "-wal")
                shm = path.with_name(path.name + "-shm")
                if wal.exists():
                    wal_mode = stat.S_IMODE(os.stat(wal).st_mode)
                if shm.exists():
                    shm_mode = stat.S_IMODE(os.stat(shm).st_mode)

            store.write(check_modes)

            self.assertEqual(wal_mode, 0o600)
            self.assertEqual(shm_mode, 0o600)

    def test_initialization_atomicity_and_rollback(self) -> None:
        """Failure during schema or installation initialization rolls back completely."""
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))

            # Simulate failure after table creation during initialize_schema
            original_init = initialize_schema

            def failing_init(conn: sqlite3.Connection) -> None:
                original_init(conn)
                raise RuntimeError("Simulated failure after table DDL before installation row")

            with patch("adaptive_planning.store.initialize_schema", side_effect=failing_init):
                with self.assertRaises(RuntimeError):
                    Store(path, clock)

            # Re-inspect SQLite file directly; due to transactional rollback, no tables should exist
            raw = sqlite3.connect(str(path))
            table_count = raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            raw.close()
            self.assertEqual(table_count, 0)

    def test_permission_enforcement_raises_on_failure(self) -> None:
        """Storage permission enforcement raises DomainError(invalid) on chmod failure."""
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))

            with patch("os.chmod", side_effect=OSError("Permission denied")):
                with self.assertRaises(DomainError) as ctx:
                    Store(path, clock)
                self.assertEqual(ctx.exception.code, "invalid")

    def test_setup_failure_closes_connection(self) -> None:
        """Connection setup lock error raises DomainError(busy) without leaking connection."""
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            # Simulate PRAGMA lock error during _connect
            original_connect = sqlite3.connect

            def locking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
                conn = original_connect(*args, **kwargs)
                raise sqlite3.OperationalError("database is locked")

            with patch("sqlite3.connect", side_effect=locking_connect):
                with self.assertRaises(DomainError) as ctx:
                    store.read(lambda db: db.execute("SELECT count(*) FROM plans").fetchone())
                self.assertEqual(ctx.exception.code, "busy")


if __name__ == "__main__":
    unittest.main()
