"""Durable creation, semantic planning CRUD, safe retries, and bounded coordination reads."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import threading
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adaptive_planning.core import PlanValidationError, validate_published_snapshot
from adaptive_planning.types import (
    ArtifactRef,
    AuthorizationChange,
    DomainError,
    Evidence,
    RetirementRequest,
)
from tests.service_fixtures import draft, make_service, step

TABLES = (
    "plans", "steps", "attempts", "capabilities", "events", "requests", "creation_ids",
    "resource_uses", "holds", "reviews", "result_versions",
)


def count(store, table: str) -> int:
    return store.read(lambda db: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def counts(store) -> dict[str, int]:
    return {table: count(store, table) for table in TABLES}


def dump(store) -> list:
    def run(db):
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return [(n, db.execute(f"SELECT * FROM {n} ORDER BY rowid").fetchall()) for n in names
                if not n.startswith("sqlite_")]
    return store.read(run)


def set_step_state(store, plan_id: str, step_id: str, state: str, evidence: tuple[str, ...] = ()) -> None:
    def run(db):
        payload = json.loads(db.execute(
            "SELECT payload FROM steps WHERE plan_id=? AND step_id=?", (plan_id, step_id)).fetchone()[0])
        payload["evidence"] = list(evidence)
        db.execute("UPDATE steps SET state=?, payload=? WHERE plan_id=? AND step_id=?",
                   (state, json.dumps(payload), plan_id, step_id))
    store.write(run)


def insert_attempt(store, plan_id: str, attempt_id: str, step_id: str, state: str, payload: dict | None = None) -> None:
    store.write(lambda db: db.execute(
        "INSERT INTO attempts (plan_id, attempt_id, step_id, assignee, state, version, definition_version, payload) "
        "VALUES (?, ?, ?, 'worker-a', ?, 1, 'v1', ?)",
        (plan_id, attempt_id, step_id, state, json.dumps(payload or {}))))


def insert_hold(store, plan_id: str, hold_id: str, step_id: str | None, kind: str = "reassessment") -> None:
    reason = json.dumps({"kind": kind, "step_id": step_id, "source_id": "src-1", "reason": "synthetic hold"})
    store.write(lambda db: db.execute(
        "INSERT INTO holds (hold_id, plan_id, reason, active, created_at) VALUES (?, ?, ?, 1, 'ts')",
        (hold_id, plan_id, reason)))


def insert_orphan_use(store, use_id: str = "use-orphan", status: str = "unresolved") -> None:
    def run(db):
        db.execute("INSERT OR IGNORE INTO resources (resource_id, host_id, kind, status, created_at) "
                   "VALUES ('res-1', 'synthetic-host', 'workspace', 'active', 'ts')")
        db.execute("INSERT OR IGNORE INTO absent_host_work (absent_work_id, host_id, resource_id, scope, "
                   "declared_identity, status, created_at) VALUES ('absent-1', 'synthetic-host', 'res-1', "
                   "'workspace', 'worker-gone', 'unresolved', 'ts')")
        db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope, "
                   "status, created_at) VALUES (?, 'res-1', NULL, NULL, 'absent-1', 'workspace', ?, 'ts')",
                   (use_id, status))
    store.write(run)


def foreign_orphan_release(store, use_id: str = "use-orphan") -> None:
    def run(db):
        db.execute("UPDATE resource_uses SET status='released' WHERE use_id=?", (use_id,))
        db.execute("INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) "
                   "VALUES (NULL, NULL, 'release_resource_use', 'other', 'ts', '{}')")
    store.write(run)


class PlanServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.f = make_service(Path(self._directory.name))
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    def revise(self, reason="revision", *, change=None, retire=(), base=None, receipt=None, **kwargs):
        base = base if base is not None else self.f.service.read_plan(self.plan_id).draft
        return self.f.command(
            "revise_plan",
            {"draft": base, "reason": reason, "authorization_change": change, "retire_steps": tuple(retire)},
            receipt=receipt or self.p, **kwargs)

    def events(self):
        return self.f.service.read_events(self.plan_id, limit=1000)


class TestCreation(PlanServiceTestCase):
    def test_creation_id_survives_changed_request_key(self):
        first = self.f.create(key="create-one")
        retry = self.f.create(key="create-two")
        self.assertEqual(first.current_metadata.plan_id, retry.current_metadata.plan_id)
        self.assertEqual(len(self.f.service.list_plans()), 1)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.private, first.private)

    def test_creation_id_with_changed_content_conflicts(self):
        changed = dataclasses.replace(draft(), next_check="a different content check")
        command = self.f.command("create_plan", {"creation_id": "fixture-create", "draft": changed,
                                                 "new_plan_intent": "x"})
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(len(self.f.service.list_plans()), 1)

    def test_empty_store_epoch_bootstrap(self):
        with TemporaryDirectory() as directory:
            fresh = make_service(Path(directory))
            self.assertEqual(fresh.service.list_plans(), ())
            info = fresh.service.read_service_info()
            command = fresh.command("create_plan", {"creation_id": "bootstrap", "draft": draft(),
                                                    "new_plan_intent": None})
            self.assertEqual(command.epoch, info.epoch)
            receipt = fresh.service.execute(command)
            self.assertEqual(receipt.current_metadata.epoch, info.epoch)
            self.assertEqual(len(fresh.service.list_plans()), 1)

    def test_creation_requires_blank_authorization_and_supplies_server_values(self):
        for field, value in (("interval", "client-interval"), ("since_revision", 1),
                             ("baseline", ("a",)), ("claimed", "a")):
            with self.subTest(field=field):
                auth = dataclasses.replace(draft().authorization, **{field: value})
                bad = dataclasses.replace(draft(), authorization=auth, objective=f"other {field}")
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.f.command(
                        "create_plan", {"creation_id": f"bad-{field}", "draft": bad, "new_plan_intent": None}))
                self.assertEqual(ctx.exception.code, "invalid")
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.metadata.revision, 1)
        self.assertEqual(view.draft.authorization.since_revision, 1)
        self.assertEqual(str(uuid.UUID(view.draft.authorization.interval)), view.draft.authorization.interval)
        self.assertEqual(view.draft.authorization.baseline, ())
        self.assertIsNone(view.draft.authorization.claimed)
        self.assertEqual(str(uuid.UUID(view.metadata.generation)), view.metadata.generation)

    def test_duplicate_candidate_not_silent_merge(self):
        duplicate = self.f.command("create_plan", {"creation_id": "dup", "draft": draft(), "new_plan_intent": None})
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(duplicate)
        self.assertEqual(ctx.exception.code, "duplicate_candidate")
        self.assertIn(self.plan_id, ctx.exception.message)
        self.assertEqual(len(self.f.service.list_plans()), 1)
        separate = self.f.command("create_plan", {"creation_id": "dup", "draft": draft(),
                                                  "new_plan_intent": "deliberately separate"})
        receipt = self.f.service.execute(separate)
        self.assertNotEqual(receipt.current_metadata.plan_id, self.plan_id)
        self.assertEqual(len(self.f.service.list_plans()), 2)

    def test_duplicate_candidates_consider_workspace_overlap(self):
        ws = dataclasses.replace(draft(), workspace_refs=("ws-a",))
        self.f.service.execute(self.f.command(
            "create_plan", {"creation_id": "ws-a", "draft": ws, "new_plan_intent": "first workspace plan"}))
        disjoint = dataclasses.replace(draft(), workspace_refs=("ws-b",))
        self.f.service.execute(self.f.command(
            "create_plan", {"creation_id": "ws-b", "draft": disjoint, "new_plan_intent": None}))
        overlap = dataclasses.replace(draft(), workspace_refs=("ws-a", "ws-c"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.f.command(
                "create_plan", {"creation_id": "ws-ac", "draft": overlap, "new_plan_intent": None}))
        self.assertEqual(ctx.exception.code, "duplicate_candidate")

    def test_concurrent_creation_id_yields_one_plan(self):
        from adaptive_planning.app import PlanningService
        from adaptive_planning.store import Store
        other = PlanningService(Store(self.f.store.path, self.f.clock, host_id="synthetic-host"),
                                base_url="http://127.0.0.1:8790", clock=self.f.clock)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(service, key):
            command = self.f.command("create_plan", {"creation_id": "race", "draft": dataclasses.replace(
                draft(), objective="Racing objective"), "new_plan_intent": "race"}, key=key)
            barrier.wait()
            try:
                results.append(service.execute(command))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=(s, k)) for s, k in
                   ((self.f.service, "race-1"), (other, "race-2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({r.current_metadata.plan_id for r in results}), 1)
        self.assertEqual(sorted(r.replayed for r in results), [False, True])
        self.assertEqual(len(self.f.service.list_plans()), 2)

    def test_reopen_preserves_idempotency(self):
        command = self.f.command("create_plan", {"creation_id": "reopen", "draft": dataclasses.replace(
            draft(), objective="Reopen objective"), "new_plan_intent": "reopen"})
        first = self.f.service.execute(command)
        reopened = make_service(Path(self._directory.name))
        replay = reopened.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed_metadata, first.committed_metadata)
        self.assertEqual(len(reopened.service.list_plans()), 2)


class TestRevisionsAndRetries(PlanServiceTestCase):
    def test_request_replay_and_cas(self):
        command = self.revise("first revision")
        first = self.f.service.execute(command)
        self.assertFalse(first.replayed)
        self.assertEqual(first.committed_metadata.revision, 2)
        second = self.f.service.execute(self.revise("second revision"))
        self.assertEqual(second.committed_metadata.revision, 3)
        events_before = len(self.events())

        replay = self.f.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed, first.committed)
        self.assertEqual(replay.committed_metadata, first.committed_metadata)
        self.assertEqual(replay.current_metadata.revision, 3)
        self.assertEqual(len(self.events()), events_before)

        changed_payload = self.revise("changed reason", key=command.key)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(changed_payload)
        self.assertEqual(ctx.exception.code, "conflict")
        changed_kind = self.f.command("create_plan", {"creation_id": "other", "draft": draft(),
                                                      "new_plan_intent": "x"}, key=command.key)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(changed_kind)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(len(self.events()), events_before)

    def test_stale_revision_generation_and_epoch_fail_without_events(self):
        self.f.service.execute(self.revise("advance"))
        events_before = len(self.events())
        stale_revision = self.revise("stale", expected_revision=1)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(stale_revision)
        self.assertEqual(ctx.exception.code, "conflict")

        wrong_generation = dataclasses.replace(
            self.p, committed_metadata=dataclasses.replace(self.p.committed_metadata, generation="other-generation"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("generation", receipt=wrong_generation,
                                               refresh_metadata=False, expected_revision=2))
        self.assertEqual(ctx.exception.code, "stale_generation")

        wrong_epoch = dataclasses.replace(
            self.p, committed_metadata=dataclasses.replace(self.p.committed_metadata, epoch="epoch-other"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("epoch", receipt=wrong_epoch,
                                               refresh_metadata=False, expected_revision=2))
        self.assertEqual(ctx.exception.code, "stale_epoch")
        self.assertEqual(len(self.events()), events_before)

    def test_missing_expected_revision_and_unknown_plan_rejected(self):
        command = dataclasses.replace(self.revise("no cas"), expected_revision=None)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        self.assertEqual(ctx.exception.code, "invalid")
        missing = dataclasses.replace(self.revise("missing plan"), plan_id="plan-missing")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(missing)
        self.assertIn(ctx.exception.code, {"not_found", "unauthorized"})

    def test_orchestrator_capability_is_authenticated_before_mutation(self):
        events_before = len(self.events())
        for token in (None, "bogus-token"):
            with self.subTest(token=token):
                command = dataclasses.replace(self.revise("auth"), orchestrator_token=token)
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(command)
                self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertEqual(len(self.events()), events_before)

    def test_only_planning_mutations_are_enabled(self):
        command = self.f.command("sweep_stale", {}, receipt=self.p)
        before = counts(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        self.assertEqual(ctx.exception.code, "unsupported")
        self.assertEqual(counts(self.f.store), before)

    def test_revise_cannot_tamper_authorization(self):
        base = self.f.service.read_plan(self.plan_id).draft
        events_before = len(self.events())
        tampered = {
            "baseline": ("a",), "claimed": "a", "interval": "client-interval", "since_revision": 5,
            "mode": "plan-only", "note": "client rewritten note",
        }
        for field, value in tampered.items():
            with self.subTest(field=field):
                auth = dataclasses.replace(base.authorization, **{field: value})
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.revise("tamper", base=dataclasses.replace(base, authorization=auth)))
                self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(len(self.events()), events_before)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, 1)

    def test_authorization_change_renews_with_server_derived_claims(self):
        before = self.f.service.read_plan(self.plan_id).draft.authorization
        set_step_state(self.f.store, self.plan_id, "b", "complete", ("verified b",))
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "active")
        change = AuthorizationChange("first-step", "  user approved one step  ")
        self.f.service.execute(self.revise("renew", change=change))
        view = self.f.service.read_plan(self.plan_id)
        auth = view.draft.authorization
        self.assertEqual(auth.mode, "first-step")
        self.assertEqual(auth.note, "user approved one step")
        self.assertEqual(auth.baseline, ("b",))
        self.assertEqual(auth.claimed, "a")
        self.assertEqual(auth.since_revision, 2)
        self.assertNotEqual(auth.interval, before.interval)
        self.assertEqual(view.legacy_snapshot["scope"], {
            "mode": "first-step", "sinceRevision": 2, "baseline": ["b"], "claimed": "a"})
        self.assertEqual(self.events()[-1].payload["authorization_note"], "user approved one step")

    def test_first_step_renewal_rejects_two_unfinished_steps(self):
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "active")
        insert_attempt(self.f.store, self.plan_id, "att-b", "b", "assigned")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("renew", change=AuthorizationChange("first-step", "note")))
        self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, 1)

    def test_unassigned_retirement(self):
        before = self.f.service.read_plan(self.plan_id)
        command = self.f.command("revise_plan", {
            "draft": before.draft, "reason": "explicit retirement", "authorization_change": None,
            "retire_steps": (RetirementRequest("b", "synthetic obsolete route"),)}, receipt=self.p)
        self.f.service.execute(command)
        after = self.f.service.read_plan(self.plan_id)
        node = next(s for s in after.legacy_snapshot["steps"] if s["id"] == "b")
        self.assertEqual(node["status"], "retired")
        self.assertEqual(node["retiredReason"], "synthetic obsolete route")
        self.assertEqual(after.draft.authorization, before.draft.authorization)
        self.assertEqual([s.step_id for s in after.draft.steps], ["a", "b", "c"])
        self.assertEqual(after.draft.steps[1], before.draft.steps[1])
        self.assertEqual(len(after.legacy_snapshot["history"]), 2)
        self.assertNotEqual(after.legacy_snapshot["nextStepId"], "b")
        self.assertEqual(self.events()[-1].payload["retired_steps"],
                         [{"step_id": "b", "reason": "synthetic obsolete route"}])

    def test_invalid_retirements_fail_atomically(self):
        set_step_state(self.f.store, self.plan_id, "a", "complete", ("verified a",))
        insert_attempt(self.f.store, self.plan_id, "att-c", "c", "assigned")
        before_dump = dump(self.f.store)
        cases = {
            "completed": (RetirementRequest("a", "no longer needed"),),
            "assigned": (RetirementRequest("c", "no longer needed"),),
            "unknown": (RetirementRequest("zzz", "no longer needed"),),
            "blank reason": (RetirementRequest("b", "  "),),
            "duplicate": (RetirementRequest("b", "one"), RetirementRequest("b", "two")),
        }
        for name, retire in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.revise("retire", retire=retire))
                self.assertEqual(ctx.exception.code, "invalid")
                self.assertEqual(dump(self.f.store), before_dump)

    def test_retirement_with_live_dependent_fails_dag_rules_atomically(self):
        set_step_state(self.f.store, self.plan_id, "b", "pending")
        before_dump = dump(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("break dag", retire=(RetirementRequest("a", "obsolete"),)))
        self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(dump(self.f.store), before_dump)

    def test_completed_step_cannot_be_altered_or_cyclic_graph_accepted(self):
        set_step_state(self.f.store, self.plan_id, "b", "complete", ("verified b",))
        base = self.f.service.read_plan(self.plan_id).draft
        before_dump = dump(self.f.store)
        altered = tuple(dataclasses.replace(s, title="Rewritten") if s.step_id == "b" else s for s in base.steps)
        cyclic = tuple(dataclasses.replace(s, dependencies=("c",)) if s.step_id == "a" else s for s in base.steps)
        dropped = tuple(s for s in base.steps if s.step_id != "b")
        for name, steps in (("altered", altered), ("cyclic", cyclic), ("dropped", dropped)):
            with self.subTest(case=name):
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.revise(name, base=dataclasses.replace(base, steps=steps)))
                self.assertEqual(ctx.exception.code, "invalid")
                self.assertEqual(dump(self.f.store), before_dump)

    def test_revision_applies_new_steps_and_keeps_explicit_horizon(self):
        base = self.f.service.read_plan(self.plan_id).draft
        extended = dataclasses.replace(base, steps=base.steps + (step("d", ("c",)),), horizon=11)
        self.f.service.execute(self.revise("extend", base=extended))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.draft.horizon, 11)
        self.assertEqual([s["id"] for s in view.legacy_snapshot["steps"]], ["a", "b", "c", "d"])
        self.assertEqual(view.draft.steps[-1].dependencies, ("c",))

    def test_limits_without_truncation(self):
        before = counts(self.f.store)
        too_many = tuple(step(f"s{i}") for i in range(201))
        with self.assertRaises(DomainError):
            self.f.create(steps=too_many, creation_id="too-many")
        oversized = dataclasses.replace(draft(), objective="Oversized plan", facts=("x" * 1_000_000,) * 11)
        with self.assertRaises(DomainError):
            self.f.service.execute(self.f.command(
                "create_plan", {"creation_id": "oversized", "draft": oversized, "new_plan_intent": "x"}))
        self.assertEqual(counts(self.f.store), before)

        maximum = tuple(step(f"s{i}") for i in range(200))
        receipt = self.f.create(steps=maximum, creation_id="max-nodes")
        self.assertEqual(len(self.f.service.read_plan(receipt.current_metadata.plan_id).draft.steps), 200)
        explicit = dataclasses.replace(draft(), objective="Explicit horizon", horizon=11)
        receipt = self.f.service.execute(self.f.command(
            "create_plan", {"creation_id": "horizon", "draft": explicit, "new_plan_intent": "h"}))
        self.assertEqual(self.f.service.read_plan(receipt.current_metadata.plan_id).draft.horizon, 11)

    def test_oversized_coordination_row_is_never_truncated(self):
        insert_attempt(self.f.store, self.plan_id, "att-huge", "a", "active",
                       {"last_activity": "x" * (11 * 1024 * 1024)})
        with self.assertRaises(DomainError):
            self.f.service.read_coordination(self.plan_id)
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.coordination.attempts, ())
        self.assertIsNotNone(view.coordination.next_cursor)
        self.assertEqual(view.coordination.total_counts["attempts"], 1)
        self.assertTrue(view.coordination.has_live_attempts)

    def test_injected_rollback_after_projection_leaves_no_state(self):
        import adaptive_planning.plans as plans
        original = plans.project_plan

        def failing(connection, plan_id):
            original(connection, plan_id)
            raise RuntimeError("injected after projection")

        before = counts(self.f.store)
        create = self.f.command("create_plan", {"creation_id": "rollback", "draft": dataclasses.replace(
            draft(), objective="Rollback objective"), "new_plan_intent": "x"})
        revise = self.revise("rollback revise")
        with mock.patch.object(plans, "project_plan", failing):
            for command in (create, revise):
                with self.assertRaises(RuntimeError):
                    self.f.service.execute(command)
        self.assertEqual(counts(self.f.store), before)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, 1)
        self.assertFalse(self.f.service.execute(create).replayed)
        self.assertFalse(self.f.service.execute(revise).replayed)


class TestProjection(PlanServiceTestCase):
    def test_legacy_snapshot_projection_and_strict_publication(self):
        view = self.f.service.read_plan(self.plan_id)
        snapshot = view.legacy_snapshot
        self.assertEqual(set(snapshot), {
            "schemaVersion", "generation", "revision", "updatedAt", "taskId", "objective", "status",
            "executionMode", "scope", "nextStepId", "changeSummary", "steps", "history"})
        self.assertEqual(snapshot["schemaVersion"], 2)
        self.assertEqual(snapshot["taskId"], self.plan_id)
        self.assertEqual(snapshot["generation"], view.metadata.generation)
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["status"], "planning")
        self.assertEqual(snapshot["executionMode"], "complete-task")
        self.assertEqual(snapshot["scope"], {"mode": "complete-task", "sinceRevision": 1, "baseline": [],
                                              "claimed": None})
        self.assertEqual(snapshot["nextStepId"], "a")
        self.assertEqual(snapshot["steps"][2], {
            "id": "c", "title": "Step c", "status": "pending", "dependsOn": ["a"],
            "check": "Check for step c", "evidence": []})
        self.assertTrue(snapshot["changeSummary"].strip())
        self.assertEqual(snapshot["history"][-1]["summary"], snapshot["changeSummary"])
        self.assertEqual(snapshot["history"][-1]["timestamp"], snapshot["updatedAt"])
        self.assertIn("authorizationNote", snapshot["history"][0])
        self.assertEqual(view.published_at, snapshot["updatedAt"])
        self.assertEqual(view.legacy_publication, snapshot)
        validate_published_snapshot(view.legacy_publication)
        self.assertEqual(view.metadata.dashboard_url, f"http://127.0.0.1:8790/plans/{self.plan_id}")
        self.assertEqual(view.metadata, self.p.current_metadata)

    def test_history_is_bounded_to_latest_hundred_while_events_are_retained(self):
        for index in range(100):
            self.f.service.execute(self.revise(f"revision {index}"))
        view = self.f.service.read_plan(self.plan_id)
        history = view.legacy_snapshot["history"]
        self.assertEqual(view.metadata.revision, 101)
        self.assertEqual([h["revision"] for h in history], list(range(2, 102)))
        self.assertEqual(len(self.events()), 101)
        validate_published_snapshot(view.legacy_publication)

    def test_explicit_stopped_and_blocked_status_from_draft(self):
        base = self.f.service.read_plan(self.plan_id).draft
        self.f.service.execute(self.revise("stop", base=dataclasses.replace(base, stop_reason="user stopped")))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual((view.status, view.legacy_snapshot["status"]), ("stopped", "stopped"))
        self.assertEqual(view.draft.stop_reason, "user stopped")
        self.f.service.execute(self.revise("resume", base=dataclasses.replace(base, block_reason="needs input")))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.draft.block_reason, "needs input")

    def test_stopped_plan_retains_live_work_and_publication_stays_strict(self):
        set_step_state(self.f.store, self.plan_id, "a", "in-progress")
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "active")
        running = self.f.service.read_plan(self.plan_id)
        self.assertEqual(running.status, "running")
        base = running.draft
        self.f.service.execute(self.revise("stop", base=dataclasses.replace(base, stop_reason="user stopped")))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.status, "stopped")
        self.assertEqual(next(s for s in view.legacy_snapshot["steps"] if s["id"] == "a")["status"], "in-progress")
        self.assertEqual(view.coordination.attempts[0].state, "active")
        self.assertIsNone(view.legacy_publication)
        with self.assertRaises(PlanValidationError):
            validate_published_snapshot(view.legacy_snapshot)

    def test_in_progress_step_without_stop_or_backing_work_is_rejected_when_planning(self):
        set_step_state(self.f.store, self.plan_id, "a", "in-progress")
        base = self.f.service.read_plan(self.plan_id).draft
        before = counts(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("block", base=dataclasses.replace(base, block_reason="blocked")))
        self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(counts(self.f.store), before)

    def test_store_hold_and_global_hold_block_while_step_hold_is_scoped(self):
        insert_hold(self.f.store, self.plan_id, "hold-step", "b")
        view = self.f.service.read_plan(self.plan_id)
        self.assertTrue(view.coordination.has_plan_holds)
        self.assertEqual(view.status, "planning")
        ready = {r.step_id: r for r in self.f.service.list_ready(self.plan_id)}
        self.assertTrue(ready["a"].ready)
        self.assertFalse(ready["b"].ready)
        self.assertFalse(ready["c"].ready)
        insert_hold(self.f.store, self.plan_id, "hold-global", None, kind="identity")
        self.assertEqual(self.f.service.read_plan(self.plan_id).status, "blocked")
        self.assertFalse(any(r.ready for r in self.f.service.list_ready(self.plan_id)))

    def test_store_hold_blocks_every_plan(self):
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO store_hold_reasons (reason_id, epoch, kind, details, resolved, created_at) "
            "VALUES ('r1', ?, 'restore', 'restored from backup', 0, 'ts')", (self.f.epoch,)))
        self.assertTrue(self.f.service.read_service_info().store_hold)
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.status, "blocked")
        self.assertTrue(view.coordination.store_hold)

    def test_list_ready_reports_reasons_and_execution_scope(self):
        ready = {r.step_id: r for r in self.f.service.list_ready(self.plan_id)}
        self.assertEqual([k for k, r in ready.items() if r.ready], ["a", "b"])
        self.assertTrue(any("a" in reason for reason in ready["c"].reasons))
        plan_only = self.f.create(mode="plan-only", creation_id="plan-only",
                                  steps=(step("x"),)).current_metadata.plan_id
        self.assertFalse(self.f.service.list_ready(plan_only)[0].ready)
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "assigned")
        self.assertFalse({r.step_id: r for r in self.f.service.list_ready(self.plan_id)}["a"].ready)

    def test_list_plans_filters_terminal_status(self):
        done = self.f.create(creation_id="done", steps=(step("only"),)).current_metadata.plan_id
        set_step_state(self.f.store, done, "only", "complete", ("verified",))
        self.assertEqual({v.plan_id for v in self.f.service.list_plans()}, {self.plan_id})
        self.assertEqual({v.plan_id for v in self.f.service.list_plans(include_terminal=True)},
                         {self.plan_id, done})
        self.assertEqual(self.f.service.read_plan(done).status, "complete")

    def test_reads_are_copies_and_mutate_no_authority(self):
        before = dump(self.f.store)
        view = self.f.service.read_plan(self.plan_id)
        view.legacy_snapshot["steps"].append({"id": "tamper"})
        view.legacy_snapshot["history"].clear()
        self.f.service.list_plans(include_terminal=True)
        self.f.service.list_ready(self.plan_id)
        self.f.service.read_coordination(self.plan_id)
        self.f.service.read_events(self.plan_id)
        self.f.service.read_service_info()
        self.assertEqual(dump(self.f.store), before)
        again = self.f.service.read_plan(self.plan_id)
        self.assertEqual(len(again.legacy_snapshot["steps"]), 3)
        self.assertEqual(len(again.legacy_snapshot["history"]), 1)

    def test_private_grant_is_never_public(self):
        token = self.p.private.orchestrator_token
        self.assertTrue(token)
        self.assertIsNone(self.p.private.attempt_token)
        self.assertNotIn(token, json.dumps(dict(self.p.committed)))
        self.assertNotIn(token, repr(self.p))
        view = self.f.service.read_plan(self.plan_id)
        public = repr(view) + repr(self.f.service.read_events(self.plan_id)) + repr(
            self.f.service.read_coordination(self.plan_id)) + view.metadata.dashboard_url
        self.assertNotIn(token, public)
        revised = self.f.service.execute(self.revise("no grant"))
        self.assertIsNone(revised.private)
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.f.store.read(lambda db: db.execute(
            "SELECT secret_hash, role, plan_id FROM capabilities").fetchall())
        self.assertEqual(row, [(digest, "orchestrator", self.plan_id)])

    def test_read_events_validates_limits_and_pages(self):
        for index in range(3):
            self.f.service.execute(self.revise(f"event {index}"))
        page = self.f.service.read_events(self.plan_id, after=0, limit=2)
        self.assertEqual([e.plan_revision for e in page], [1, 2])
        rest = self.f.service.read_events(self.plan_id, after=page[-1].sequence, limit=10)
        self.assertEqual([e.plan_revision for e in rest], [3, 4])
        self.assertEqual(page[0].kind, "create_plan")
        for after, limit in ((-1, 10), (0, 0), (0, 1001)):
            with self.subTest(after=after, limit=limit):
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.read_events(self.plan_id, after=after, limit=limit)
                self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_events("missing")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_read_service_info_reports_installation(self):
        info = self.f.service.read_service_info()
        self.assertEqual(info.epoch, self.f.epoch)
        self.assertEqual(info.host_id, "synthetic-host")
        self.assertEqual(info.dashboard_url, "http://127.0.0.1:8790/")
        self.assertFalse(info.store_hold)
        self.assertIsNone(info.restore_cutoff)

    def test_evidence_format_is_deterministic_and_public(self):
        from adaptive_planning.plans import format_evidence
        evidence = Evidence(
            summary="Suite passed", artifact=ArtifactRef("code", "repo@abc", "v2", "sha256:1", "artifact summary"),
            workspace_id="private-workspace", check="unit tests", result="passed",
            limitations=("no windows", "no network"), blockers=("none yet",))
        text = format_evidence(evidence)
        self.assertEqual(text, format_evidence(evidence))
        for part in ("Suite passed", "unit tests", "passed", "repo@abc", "v2", "sha256:1",
                     "no windows", "no network", "none yet"):
            self.assertIn(part, text)
        self.assertNotIn("private-workspace", text)
        self.assertNotIn("None", format_evidence(dataclasses.replace(evidence, artifact=None)))


class TestCoordinationPaging(PlanServiceTestCase):
    def pages(self, limit):
        items, cursor, seen = [], None, set()
        while True:
            page = self.f.service.read_coordination(self.plan_id, after=cursor, limit=limit)
            items.extend(page.items)
            if page.next_cursor is None:
                return items
            self.assertNotIn(page.next_cursor, seen)
            self.assertTrue(page.items)
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def test_priority_rows_first_and_all_rows_reachable(self):
        for index, state in enumerate(("accepted", "released", "active", "stale", "rejected")):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", state)
        items = self.pages(limit=2)
        ids = [i.value.attempt_id for i in items if i.kind == "attempt"]
        self.assertEqual(ids[:2], ["att-2", "att-3"])
        self.assertEqual(sorted(ids), [f"att-{i}" for i in range(5)])
        first = self.f.service.read_coordination(self.plan_id, limit=2)
        self.assertEqual(first.total_counts["attempts"], 5)
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.store_sequence, 1)

    def test_summary_page_uses_authority_aggregates_not_page_rows(self):
        for index in range(3):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", "active")
        insert_hold(self.f.store, self.plan_id, "hold-1", "b")
        with mock.patch("adaptive_planning.plans.COORDINATION_SUMMARY_LIMIT", 1):
            view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(len(view.coordination.attempts) + len(view.coordination.holds), 1)
        self.assertIsNotNone(view.coordination.next_cursor)
        self.assertTrue(view.coordination.has_live_attempts)
        self.assertTrue(view.coordination.has_plan_holds)
        self.assertEqual(view.coordination.total_counts["attempts"], 3)
        self.assertEqual(view.coordination.ownership_actor, "synthetic-orchestrator")
        self.assertEqual(view.coordination.ownership_version, self.p.committed_metadata.ownership_version)

    def test_cursor_conflicts_when_revision_sequence_or_plan_changes(self):
        for index in range(3):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", "active")
        cursor = self.f.service.read_coordination(self.plan_id, limit=1).next_cursor
        self.assertEqual(len(self.f.service.read_coordination(self.plan_id, after=cursor, limit=1).items), 1)

        other = self.f.create(creation_id="other", steps=(step("o"),)).current_metadata.plan_id
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(other, after=cursor)
        self.assertEqual(ctx.exception.code, "conflict")
        # creating the other plan advanced the global sequence
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(self.plan_id, after=cursor)
        self.assertEqual(ctx.exception.code, "conflict")
        fresh = self.f.service.read_coordination(self.plan_id, limit=1).next_cursor
        self.f.service.execute(self.revise("advance revision"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(self.plan_id, after=fresh)
        self.assertEqual(ctx.exception.code, "conflict")
        for malformed in ("not-a-cursor", base64.urlsafe_b64encode(b"[1]").decode()):
            with self.assertRaises(DomainError) as ctx:
                self.f.service.read_coordination(self.plan_id, after=malformed)
            self.assertEqual(ctx.exception.code, "invalid")

    def test_read_coordination_validates_limit(self):
        for limit in (0, -1, 10_000):
            with self.assertRaises(DomainError) as ctx:
                self.f.service.read_coordination(self.plan_id, limit=limit)
            self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination("missing")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_orphan_uses_reachable_and_foreign_release_invalidates_cursor(self):
        insert_orphan_use(self.f.store)
        for index in range(2):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", "accepted")
        other = self.f.create(creation_id="other", steps=(step("o"),)).current_metadata.plan_id
        for plan in (self.plan_id, other):
            page = self.f.service.read_coordination(plan, limit=1)
            first = page.items[0]
            self.assertEqual(first.kind, "reservation")
            self.assertIsNone(first.value.plan_id)
            self.assertEqual(first.value.absent_work_id, "absent-1")
            self.assertTrue(first.value.unresolved)
            self.assertEqual(first.value.resource_ids, ("res-1",))
            self.assertEqual(page.total_counts["orphan_uses"], 1)
        view = self.f.service.read_plan(self.plan_id)
        self.assertFalse(view.coordination.has_unresolved_uses)
        self.assertEqual(view.status, "planning")

        cursor = self.f.service.read_coordination(self.plan_id, limit=1).next_cursor
        revision_before = self.f.service.read_plan(self.plan_id).metadata.revision
        foreign_orphan_release(self.f.store)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, revision_before)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(self.plan_id, after=cursor)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.f.service.read_coordination(self.plan_id).total_counts["orphan_uses"], 0)

    def test_foreign_plan_obligations_do_not_block_target_plan(self):
        other = self.f.create(creation_id="other", steps=(step("o"),)).current_metadata.plan_id
        insert_attempt(self.f.store, other, "att-o", "o", "active")
        self.f.store.write(lambda db: (
            db.execute("INSERT INTO resources (resource_id, host_id, kind, status, created_at) "
                       "VALUES ('res-o', 'synthetic-host', 'workspace', 'active', 'ts')"),
            db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, scope, status, "
                       "created_at) VALUES ('use-o', 'res-o', ?, 'att-o', 'workspace', 'unresolved', 'ts')",
                       (other,))))
        view = self.f.service.read_plan(self.plan_id)
        self.assertFalse(view.coordination.has_live_attempts)
        self.assertFalse(view.coordination.has_unresolved_uses)
        self.assertEqual(view.coordination.total_counts["reservations"], 0)
        self.assertTrue(self.f.service.read_plan(other).coordination.has_unresolved_uses)

    def test_reviews_reservations_and_invalidations_are_decoded(self):
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "submitted", {
            "prior_state": "active", "last_activity": "ts-1", "recovery_required": True,
            "context": {"kind": "read-only", "repository_id": None, "workspace_id": None,
                        "repository_exclusive": False, "external_ids": [], "writes": False,
                        "checked_base": None}})
        artifact = {"kind": "code", "identity": "repo", "version": "v1", "digest": None, "summary": "s"}
        self.f.store.write(lambda db: (
            db.execute("INSERT INTO reviews (review_id, plan_id, attempt_id, step_id, reviewer, verdict, findings, "
                       "evidence, artifact, verification_use_id, created_at) VALUES ('rev-1', ?, 'att-a', 'a', "
                       "'reviewer-x', 'passed', '[\"fine\"]', '[]', ?, NULL, 'ts')",
                       (self.plan_id, json.dumps(artifact))),
            db.execute("INSERT INTO result_versions (plan_id, result_version, step_id, identity, created_at) "
                       "VALUES (?, 'rv-1', 'a', 'repo', 'ts')", (self.plan_id,)),
            db.execute("INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) VALUES "
                       "(?, NULL, 'invalidate_result', 'orch', 'ts', ?)", (self.plan_id, json.dumps({
                           "invalidation_id": "inv-1", "producer_step_id": "a", "result_version": "rv-1",
                           "affected_steps": ["c"]})))))
        view = self.f.service.read_plan(self.plan_id)
        attempt = view.coordination.attempts[0]
        self.assertEqual((attempt.state, attempt.prior_state, attempt.last_activity, attempt.recovery_required),
                         ("submitted", "active", "ts-1", True))
        self.assertEqual(attempt.context.kind, "read-only")
        self.assertEqual(view.coordination.reviews[0].review.reviewer, "reviewer-x")
        self.assertEqual(view.coordination.reviews[0].review.findings, ("fine",))
        self.assertEqual(view.coordination.reviews[0].definition_version, "v1")
        self.assertEqual(dict(view.coordination.result_versions), {"a": "rv-1"})
        kinds = [i.kind for i in self.f.service.read_coordination(self.plan_id).items]
        self.assertIn("invalidation", kinds)


if __name__ == "__main__":
    unittest.main()
