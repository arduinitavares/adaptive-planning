from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from adaptive_planning.types import DomainError
from tests.service_fixtures import FakeClock

from adaptive_planning.schema import initialize_schema
from adaptive_planning.store import Store


class TestService(unittest.TestCase):
    """Test suite for atomic SQLite authority, schema, and Store operations."""

    def test_restart_preserves_epoch_and_events(self) -> None:
        """Closing and reopening DB preserves epoch, host_id, events, watermark, and monotonic sequence."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock, host_id="test-host")
            epoch = store.installation_epoch()
            host = store.host_id
            self.assertEqual(host, "test-host")

            # Write two semantic events
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("create_plan", "user1", clock().isoformat(), "{}"),
                )
            )
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("record_checkpoint", "worker1", clock().isoformat(), "{}"),
                )
            )
            self.assertEqual(store.get_store_sequence(), 2)

            # Reopen store without specifying host_id: must recover stored host_id
            reopened = Store(path, clock)
            self.assertEqual(reopened.installation_epoch(), epoch)
            self.assertEqual(reopened.host_id, host)

            # Check persisted events and watermark
            events = reopened.read(
                lambda db: db.execute("SELECT sequence, kind, actor FROM events ORDER BY sequence").fetchall()
            )
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0], (1, "create_plan", "user1"))
            self.assertEqual(events[1], (2, "record_checkpoint", "worker1"))
            self.assertEqual(reopened.get_store_sequence(), 2)

            # Monotonic next sequence continues from previous sequence
            reopened.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("revise_plan", "user1", clock().isoformat(), "{}"),
                )
            )
            self.assertEqual(reopened.get_store_sequence(), 3)
            row3 = reopened.read(
                lambda db: db.execute("SELECT sequence, kind FROM events WHERE sequence = 3").fetchone()
            )
            self.assertEqual(row3, (3, "revise_plan"))

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
                    "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("req-1", epoch, "key-1", "0" * 64, "{}", clock().isoformat()),
                )
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("create_plan", "test", clock().isoformat(), "{}"),
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
        """Write contention on separate connection yields structured busy DomainError within bounded elapsed time."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            barrier = threading.Barrier(2)
            blocker_done = threading.Event()
            thread_error: list[Exception] = []

            def holding_worker() -> None:
                conn = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
                try:
                    conn.execute("PRAGMA busy_timeout = 2000")
                    conn.execute("BEGIN IMMEDIATE")
                    barrier.wait(timeout=5.0)
                    blocker_done.wait(timeout=5.0)
                    conn.execute("ROLLBACK")
                except Exception as ex:
                    thread_error.append(ex)
                finally:
                    conn.close()

            t = threading.Thread(target=holding_worker)
            t.start()

            try:
                barrier.wait(timeout=5.0)
                start_time = time.monotonic()
                with self.assertRaises(DomainError) as ctx:
                    store.write(lambda db: db.execute("SELECT count(*) FROM plans").fetchone())
                elapsed = time.monotonic() - start_time
                self.assertEqual(ctx.exception.code, "busy")
                self.assertGreaterEqual(elapsed, 1.5)
                self.assertLessEqual(elapsed, 5.0)
            finally:
                blocker_done.set()
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive())
                self.assertEqual(thread_error, [])

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
        """Refuses newer schema versions with DomainError(schema_newer) without invoking mutating callback."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            # Manually advance schema version in installation table
            raw = sqlite3.connect(str(path))
            raw.execute("UPDATE installation SET schema_version = 99")
            raw.commit()
            raw.close()

            # 1. Existing already-open store instance refuses newer schema on write
            callback_executed = False

            def mutating_callback(db: sqlite3.Connection) -> None:
                nonlocal callback_executed
                callback_executed = True
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p-mut", "g-mut", clock().isoformat(), clock().isoformat()),
                )

            with self.assertRaises(DomainError) as ctx:
                store.write(mutating_callback)
            self.assertEqual(ctx.exception.code, "schema_newer")
            self.assertFalse(callback_executed, "Mutating callback must not run on newer schema")

            # 2. Reopened store also refuses newer schema on write
            reopened = Store(path, clock)
            reopened_callback_executed = False

            def reopened_mutating_callback(db: sqlite3.Connection) -> None:
                nonlocal reopened_callback_executed
                reopened_callback_executed = True
                db.execute(
                    "INSERT INTO plans (plan_id, generation, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("p-mut2", "g-mut2", clock().isoformat(), clock().isoformat()),
                )

            with self.assertRaises(DomainError) as ctx:
                reopened.write(reopened_mutating_callback)
            self.assertEqual(ctx.exception.code, "schema_newer")
            self.assertFalse(reopened_callback_executed, "Mutating callback must not run on newer schema after reopen")

            # Verify no mutation was persisted
            raw_check = sqlite3.connect(str(path))
            plan_count = raw_check.execute("SELECT count(*) FROM plans").fetchone()[0]
            raw_check.close()
            self.assertEqual(plan_count, 0)

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
                    ("create_plan", "tester", clock().isoformat(), "{}"),
                )
            )

            step1_barrier = threading.Barrier(2)
            step2_barrier = threading.Barrier(2)
            observed_snapshots: list[tuple[list[int], int]] = []
            thread_error: list[Exception] = []

            def reader_thread() -> None:
                try:
                    def read_callback(db: sqlite3.Connection) -> None:
                        rows1 = [r[0] for r in db.execute("SELECT sequence FROM events ORDER BY sequence").fetchall()]
                        wm1 = store.get_store_sequence(db)
                        observed_snapshots.append((rows1, wm1))

                        step1_barrier.wait(timeout=5.0)  # Tell writer to write
                        step2_barrier.wait(timeout=5.0)  # Wait for writer to finish commit

                        rows2 = [r[0] for r in db.execute("SELECT sequence FROM events ORDER BY sequence").fetchall()]
                        wm2 = store.get_store_sequence(db)
                        observed_snapshots.append((rows2, wm2))

                    store.read(read_callback)
                except Exception as ex:
                    thread_error.append(ex)

            t = threading.Thread(target=reader_thread)
            t.start()

            step1_barrier.wait(timeout=5.0)
            # Concurrently write another event while reader is inside its callback
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("revise_plan", "writer", clock().isoformat(), "{}"),
                )
            )
            step2_barrier.wait(timeout=5.0)
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(thread_error, [])

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
                    ("create_plan", "tester", clock().isoformat(), "{}"),
                )
            )
            self.assertEqual(notified, [1])

            # Failed write rolls back; notification must not be sent
            def failing_write(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("revise_plan", "tester", clock().isoformat(), "{}"),
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
                    "INSERT INTO requests (request_id, epoch, key, command_digest, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("req1", epoch, "k1", "0" * 64, now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, command_digest, created_at) VALUES (?, ?, ?, ?, ?)",
                        ("req2", epoch, "k1", "0" * 64, now),
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
                    ("create_plan", "tester", clock().isoformat(), "{}"),
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
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))

            original_init = initialize_schema

            def failing_init(conn: sqlite3.Connection) -> None:
                original_init(conn)
                raise RuntimeError("Simulated failure after table DDL before installation row")

            with patch("adaptive_planning.store.initialize_schema", side_effect=failing_init):
                with self.assertRaises(RuntimeError):
                    Store(path, clock)

            raw = sqlite3.connect(str(path))
            table_count = raw.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            raw.close()
            self.assertEqual(table_count, 0)

    def test_permission_enforcement_raises_on_failure(self) -> None:
        """Storage permission enforcement raises DomainError(invalid) on chmod failure."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))

            with patch("os.chmod", side_effect=OSError("Permission denied")):
                with self.assertRaises(DomainError) as ctx:
                    Store(path, clock)
                self.assertEqual(ctx.exception.code, "invalid")

    # =========================================================================
    # Regressions for I1–I5 and rulings
    # =========================================================================

    def test_same_plan_wrong_step_pairing_rejected(self) -> None:
        """I1: Same-plan wrong-step pairing on reviews->attempts and availability->result_versions is rejected."""
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
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("p1", "s1", "v1"),
                )
                db.execute(
                    "INSERT INTO steps (plan_id, step_id, definition_version) VALUES (?, ?, ?)",
                    ("p1", "s2", "v1"),
                )
                # Attempt a1 belongs to step s1
                db.execute(
                    "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                    ("p1", "a1", "s1"),
                )
                # Result version rv1 belongs to step s1
                db.execute(
                    "INSERT INTO result_versions (plan_id, result_version, step_id) VALUES (?, ?, ?)",
                    ("p1", "rv1", "s1"),
                )

            store.write(setup)

            # 1. Review: wrong step on insert (attempt a1 is for s1, but review claims s2)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO reviews (review_id, plan_id, attempt_id, step_id, reviewer, verdict, "
                        "findings, evidence, artifact, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("r-bad", "p1", "a1", "s2", "rev", "passed", "[]", "[]", "{}", now),
                    )
                )

            # Insert valid review
            store.write(
                lambda db: db.execute(
                    "INSERT INTO reviews (review_id, plan_id, attempt_id, step_id, reviewer, verdict, "
                    "findings, evidence, artifact, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("r-good", "p1", "a1", "s1", "rev", "passed", "[]", "[]", "{}", now),
                )
            )

            # Review: wrong step on update
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "UPDATE reviews SET step_id = 's2' WHERE review_id = 'r-good'"
                    )
                )

            # 2. Availability: wrong prerequisite on insert (rv1 belongs to s1, but availability claims prerequisite s2)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO availability (plan_id, prerequisite, result_version, method, checked_base, "
                        "evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("p1", "s2", "rv1", "ancestor", "base", "{}", now),
                    )
                )

            # Insert valid availability
            store.write(
                lambda db: db.execute(
                    "INSERT INTO availability (plan_id, prerequisite, result_version, method, checked_base, "
                    "evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("p1", "s1", "rv1", "ancestor", "base", "{}", now),
                )
            )

            # Availability: wrong prerequisite on update
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "UPDATE availability SET prerequisite = 's2' WHERE plan_id = 'p1' AND result_version = 'rv1'"
                    )
                )

    def test_parent_delete_cannot_remove_unresolved_uses(self) -> None:
        """I2: Deleting parents of resource_uses or absent_host_work fails via RESTRICT and rolls back."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                    ("res-normal", "h1", "workspace", now),
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
                    "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, scope, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    ("u-normal", "res-normal", "p1", "a1", "workspace", now),
                )

                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                    ("res-orphan", "h1", "workspace", now),
                )
                db.execute(
                    "INSERT INTO absent_host_work (absent_work_id, host_id, resource_id, scope, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("ab-orphan", "h1", "res-orphan", "workspace", now),
                )
                db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, absent_work_id, scope, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'unresolved', ?)",
                    ("u-orphan", "res-orphan", "ab-orphan", "workspace", now),
                )
                # Released history row: must also be conservatively retained against parent deletion
                db.execute(
                    "INSERT INTO attempts (plan_id, attempt_id, step_id) VALUES (?, ?, ?)",
                    ("p1", "a-rel", "s1"),
                )
                db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, scope, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'released', ?)",
                    ("u-released", "res-normal", "p1", "a-rel", "workspace", now),
                )
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("create_plan", "tester", now, "{}"),
                )

            store.write(setup)

            # 1. Attempt to delete res-normal (referenced by u-normal and u-released)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM resources WHERE resource_id = 'res-normal'"))

            # 2. Attempt to delete attempt a1 (referenced by u-normal)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM attempts WHERE attempt_id = 'a1'"))

            # 3. Attempt to delete attempt a-rel (referenced by released u-released: history must be retained)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM attempts WHERE attempt_id = 'a-rel'"))

            # 4. Attempt to delete plan p1 (cascades to attempts, which are restricted by u-normal / u-released)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM plans WHERE plan_id = 'p1'"))

            # 5. Attempt to delete res-orphan (referenced by absent_host_work and u-orphan)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM resources WHERE resource_id = 'res-orphan'"))

            # 6. Attempt to delete ab-orphan (referenced by u-orphan)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM absent_host_work WHERE absent_work_id = 'ab-orphan'"))

            # Assert all uses (including released) and events remain intact
            uses = store.read(lambda db: [r[0] for r in db.execute("SELECT use_id FROM resource_uses ORDER BY use_id").fetchall()])
            self.assertEqual(uses, ["u-normal", "u-orphan", "u-released"])
            events_count = store.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0])
            self.assertEqual(events_count, 1)

    def test_resource_repository_id_and_orphan_scope_constraints(self) -> None:
        """I3: Workspace->repository identity rules and orphan scope downgrade prevention."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup(db: sqlite3.Connection) -> None:
                # Valid repository on host-1
                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, 'repository', ?)",
                    ("repo-1", "host-1", now),
                )
                # Valid repository on host-2
                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, 'repository', ?)",
                    ("repo-2", "host-2", now),
                )
                # Standalone workspace
                db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, 'workspace', ?)",
                    ("ws-standalone", "host-1", now),
                )

            store.write(setup)

            # Valid workspace referencing repository on same host
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, repository_id, created_at) VALUES (?, ?, 'workspace', ?, ?)",
                    ("ws-child", "host-1", "repo-1", now),
                )
            )

            # Reject workspace referencing repository on different host
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, repository_id, created_at) VALUES (?, ?, 'workspace', ?, ?)",
                        ("ws-diff-host", "host-1", "repo-2", now),
                    )
                )

            # Reject workspace referencing parent that is NOT a repository (ws-standalone is a workspace)
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, repository_id, created_at) VALUES (?, ?, 'workspace', ?, ?)",
                        ("ws-bad-parent", "host-1", "ws-standalone", now),
                    )
                )

            # Reject non-workspace resource having repository_id (kind='external' or kind='repository')
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, repository_id, created_at) VALUES (?, ?, 'external', ?, ?)",
                        ("ext-bad", "host-1", "repo-1", now),
                    )
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, repository_id, created_at) VALUES (?, ?, 'repository', ?, ?)",
                        ("repo-bad", "host-1", "repo-1", now),
                    )
                )

            # Reject deleting repository when a workspace references it
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("DELETE FROM resources WHERE resource_id = 'repo-1'"))

            # Reject changing kind of parent repository to 'workspace' while child workspace exists
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(lambda db: db.execute("UPDATE resources SET kind = 'workspace' WHERE resource_id = 'repo-1'"))

            # --- Orphan use scope tests ---
            store.write(
                lambda db: db.execute(
                    "INSERT INTO absent_host_work (absent_work_id, host_id, scope, created_at) VALUES (?, ?, 'repository', ?)",
                    ("ab-repo-work", "host-1", now),
                )
            )

            # Inserting orphan use with downgraded scope ('workspace' instead of 'repository') must fail
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resource_uses (use_id, resource_id, absent_work_id, scope, status, created_at) "
                        "VALUES (?, 'repo-1', 'ab-repo-work', 'workspace', 'unresolved', ?)",
                        ("u-orphan-bad", now),
                    )
                )

            # Inserting orphan use matching absent_host_work scope succeeds
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, absent_work_id, scope, status, created_at) "
                    "VALUES (?, 'repo-1', 'ab-repo-work', 'repository', 'unresolved', ?)",
                    ("u-orphan-good", now),
                )
            )

            # Updating orphan use scope to downgrade to 'workspace' must fail
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "UPDATE resource_uses SET scope = 'workspace' WHERE use_id = 'u-orphan-good'"
                    )
                )

    def test_post_commit_cleanup_failure_does_not_mask_success(self) -> None:
        """I4: Post-commit chmod failure does not replace successful write result; pre-commit failure rolls back."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            # 1. Post-commit chmod failure: write succeeds and returns value; proves late chmod is called & caught
            real_connect = sqlite3.connect
            real_chmod = os.chmod
            committed = False
            chmod_after_commit_called = False

            class CommitTrackingConn:
                def __init__(self, real_c: sqlite3.Connection) -> None:
                    self._c = real_c

                def execute(self, sql: str, *args: object, **kwargs: object):
                    res = self._c.execute(sql, *args, **kwargs)
                    if "commit" in sql.lower():
                        nonlocal committed
                        committed = True
                    return res

                def close(self) -> None:
                    return self._c.close()

                def __getattr__(self, name: str):
                    return getattr(self._c, name)

            def selective_chmod(target_path: os.PathLike[str] | str, mode: int, *args: object, **kwargs: object) -> None:
                if committed:
                    nonlocal chmod_after_commit_called
                    chmod_after_commit_called = True
                    raise OSError("Late chmod EPERM")
                return real_chmod(target_path, mode, *args, **kwargs)

            def test_write(db: sqlite3.Connection) -> str:
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("create_plan", "tester", clock().isoformat(), "{}"),
                )
                return "write_success"

            with patch("sqlite3.connect", side_effect=lambda *args, **kwargs: CommitTrackingConn(real_connect(*args, **kwargs))), \
                 patch("os.chmod", side_effect=selective_chmod):
                result = store.write(test_write)
                self.assertEqual(result, "write_success")
                self.assertTrue(chmod_after_commit_called, "Post-commit chmod must be invoked during cleanup")

            # Verify the write was actually committed
            seq = store.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0])
            self.assertEqual(seq, 1)

            # 2. Post-commit close failure: close failure in finally does not mask successful result
            class FailingCloseConn:
                def __init__(self, real_c: sqlite3.Connection) -> None:
                    self._c = real_c

                def close(self) -> None:
                    self._c.close()
                    raise sqlite3.OperationalError("Simulated post-commit close error")

                def __getattr__(self, name: str):
                    return getattr(self._c, name)

            with patch("sqlite3.connect", side_effect=lambda *args, **kwargs: FailingCloseConn(real_connect(*args, **kwargs))):
                res2 = store.write(
                    lambda db: db.execute(
                        "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                        ("revise_plan", "tester", clock().isoformat(), "{}"),
                    )
                )
                self.assertIsNotNone(res2)

            seq2 = store.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0])
            self.assertEqual(seq2, 2)

            # 3. Before-commit chmod failure: mutating operation runs, but pre-commit chmod fails; transaction rolls back
            mutating_called = False

            def fail_before_commit_write(db: sqlite3.Connection) -> None:
                nonlocal mutating_called
                mutating_called = True
                db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("revise_plan", "tester", clock().isoformat(), "{}"),
                )

            def failing_precommit_chmod(target_path: os.PathLike[str] | str, mode: int, *args: object, **kwargs: object) -> None:
                if mutating_called:
                    raise OSError("Pre-commit chmod EPERM")
                return real_chmod(target_path, mode, *args, **kwargs)

            with patch("os.chmod", side_effect=failing_precommit_chmod):
                with self.assertRaises(DomainError) as ctx:
                    store.write(fail_before_commit_write)
                self.assertEqual(ctx.exception.code, "invalid")
                self.assertTrue(mutating_called, "Mutating operation must run before pre-commit chmod failure")

            # Verify the attempted third event was rolled back
            seq_after = store.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0])
            self.assertEqual(seq_after, 2)

    def test_constructor_post_commit_cleanup_failure_does_not_fail(self) -> None:
        """I4: Constructor cleanup failure after installation commit does not report initialized store as failed."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))

            real_connect = sqlite3.connect
            real_chmod = os.chmod
            committed = False
            constructor_chmod_after_commit_called = False

            class CommitTrackingConn:
                def __init__(self, real_c: sqlite3.Connection) -> None:
                    self._c = real_c

                def execute(self, sql: str, *args: object, **kwargs: object):
                    res = self._c.execute(sql, *args, **kwargs)
                    if "commit" in sql.lower():
                        nonlocal committed
                        committed = True
                    return res

                def close(self) -> None:
                    return self._c.close()

                def __getattr__(self, name: str):
                    return getattr(self._c, name)

            def selective_chmod(target_path: os.PathLike[str] | str, mode: int, *args: object, **kwargs: object) -> None:
                if committed:
                    nonlocal constructor_chmod_after_commit_called
                    constructor_chmod_after_commit_called = True
                    raise OSError("Late constructor chmod EPERM")
                return real_chmod(target_path, mode, *args, **kwargs)

            with patch("sqlite3.connect", side_effect=lambda *args, **kwargs: CommitTrackingConn(real_connect(*args, **kwargs))), \
                 patch("os.chmod", side_effect=selective_chmod):
                store = Store(path, clock)
                self.assertIsNotNone(store.installation_epoch())
                self.assertTrue(constructor_chmod_after_commit_called, "Post-commit chmod must be invoked during constructor cleanup")

    def test_concurrent_umask_restoration(self) -> None:
        """I4: Concurrent Store initialization preserves process umask deterministically."""
        initial_umask = os.umask(0o022)
        os.umask(initial_umask)

        with TemporaryDirectory() as directory:
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            errors: list[Exception] = []

            def worker(idx: int) -> None:
                try:
                    p = Path(directory) / f"store_{idx}.sqlite"
                    s = Store(p, clock)
                    s.write(lambda db: db.execute("SELECT 1").fetchone())
                except Exception as ex:
                    errors.append(ex)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive())

            self.assertEqual(errors, [])
            current_umask = os.umask(0o022)
            os.umask(current_umask)
            self.assertEqual(current_umask, initial_umask)

    def test_connection_closure_across_all_paths(self) -> None:
        """I5: Verify deterministic connection closure on PRAGMA failure, success, callback error, and busy."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            real_connect = sqlite3.connect

            class TrackingConnection:
                def __init__(self, real_conn: sqlite3.Connection, fail_pragma: bool = False, fail_busy: bool = False) -> None:
                    self._conn = real_conn
                    self._fail_pragma = fail_pragma
                    self._fail_busy = fail_busy
                    self.close_count = 0

                def execute(self, sql: str, *args: object, **kwargs: object):
                    if self._fail_pragma and "pragma foreign_keys" in sql.lower():
                        raise sqlite3.OperationalError("database is locked")
                    if self._fail_busy and "begin immediate" in sql.lower():
                        raise sqlite3.OperationalError("database is locked")
                    return self._conn.execute(sql, *args, **kwargs)

                def close(self) -> None:
                    self.close_count += 1
                    return self._conn.close()

                def __getattr__(self, name: str):
                    return getattr(self._conn, name)

            # 1. Setup failure on PRAGMA: connection is closed
            proxy1: TrackingConnection | None = None

            def pragma_fail_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy1
                proxy1 = TrackingConnection(real_connect(*args, **kwargs), fail_pragma=True)
                return proxy1

            with patch("sqlite3.connect", side_effect=pragma_fail_connect):
                with self.assertRaises(DomainError) as ctx:
                    store.read(lambda db: db.execute("SELECT 1").fetchone())
                self.assertEqual(ctx.exception.code, "busy")
                self.assertIsNotNone(proxy1)
                self.assertEqual(proxy1.close_count, 1)

            # 2. Successful read: connection is closed
            proxy2: TrackingConnection | None = None

            def success_read_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy2
                proxy2 = TrackingConnection(real_connect(*args, **kwargs))
                return proxy2

            with patch("sqlite3.connect", side_effect=success_read_connect):
                res = store.read(lambda db: db.execute("SELECT 1").fetchone()[0])
                self.assertEqual(res, 1)
                self.assertIsNotNone(proxy2)
                self.assertEqual(proxy2.close_count, 1)

            # 3. Successful write: connection is closed
            proxy3: TrackingConnection | None = None

            def success_write_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy3
                proxy3 = TrackingConnection(real_connect(*args, **kwargs))
                return proxy3

            with patch("sqlite3.connect", side_effect=success_write_connect):
                store.write(lambda db: db.execute("SELECT 1").fetchone())
                self.assertIsNotNone(proxy3)
                self.assertEqual(proxy3.close_count, 1)

            # 4. Callback failure in read: connection is closed
            proxy4: TrackingConnection | None = None

            def cb_fail_read_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy4
                proxy4 = TrackingConnection(real_connect(*args, **kwargs))
                return proxy4

            with patch("sqlite3.connect", side_effect=cb_fail_read_connect):
                with self.assertRaises(RuntimeError):
                    store.read(lambda db: (_ for _ in ()).throw(RuntimeError("cb error")))
                self.assertIsNotNone(proxy4)
                self.assertEqual(proxy4.close_count, 1)

            # 5. Callback failure in write: connection is closed
            proxy5: TrackingConnection | None = None

            def cb_fail_write_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy5
                proxy5 = TrackingConnection(real_connect(*args, **kwargs))
                return proxy5

            with patch("sqlite3.connect", side_effect=cb_fail_write_connect):
                with self.assertRaises(RuntimeError):
                    store.write(lambda db: (_ for _ in ()).throw(RuntimeError("cb error")))
                self.assertIsNotNone(proxy5)
                self.assertEqual(proxy5.close_count, 1)

            # 6. Busy during write: connection is closed
            proxy6: TrackingConnection | None = None

            def busy_write_connect(*args: object, **kwargs: object) -> TrackingConnection:
                nonlocal proxy6
                proxy6 = TrackingConnection(real_connect(*args, **kwargs), fail_busy=True)
                return proxy6

            with patch("sqlite3.connect", side_effect=busy_write_connect):
                with self.assertRaises(DomainError) as ctx:
                    store.write(lambda db: db.execute("SELECT 1").fetchone())
                self.assertEqual(ctx.exception.code, "busy")
                self.assertIsNotNone(proxy6)
                self.assertEqual(proxy6.close_count, 1)

    def test_alias_host_scoping_enforced(self) -> None:
        """Ruling 13: Aliases must reference resources on the same host."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            store.write(
                lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                    ("res-h1", "host-1", "workspace", now),
                )
            )

            # Same-host alias succeeds
            store.write(
                lambda db: db.execute(
                    "INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("al-1", "host-1", "/path/repo", "res-h1", now),
                )
            )

            # Different-host alias referencing res-h1 must fail
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id, created_at) VALUES (?, ?, ?, ?, ?)",
                        ("al-2", "host-2", "/path/repo", "res-h1", now),
                    )
                )

            # Updating alias host_id to a different host must fail
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "UPDATE aliases SET host_id = 'host-2' WHERE alias_id = 'al-1'"
                    )
                )

    def test_requests_command_digest_required_and_validated(self) -> None:
        """Ruling 15: Requests table requires command_digest column with exact 64-character SHA-256 length."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            epoch = store.installation_epoch()
            now = clock().isoformat()
            valid_digest = "a" * 64

            # Valid command_digest succeeds
            store.write(
                lambda db: db.execute(
                    "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("req-valid", epoch, "k-valid", valid_digest, "{}", now),
                )
            )

            # Real SHA-256 hex digest succeeds and round-trips identically
            import hashlib
            real_sha = hashlib.sha256(b"canonical-envelope-bytes").hexdigest()
            self.assertEqual(len(real_sha), 64)
            store.write(
                lambda db: db.execute(
                    "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("req-real-sha", epoch, "k-real-sha", real_sha, "{}", now),
                )
            )
            stored_sha = store.read(
                lambda db: db.execute("SELECT command_digest FROM requests WHERE request_id = 'req-real-sha'").fetchone()[0]
            )
            self.assertEqual(stored_sha, real_sha)

            # Missing (NULL) command_digest is rejected
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("req-null", epoch, "k-null", None, "{}", now),
                    )
                )

            # Empty string (< 64) is rejected
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("req-empty", epoch, "k-empty", "", "{}", now),
                    )
                )

            # Invalid length (< 64) is rejected
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("req-short", epoch, "k-short", "a" * 63, "{}", now),
                    )
                )

            # Invalid length (> 64) is rejected
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO requests (request_id, epoch, key, command_digest, response_payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("req-long", epoch, "k-long", "a" * 65, "{}", now),
                    )
                )

    def test_resource_redirect_cycle_two_node_and_update(self) -> None:
        """Minors: Two-node redirect cycles and cyclic redirect updates are rejected."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planning.sqlite"
            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)
            now = clock().isoformat()

            def setup_resources(db: sqlite3.Connection) -> None:
                for rid in ("r-A", "r-B", "r-C", "r-D"):
                    db.execute(
                        "INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
                        (rid, "host-1", "workspace", now),
                    )

            store.write(setup_resources)

            # 1. Two-node cycle: r-A -> r-B, then r-B -> r-A
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                    ("r-A", "r-B", now),
                )
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                        ("r-B", "r-A", now),
                    )
                )

            # 2. Update causing a cycle: chain r-B -> r-C -> r-D
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                    ("r-B", "r-C", now),
                )
            )
            store.write(
                lambda db: db.execute(
                    "INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                    ("r-C", "r-D", now),
                )
            )
            # Updating r-B's survivor to r-A creates a cycle: r-A -> r-B -> r-A
            with self.assertRaises(sqlite3.IntegrityError):
                store.write(
                    lambda db: db.execute(
                        "UPDATE resource_redirects SET survivor_id = 'r-A' WHERE retired_id = 'r-B'"
                    )
                )

    def test_preexisting_loose_permissions_tightened(self) -> None:
        """Minors: Pre-existing loose permissions (0755 dir, 0644 db file) are tightened to 0700 and 0600."""
        with TemporaryDirectory() as directory:
            secure_dir = Path(directory) / "loose_dir"
            secure_dir.mkdir(mode=0o755, exist_ok=True)
            os.chmod(secure_dir, 0o755)

            path = secure_dir / "planning.sqlite"
            path.touch(mode=0o644)
            os.chmod(path, 0o644)

            self.assertEqual(stat.S_IMODE(os.stat(secure_dir).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

            clock = FakeClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
            store = Store(path, clock)

            self.assertEqual(stat.S_IMODE(os.stat(secure_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

            # Also verify after a write operation
            store.write(
                lambda db: db.execute(
                    "INSERT INTO events (kind, actor, published_at, payload) VALUES (?, ?, ?, ?)",
                    ("create_plan", "user1", clock().isoformat(), "{}"),
                )
            )
            self.assertEqual(stat.S_IMODE(os.stat(secure_dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
