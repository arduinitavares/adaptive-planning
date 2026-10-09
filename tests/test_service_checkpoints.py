"""Durable checkpoint publication, authenticated replay and atomic provisional holds."""
from __future__ import annotations
from contextlib import ExitStack

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import importlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from adaptive_planning import plans
from adaptive_planning.app import PlanningService
from adaptive_planning.core import MAX_TEXT_LEN
from adaptive_planning.store import Store
from adaptive_planning.types import DomainError, _to_checkpoint, parse_command
from tests.service_fixtures import (
    checkpoint, context, draft, make_service, seed_accepted_result, step, workspace,
)


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())
        self.addCleanup(self.f.cleanup)
        self.p = self.f.create(steps=(step("a"), step("b"), step("c", ("b",)), step("d", ("c",))))
        self.plan_id = self.p.current_metadata.plan_id

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    def execute(self, command, service=None):
        try:
            return (self.f.service if service is None else service).execute(command)
        except DomainError as error:
            if error.code == "unsupported":
                self.fail("Task15 record_checkpoint semantic operation is not enabled")
            raise

    def denied(self, command, code="invalid", service=None):
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            (self.f.service if service is None else service).execute(command)
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(self.snapshot() == before, "Rejected checkpoint changed persisted state")

    def assign(self, step_id="a", *, execution=None, assignee="worker-a"):
        return self.f.service.execute(self.f.command("assign_step", {
            "step_id": step_id, "assignee": assignee,
            "context": context(None, writes=False) if execution is None else execution,
        }, receipt=self.p))

    def start(self, assigned, origin="worker-a"):
        command = self.f.command("start_step", {
            "origin_worker": origin, "relay": None, "observed_activity": "Actual worker start",
        }, receipt=self.p, attempt=assigned)
        return self.f.service.execute(replace(command, actor=origin, expected_revision=None, ownership_version=None))

    def command(self, active, value=None, *, key=None):
        value = checkpoint() if value is None else value
        command = self.f.command("record_checkpoint", {"checkpoint": value},
                                 receipt=self.p, attempt=active, key=key)
        return replace(command, actor=value.origin_worker if value.relay is None else value.relay,
                       expected_revision=None if value.relay is None else command.expected_revision,
                       ownership_version=None if value.relay is None else command.ownership_version)

    def active(self, step_id="a", **kwargs):
        assigned = self.assign(step_id, **kwargs)
        self.start(assigned, kwargs.get("assignee", "worker-a"))
        return assigned

    def payload(self, assigned):
        return self.f.store.read(lambda db: json.loads(db.execute(
            "SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
            (self.plan_id, assigned.committed["attempt_id"])).fetchone()[0]))

    def edit_payload(self, assigned, update):
        def edit(db):
            value = json.loads(db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                          (self.plan_id, assigned.committed["attempt_id"])).fetchone()[0])
            update(value)
            db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(value), self.plan_id, assigned.committed["attempt_id"]))
        self.f.store.write(edit)

    def attempt(self, assigned, service=None):
        service = self.f.service if service is None else service
        return next(a for a in service.read_plan(self.plan_id).coordination.attempts
                    if a.attempt_id == assigned.committed["attempt_id"])

    def rows(self, table):
        return self.f.store.read(lambda db: tuple(db.execute(f"SELECT * FROM {table}")))

    def node(self, step_id, service=None):
        service = self.f.service if service is None else service
        return next(node for node in service.read_plan(self.plan_id).legacy_snapshot["steps"]
                    if node["id"] == step_id)

    def checkpoint_module(self):
        try:
            return importlib.import_module("adaptive_planning.checkpoints")
        except ModuleNotFoundError:
            self.fail("Task15 checkpoint domain is not implemented")

    def test_checkpoint_evidence_outcome_reassessment(self):
        with ExitStack() as _owned_store_lifetimes:
            active = self.active()
            value = checkpoint()
            self.denied(self.command(active, replace(value, evidence=())))
            raw = plans.to_jsonable(self.command(active))
            del raw["payload"]["checkpoint"]["reassessment"]
            before = self.snapshot()
            with self.assertRaises(DomainError) as missing:
                parse_command(raw)
            self.assertEqual(missing.exception.code, "invalid")
            self.assertTrue(self.snapshot() == before)
            oversized = replace(value, evidence=value.evidence * 51)
            self.denied(self.command(active, oversized), "limit_exceeded")
            failure = replace(checkpoint("failed"), evidence=(replace(checkpoint("failed").evidence[0],
                              summary="Essential failing check: second document page is missing",
                              limitations=("Temporary screenshot path will expire",),
                              blockers=("Missing second page must be fixed",)),))
            receipt = self.execute(self.command(active, failure))
            self.assertEqual(self.attempt(active).state, "submitted")
            self.assertEqual(self.attempt(active).version, 3)
            self.assertEqual(self.node("a")["status"], "blocked")
            self.assertNotIn("a", self.f.service.read_plan(self.plan_id).coordination.result_versions)
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            row = reopened.store.read(lambda db: db.execute(
                "SELECT sequence,outcome,check_name,evidence,origin_worker,relay,artifact,reassessment FROM checkpoints "
                "WHERE plan_id=? AND attempt_id=?", (self.plan_id, active.committed["attempt_id"])).fetchone())
            self.assertEqual(str(row[0]), receipt.committed["checkpoint_id"])
            durable = _to_checkpoint({"outcome": row[1], "check": row[2], "evidence": json.loads(row[3]),
                                      "origin_worker": row[4], "relay": row[5],
                                      "artifact": json.loads(row[6]), "reassessment": json.loads(row[7])})
            self.assertEqual(durable, failure)
            event = next(e for e in reopened.service.read_events(self.plan_id) if e.sequence == row[0])
            self.assertEqual(_to_checkpoint(event.payload["checkpoint"]), failure)
            self.assertEqual(self.node("a", reopened.service)["evidence"], [
                "Essential failing check: second document page is missing | check: Synthetic checkpoint check | result: failed"
                " | artifact: synthetic-artifact version v1 (code: Synthetic artifact output)"
                " | limitations: Temporary screenshot path will expire | blockers: Missing second page must be fixed"])

    def test_pass_submits_without_completing_or_satisfying_dependency(self):
        active = self.active("b")
        self.execute(self.command(active))
        self.assertEqual(self.node("b")["status"], "in-progress")
        self.assertEqual(self.attempt(active).state, "submitted")
        self.assertFalse(next(r for r in self.f.service.list_ready(self.plan_id) if r.step_id == "c").ready)
        self.assertEqual(self.rows("result_versions"), ())
        self.denied(self.command(active))

    def test_optional_artifact_allows_meaningful_passing_observation(self):
        active = self.active()
        value = checkpoint()
        value = replace(value, artifact=None, evidence=(replace(value.evidence[0], artifact=None,
                         limitations=("Observed immutable output; no final acceptance artifact yet",)),))
        self.execute(self.command(active, value))
        self.assertEqual(self.node("a")["status"], "in-progress")
        self.assertIn("no final acceptance artifact", self.node("a")["evidence"][0])

    def test_inconsistent_pass_proof_is_rejected_atomically(self):
        active = self.active()
        value = checkpoint()
        cases = [
            replace(value, evidence=(replace(value.evidence[0], result="failed"),)),
            replace(value, evidence=(replace(value.evidence[0], result="blocked"),)),
            replace(value, evidence=(replace(value.evidence[0], result="observation"),)),
            replace(value, evidence=(replace(value.evidence[0], blockers=("Unresolved check failure",)),)),
            replace(value, evidence=(replace(value.evidence[0], check="A different check"),)),
            replace(value, evidence=(replace(value.evidence[0], artifact=replace(value.artifact, version="wrong")),)),
            replace(value, check=" "),
            replace(value, evidence=(replace(value.evidence[0], summary=" "),)),
            replace(value, reassessment=replace(value.reassessment, reason=" ")),
        ]
        for item in cases:
            with self.subTest(case=cases.index(item)):
                self.denied(self.command(active, item))

    def test_evidence_limit_boundary_and_no_truncation(self):
        active = self.active()
        value = checkpoint()
        items = tuple(replace(value.evidence[0], summary=f"Essential evidence item {i}") for i in range(50))
        self.execute(self.command(active, replace(value, evidence=items)))
        self.assertEqual(len(self.node("a")["evidence"]), 50)
        self.assertEqual(len(json.loads(self.rows("checkpoints")[0][5])), 50)
        self.assertIn("Essential evidence item 49", self.node("a")["evidence"][-1])

    def test_cumulative_evidence_limit_rolls_back_all_stages(self):
        active = self.active()
        def prior(db):
            value = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                          (self.plan_id,)).fetchone()[0])
            value["evidence"] = [f"Existing durable evidence {i}" for i in range(49)]
            db.execute("UPDATE steps SET payload=? WHERE plan_id=? AND step_id='a'", (json.dumps(value), self.plan_id))
        self.f.store.write(prior)
        value = checkpoint(affected=("b",))
        self.denied(self.command(active, replace(value, evidence=value.evidence * 2)), "limit_exceeded")
        self.assertEqual(len(self.node("a")["evidence"]), 49)
        self.assertEqual(self.rows("checkpoints"), ())
        self.assertEqual(self.rows("holds"), ())

    def test_formatted_evidence_limit_is_not_cut(self):
        active = self.active()
        value = checkpoint()
        value = replace(value, evidence=(replace(value.evidence[0], summary="x" * MAX_TEXT_LEN),))
        self.denied(self.command(active, value), "limit_exceeded")

    def test_material_holds_commit_with_checkpoint(self):
        a, b = self.active(), self.active("b")
        previous = self.f.service.read_plan(self.plan_id)
        proposed = replace(draft(steps=(step("a"), step("b"), step("c", ("b",)), step("d", ("c",)))),
                           facts=("Proposed new fact awaiting orchestration",))
        value = checkpoint(affected=("b",))
        value = replace(value, reassessment=replace(value.reassessment, proposed_draft=proposed))
        receipt = self.execute(self.command(a, value))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual([(h.kind, h.step_id, h.source_id) for h in view.coordination.holds], [
            ("reassessment", "b", receipt.committed["checkpoint_id"]),
            ("reassessment", "c", receipt.committed["checkpoint_id"]),
            ("reassessment", "d", receipt.committed["checkpoint_id"])])
        self.assertEqual(self.attempt(b).state, "active")
        self.assertEqual(view.draft, previous.draft)
        ready = {r.step_id: r for r in self.f.service.list_ready(self.plan_id)}
        for sid in ("b", "c", "d"):
            self.assertFalse(ready[sid].ready)
            self.assertTrue(any("(reassessment)" in reason for reason in ready[sid].reasons))
        self.assertEqual(len(self.rows("reviews")), 0)

    def test_global_hold_blocks_fresh_assign_and_start(self):
        a = self.active()
        b = self.assign("b")
        receipt = self.execute(self.command(a, checkpoint(unbounded=True)))
        holds = self.f.service.read_plan(self.plan_id).coordination.holds
        self.assertEqual([(h.step_id, h.source_id) for h in holds], [(None, receipt.committed["checkpoint_id"])])
        start = self.f.command("start_step", {"origin_worker": "worker-a", "relay": None,
                              "observed_activity": "Attempted start under global hold"}, receipt=self.p, attempt=b)
        self.denied(replace(start, expected_revision=None, ownership_version=None), "held")
        self.denied(self.f.command("assign_step", {"step_id": "c", "assignee": "worker-a",
                    "context": context(None, writes=False)}, receipt=self.p), "held")
        self.assertTrue(all(not r.ready for r in self.f.service.list_ready(self.plan_id)))

    def test_unchanged_reason_installs_no_hold(self):
        active = self.active()
        self.execute(self.command(active))
        self.assertEqual(self.rows("holds"), ())
        self.assertTrue(next(r for r in self.f.service.list_ready(self.plan_id) if r.step_id == "b").ready)

    def test_material_scope_and_unchanged_consistency_fail_atomically(self):
        active = self.active()
        value = checkpoint()
        cases = (
            replace(value.reassessment, disposition="material", affected_steps=()),
            replace(value.reassessment, disposition="material", affected_steps=("missing",)),
            replace(value.reassessment, disposition="material", affected_steps=("",)),
            replace(value.reassessment, disposition="material", affected_steps=("b", "b")),
            replace(value.reassessment, affected_steps=("b",)),
            replace(value.reassessment, unbounded=True),
        )
        for reassessment in cases:
            with self.subTest(scope=reassessment.affected_steps, unbounded=reassessment.unbounded):
                self.denied(self.command(active, replace(value, reassessment=reassessment)))

    def test_fresh_held_active_checkpoint_preserves_independent_holds(self):
        a, b = self.active(), self.active("b")
        first = self.execute(self.command(a, checkpoint(affected=("b",))))
        self.f.store.write(lambda db: db.execute("INSERT INTO holds(hold_id,plan_id,reason) VALUES('older-global',?,?)",
            (self.plan_id, json.dumps({"kind": "invalidation", "step_id": None, "source_id": "older-source",
                                      "reason": "Independent unresolved hold"}))))
        before_holds = self.rows("holds")
        result = self.execute(self.command(b, checkpoint("blocked")))
        self.assertEqual(self.attempt(b).state, "submitted")
        self.assertEqual(self.node("b")["status"], "blocked")
        self.assertEqual(self.rows("holds"), before_holds)
        self.assertNotEqual(result.committed["checkpoint_id"], first.committed["checkpoint_id"])

    def test_failure_between_checkpoint_and_hold_rolls_back_every_stage(self):
        active = self.active()
        self.f.store.write(lambda db: db.execute(
            "CREATE TRIGGER owned_checkpoint_hold_fault BEFORE INSERT ON holds "
            "BEGIN SELECT RAISE(ABORT,'owned fault between checkpoint and hold'); END"))
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "owned fault between checkpoint and hold"):
            self.execute(self.command(active, checkpoint(affected=("b",))))
        self.assertTrue(self.snapshot() == before, "Checkpoint-to-hold failure published partial state")

    def test_request_and_projection_failure_roll_back_checkpoint_and_holds(self):
        active = self.active()
        command = self.command(active, checkpoint(affected=("b",)))
        before = self.snapshot()
        with patch("adaptive_planning.plans._commit_projection", side_effect=RuntimeError("owned projection fault")):
            with self.assertRaisesRegex(RuntimeError, "owned projection fault"):
                self.execute(command)
        self.assertTrue(self.snapshot() == before)
        self.f.store.write(lambda db: db.execute(
            "CREATE TRIGGER owned_checkpoint_request_fault BEFORE INSERT ON requests "
            "BEGIN SELECT RAISE(ABORT,'owned request fault'); END"))
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "owned request fault"):
            self.execute(command)
        self.assertTrue(self.snapshot() == before)

    def test_checkpoint_hold_serializes_with_concurrent_start(self):
        with ExitStack() as _owned_store_lifetimes:
            from adaptive_planning.app import HANDLERS
            self.assertIn("record_checkpoint", HANDLERS, "Task15 checkpoint publication is not enabled")
            active, assigned = self.active(), self.assign("b")
            publish = self.command(active, checkpoint(affected=("b",)))
            start = self.f.command("start_step", {"origin_worker": "worker-a", "relay": None,
                                  "observed_activity": "Concurrent start contender"}, receipt=self.p, attempt=assigned)
            start = replace(start, expected_revision=None, ownership_version=None)
            other_store = _owned_store_lifetimes.enter_context(Store(self.f.store.path, self.f.clock, host_id="synthetic-host"))
            other = PlanningService(other_store, base_url="http://127.0.0.1:8790", clock=self.f.clock)
            boundary = threading.Barrier(2)
            release = threading.Event()
            connect, other_connect = self.f.store._connect, other_store._connect
            paused = False

            def publishing_connection():
                db = connect()
                def pause_before_hold():
                    nonlocal paused
                    if not paused:
                        paused = True
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM holds").fetchone()[0], 0)
                        boundary.wait(timeout=10)
                        if not release.wait(timeout=10):
                            raise RuntimeError("Owned contention release timed out")
                    return 1
                db.create_function("owned_checkpoint_boundary", 0, pause_before_hold)
                return db

            def contender_connection():
                db = other_connect()
                # Only this test-owned connection fails immediately on the publisher's real write lock.
                db.execute("PRAGMA busy_timeout = 0")
                return db

            self.f.store.write(lambda db: db.execute(
                "CREATE TRIGGER owned_checkpoint_boundary_trigger BEFORE INSERT ON holds "
                "BEGIN SELECT owned_checkpoint_boundary(); END"))
            before_revision = self.f.service.read_plan(self.plan_id).metadata.revision
            before_publication = self.snapshot()
            with patch.object(self.f.store, "_connect", publishing_connection), \
                    patch.object(other_store, "_connect", contender_connection), ThreadPoolExecutor(max_workers=2) as pool:
                publisher = pool.submit(self.execute, publish)
                try:
                    boundary.wait(timeout=10)
                    # Independent WAL read sees the old complete state, never a checkpoint without its holds.
                    view = other.read_plan(self.plan_id)
                    self.assertEqual(view.metadata.revision, before_revision)
                    self.assertEqual(view.coordination.holds, ())
                    self.assertEqual(other_store.read(lambda db: db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]), 0)
                    self.assertEqual(other_store.read(lambda db: db.execute("PRAGMA busy_timeout").fetchone()[0]), 0)
                    contender = pool.submit(other.execute, start)
                    # Publication stays paused until SQLite itself has rejected the conflicting BEGIN IMMEDIATE.
                    with self.assertRaises(DomainError) as busy:
                        contender.result(timeout=10)
                    self.assertEqual(busy.exception.code, "busy")
                    sqlite_error = busy.exception.__cause__
                    self.assertIsInstance(sqlite_error, sqlite3.OperationalError)
                    self.assertEqual(sqlite_error.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                    self.assertEqual(sqlite_error.sqlite_errorname, "SQLITE_BUSY")
                    self.assertFalse(release.is_set())
                    self.assertFalse(publisher.done())
                    self.assertTrue(other_store.read(lambda db: tuple(db.iterdump())) == before_publication,
                                    "Contended start published an attempt, revision, event or request")
                finally:
                    release.set()
                receipt = publisher.result(timeout=10)
                before_retry = self.snapshot()
                with self.assertRaises(DomainError) as denied:
                    other.execute(start)
                self.assertTrue(self.snapshot() == before_retry, "Held retry changed persisted state")
            self.assertEqual(denied.exception.code, "held")
            self.assertEqual(self.attempt(assigned).state, "assigned")
            self.assertEqual(len(self.rows("checkpoints")), 1)
            self.assertEqual(len(self.rows("holds")), 3)
            self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, receipt.committed_metadata.revision)
            print(json.dumps({"task15_contention": "actual SQLite write-lock rejection before publisher release",
                              "sqlite_errorcode": sqlite_error.sqlite_errorcode, "sqlite_errorname": sqlite_error.sqlite_errorname,
                              "contender_domain": busy.exception.code, "identical_retry_domain": denied.exception.code},
                             sort_keys=True))

    def test_interleaved_sibling_updates_preserved(self):
        a, b = self.active(), self.active("b", assignee="worker-b")
        av = replace(checkpoint(), evidence=(replace(checkpoint().evidence[0], summary="Worker A essential evidence"),))
        bv = replace(checkpoint("failed"), origin_worker="worker-b",
                     evidence=(replace(checkpoint("failed").evidence[0], summary="Worker B essential failure"),))
        commands = (self.command(a, av), self.command(b, bv))
        barrier = threading.Barrier(2)
        def publish(command):
            barrier.wait(timeout=10)
            return self.execute(command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = tuple(pool.map(publish, commands))
        self.assertEqual(sorted(r.committed_metadata.revision for r in receipts), [6, 7])
        self.assertIn("Worker A essential evidence", self.node("a")["evidence"][0])
        self.assertIn("Worker B essential failure", self.node("b")["evidence"][0])
        self.assertEqual((self.attempt(a).version, self.attempt(b).version), (3, 3))
        self.assertEqual((self.attempt(a).state, self.attempt(b).state), ("submitted", "submitted"))
        self.assertEqual(len(self.rows("checkpoints")), 2)

    def test_checkpoint_retry_after_version_increment(self):
        active = self.active()
        command = self.command(active, key="lost-checkpoint")
        first = self.execute(command)
        self.start(self.assign("b"))
        before = self.snapshot()
        retry = self.execute(command)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.committed, first.committed)
        self.assertEqual(retry.committed_metadata, first.committed_metadata)
        self.assertGreater(retry.current_metadata.revision, first.committed_metadata.revision)
        self.assertEqual(sum(e.kind == "record_checkpoint" for e in self.f.service.read_events(self.plan_id)), 1)
        self.assertTrue(self.snapshot() == before, "Exact replay refreshed checkpoint or worker activity")

    def test_exact_replay_precedes_holds_frozen_lifecycle_and_elapsed_checks(self):
        active = self.active()
        command = self.command(active)
        first = self.execute(command)
        self.edit_payload(active, lambda value: value.update(recovery_required=True))
        def altered(db):
            payload = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                            (self.plan_id,)).fetchone()[0])
            payload["definition"]["title"] = "Later current instruction"
            db.execute("UPDATE steps SET payload=? WHERE plan_id=? AND step_id='a'", (json.dumps(payload), self.plan_id))
            db.execute("INSERT INTO holds(hold_id,plan_id,reason) VALUES('replay-global',?,?)",
                       (self.plan_id, json.dumps({"kind": "reassessment", "source_id": "older", "reason": "Held"})))
        self.f.store.write(altered)
        self.f.clock.advance(10_000)
        before = self.snapshot()
        retry = self.execute(command)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.committed, first.committed)
        self.assertTrue(self.snapshot() == before)
        self.denied(replace(command, key="new-submission"), "stale")

    def test_attribution_and_fenced_replay_require_exact_authority(self):
        active = self.active()
        command = self.command(active)
        self.denied(replace(command, actor="different-worker"), "forbidden")
        self.denied(replace(command, payload={"checkpoint": replace(checkpoint(), origin_worker="different-worker")}), "forbidden")
        self.denied(replace(command, orchestrator_token=self.p.private.orchestrator_token), "invalid")
        self.denied(replace(command, expected_revision=command.attempt_version), "invalid")
        self.denied(replace(command, generation="old-generation"), "stale")
        self.denied(replace(command, epoch="old-epoch"), "stale_epoch")
        self.execute(command)
        self.f.store.write(lambda db: db.execute("UPDATE capabilities SET revoked=1 WHERE role='attempt' AND plan_id=?",
                                                 (self.plan_id,)))
        self.denied(command, "fenced")

    def test_relay_cas_attribution_and_authenticated_replay(self):
        active = self.active()
        value = checkpoint(relay="synthetic-orchestrator")
        stale = self.command(active, value)
        self.start(self.assign("b"))
        self.denied(stale, "conflict")
        command = self.command(active, value)
        first = self.execute(command)
        event = next(e for e in self.f.service.read_events(self.plan_id) if e.sequence == int(first.committed["checkpoint_id"]))
        self.assertEqual((event.actor, event.origin_worker, event.relay),
                         ("synthetic-orchestrator", "worker-a", "synthetic-orchestrator"))
        self.assertEqual(self.rows("checkpoints")[0][6:8], ("worker-a", "synthetic-orchestrator"))
        self.assertTrue(self.execute(command).replayed)
        self.denied(replace(command, actor="wrong-relay"), "forbidden")
        self.f.service.execute(self.f.command("takeover", {"expected_ownership_version": self.p.current_metadata.ownership_version,
                                                         "reason": "Explicit test takeover"}, receipt=self.p))
        self.denied(command, "fenced")

    def test_new_checkpoint_own_version_frozen_definition_and_active_only(self):
        assigned = self.assign()
        self.denied(self.command(assigned))
        self.start(assigned)
        command = self.command(assigned)
        self.denied(replace(command, attempt_version=1), "stale")
        self.edit_payload(assigned, lambda value: value["definition"].update(title="Contradictory frozen instruction"))
        self.denied(command, "stale")
        self.edit_payload(assigned, lambda value: value["definition"].update(title="Step a"))
        self.edit_payload(assigned, lambda value: value.update(recovery_required=True))
        self.denied(command, "held")

    def test_inactivity_uses_configured_inclusive_boundary_and_actual_worker_clock(self):
        active = self.active()
        small = PlanningService(self.f.store, base_url="http://127.0.0.1:8790", clock=self.f.clock, stale_seconds=5)
        large = PlanningService(self.f.store, base_url="http://127.0.0.1:8790", clock=self.f.clock, stale_seconds=6)
        self.f.clock.advance(5)
        command = self.command(active)
        self.denied(command, "stale", small)
        future = (self.f.clock() + timedelta(seconds=100)).isoformat()
        self.f.store.write(lambda db: db.execute("UPDATE plans SET updated_at=? WHERE plan_id=?", (future, self.plan_id)))
        self.execute(command, large)
        self.assertEqual(self.attempt(active).last_activity, self.f.clock().isoformat())
        self.assertEqual(self.f.service.read_plan(self.plan_id).published_at, future)

    def test_checkpoint_retains_reservations_first_step_claim_and_full_payload(self):
        self.p = self.f.create(mode="first-step", steps=(step("a"), step("b")), creation_id="first-step-checkpoint")
        self.plan_id = self.p.current_metadata.plan_id
        rid = self.f.register(workspace("/synthetic/checkpoint-workspace", 1001), self.p)
        active = self.active(execution=context(rid))
        self.edit_payload(active, lambda value: value.update(retained_extension={"essential": "keep"}))
        before_payload = self.payload(active)
        reservations = self.rows("resource_uses")
        self.f.clock.advance(1)
        self.execute(self.command(active, checkpoint("blocked")))
        after_payload = self.payload(active)
        self.assertEqual({k: v for k, v in after_payload.items() if k != "last_activity"},
                         {k: v for k, v in before_payload.items() if k != "last_activity"})
        self.assertEqual(after_payload["last_activity"], self.f.clock().isoformat())
        self.assertEqual(self.rows("resource_uses"), reservations)
        self.assertEqual(self.f.service.read_plan(self.plan_id).draft.authorization.claimed, "a")
        self.denied(self.f.command("assign_step", {"step_id": "b", "assignee": "worker-a",
                    "context": context(None, writes=False)}, receipt=self.p), "forbidden")

    def test_checkpoint_preserves_bound_input_and_checked_base_under_hold(self):
        result = seed_accepted_result(self.f, self.plan_id, "b", checkpoint().artifact)
        assigned = self.assign("c")
        from adaptive_planning.types import Availability
        evidence = replace(checkpoint().evidence[0], artifact=result.artifact)
        binding = Availability("b", result.result_version, "ancestor", "final-consumer-base", None, evidence)
        self.f.service.execute(self.f.command("bind_availability", {"bindings": (binding,), "checked_base": "final-consumer-base"},
                                             receipt=self.p, attempt=assigned))
        self.start(assigned)
        before = self.payload(assigned)
        self.f.store.write(lambda db: db.execute("INSERT INTO holds(hold_id,plan_id,reason) VALUES('bound-input-hold',?,?)",
            (self.plan_id, json.dumps({"kind": "invalidation", "step_id": "b", "source_id": "invalidated-input",
                                      "reason": "Already running worker records its safe-boundary finding"}))))
        self.execute(self.command(assigned, checkpoint("blocked")))
        self.assertEqual(self.payload(assigned)["availability_bindings"], before["availability_bindings"])
        self.assertEqual(self.payload(assigned)["context"], before["context"])
        self.assertEqual(self.payload(assigned)["definition"], before["definition"])
        self.assertEqual(len(self.rows("holds")), 1)

    def test_checkpoint_id_is_actual_global_event_sequence_after_restart(self):
        with ExitStack() as _owned_store_lifetimes:
            active = self.active()
            other = self.f.create(steps=(step("x"),), creation_id="interleaved-other-plan")
            self.f.service.execute(self.f.command("revise_plan", {"draft": self.f.service.read_plan(other.current_metadata.plan_id).draft, "reason": "Other event",
                                      "authorization_change": None, "retire_steps": ()}, receipt=other))
            receipt = self.execute(self.command(active, checkpoint(affected=("b",))))
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            event = next(e for e in reopened.service.read_events(self.plan_id) if e.kind == "record_checkpoint")
            row = reopened.store.read(lambda db: db.execute("SELECT sequence,attempt_id FROM checkpoints WHERE plan_id=?",
                                                           (self.plan_id,)).fetchone())
            self.assertEqual(receipt.committed["checkpoint_id"], str(event.sequence))
            self.assertEqual(row, (event.sequence, active.committed["attempt_id"]))
            self.assertEqual(event.payload["attempt_id"], active.committed["attempt_id"])
            self.assertEqual(event.payload["step_id"], "a")
            self.assertEqual(event.payload["definition_version"], "v1")
            self.assertEqual(event.payload["checkpoint_id"], str(event.sequence))
            self.assertEqual({h.source_id for h in reopened.service.read_plan(self.plan_id).coordination.holds}, {str(event.sequence)})

    def test_closure_is_transitive_consumers_in_current_graph_order(self):
        module = self.checkpoint_module()
        snapshot = {"steps": [{"id": "d", "dependsOn": ["c"]}, {"id": "a", "dependsOn": []},
                              {"id": "c", "dependsOn": ["b"]}, {"id": "b", "dependsOn": []}]}
        self.assertEqual(module.affected_closure(snapshot, ("b",)), ("d", "c", "b"))
        self.assertEqual(module.affected_closure(snapshot, ("a",)), ("a",))
        for malformed, seeds in ((snapshot, ("unknown",)), (snapshot, ("b", "b")), ({"steps": [{}]}, ("b",))):
            with self.subTest(seeds=seeds), self.assertRaises(DomainError) as caught:
                module.affected_closure(malformed, seeds)
            self.assertEqual(caught.exception.code, "invalid")

    def test_hold_lookup_rejects_noncanonical_wrong_plan_and_contradictory_links(self):
        module = self.checkpoint_module()
        active = self.active()
        value = checkpoint(affected=("b",))
        receipt = self.execute(self.command(active, value))
        ident = receipt.committed["checkpoint_id"]
        other = self.f.create(steps=(step("x"),), creation_id="wrong-checkpoint-plan")
        for plan_id, checkpoint_id in ((self.plan_id, "0"), (self.plan_id, "01"), (self.plan_id, "+1"),
                                       (self.plan_id, "x"), (other.current_metadata.plan_id, ident), (self.plan_id, "1")):
            before = self.snapshot()
            with self.subTest(checkpoint_id=checkpoint_id, wrong_plan=plan_id != self.plan_id), self.assertRaises(DomainError):
                self.f.store.write(lambda db: module.install_holds(db, plan_id, checkpoint_id, value.reassessment))
            self.assertTrue(self.snapshot() == before)
        def corrupt(db):
            payload = json.loads(db.execute("SELECT payload FROM events WHERE sequence=?", (int(ident),)).fetchone()[0])
            payload["attempt_id"] = "contradictory-attempt"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(payload), int(ident)))
        self.f.store.write(corrupt)
        before = self.snapshot()
        with self.assertRaises(DomainError):
            self.f.store.write(lambda db: module.install_holds(db, self.plan_id, ident, value.reassessment))
        self.assertTrue(self.snapshot() == before)

    def test_mcp_checkpoint_route_strict_payload_and_kind(self):
        from adaptive_planning.transport import build_mcp
        active = self.active()
        server = build_mcp(self.f.service)
        async def exercise():
            names = {tool.name for tool in await server.list_tools()}
            self.assertIn("record_checkpoint", names)
            raw = plans.to_jsonable(self.command(active))
            for mutation in ("checkpoint-extra", "reassessment-extra", "missing-reassessment", "bool-string"):
                bad = copy.deepcopy(raw)
                if mutation == "checkpoint-extra":
                    bad["payload"]["checkpoint"]["unknown"] = "extra field"
                elif mutation == "reassessment-extra":
                    bad["payload"]["checkpoint"]["reassessment"]["unknown"] = "extra field"
                elif mutation == "missing-reassessment":
                    del bad["payload"]["checkpoint"]["reassessment"]
                else:
                    bad["payload"]["checkpoint"]["reassessment"]["unbounded"] = "false"
                before = self.snapshot()
                response = await server.call_tool("record_checkpoint", {"command": bad})
                self.assertTrue(response.is_error)
                self.assertEqual(response.structured_content["error"]["code"], "invalid")
                self.assertTrue(self.snapshot() == before)
            wrong = await server.call_tool("report_activity", {"command": raw})
            self.assertTrue(wrong.is_error)
            result = await server.call_tool("record_checkpoint", {"command": raw})
            self.assertFalse(result.is_error, "Typed checkpoint route rejected valid authenticated publication")
            self.assertIn("checkpoint_id", result.structured_content["committed"])
        asyncio.run(exercise())
        self.assertEqual(self.attempt(active).state, "submitted")


if __name__ == "__main__":
    unittest.main()
