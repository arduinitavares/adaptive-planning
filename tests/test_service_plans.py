"""Durable creation, semantic planning CRUD, safe retries, and bounded coordination reads."""
from __future__ import annotations
from contextlib import ExitStack

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

from adaptive_planning import plans
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
                   "declared_identity, status, created_at,producer_id) VALUES ('absent-1', 'synthetic-host', 'res-1', "
                   "'workspace', 'worker-gone', 'unresolved', 'ts','migration:absent-1')")
        db.execute("INSERT INTO absent_host_work_members VALUES('absent-1','res-1','workspace')")
        db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope, "
                   "status, created_at) VALUES (?, 'res-1', NULL, NULL, 'absent-1', 'workspace', ?, 'ts')",
                   (use_id, status))
        db.execute("UPDATE absent_host_work SET anchor_use_id=? WHERE absent_work_id='absent-1'",(use_id,))
        declaration=dict(format=1,producer='migration',original_parent=['absent-1','synthetic-host','res-1','workspace','worker-gone','unresolved','ts'],members=[dict(use_id=use_id,resource_id='res-1',scope='workspace',created_at='ts')])
        db.execute("INSERT INTO host_work_declarations VALUES('migration:absent-1','absent-1','migration','worker-gone','unknown',?,'ts')",(json.dumps(declaration),))
    store.write(run)


def foreign_orphan_release(store, use_id: str = "use-orphan") -> None:
    def run(db):
        db.execute("UPDATE resource_uses SET status='released' WHERE use_id=?", (use_id,))
        db.execute("INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) "
                   "VALUES (NULL, NULL, 'release_resource_use', 'other', 'ts', '{}')")
    store.write(run)


OBLIGATION_TABLES = ("attempts", "resource_uses", "holds", "reviews", "result_versions", "capabilities")


def table_rows(store, *tables: str) -> list:
    return store.read(lambda db: [
        (name, db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()) for name in tables])


def wire_bytes(dto) -> int:
    """Size of a returned DTO in canonical JSON, measured independently of the production serializer."""
    return len(json.dumps(dataclasses.asdict(dto), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8"))


def complete_all_steps(store, plan_id: str, step_ids=("a", "b", "c")) -> None:
    for step_id in step_ids:
        set_step_state(store, plan_id, step_id, "complete", ("verified",))


def insert_owned_use(store, plan_id: str, step_id: str = "b", status: str = "unresolved") -> None:
    insert_attempt(store, plan_id, f"att-use-{step_id}", step_id, "accepted")

    def run(db):
        db.execute("INSERT OR IGNORE INTO resources (resource_id, host_id, kind, status, created_at) "
                   "VALUES ('res-own', 'synthetic-host', 'workspace', 'active', 'ts')")
        db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, scope, status, "
                   "created_at) VALUES (?, 'res-own', ?, ?, 'workspace', ?, 'ts')",
                   (f"use-own-{plan_id}", plan_id, f"att-use-{step_id}", status))
    store.write(run)


def insert_store_hold(store, epoch: str) -> None:
    store.write(lambda db: db.execute(
        "INSERT INTO store_hold_reasons (reason_id, epoch, kind, details, resolved, created_at) "
        "VALUES ('r1', ?, 'restore', 'restored from backup', 0, 'ts')", (epoch,)))


class PlanServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.f = self.enterContext(make_service(Path(self._directory.name)))
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
        with TemporaryDirectory() as directory, ExitStack() as _directory_store_lifetimes_0:
            fresh = _directory_store_lifetimes_0.enter_context(make_service(Path(directory)))
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
        with ExitStack() as _owned_store_lifetimes:
            from adaptive_planning.app import PlanningService
            from adaptive_planning.store import Store
            other = PlanningService(_owned_store_lifetimes.enter_context(Store(self.f.store.path, self.f.clock, host_id="synthetic-host")),
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
        with ExitStack() as _owned_store_lifetimes:
            command = self.f.command("create_plan", {"creation_id": "reopen", "draft": dataclasses.replace(
                draft(), objective="Reopen objective"), "new_plan_intent": "reopen"})
            first = self.f.service.execute(command)
            reopened = _owned_store_lifetimes.enter_context(make_service(Path(self._directory.name)))
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
        before_dump = dump(self.f.store)
        stale_revision = self.revise("stale", expected_revision=1)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(stale_revision)
        self.assertEqual(ctx.exception.code, "conflict")

        wrong_generation = dataclasses.replace(
            self.p, committed_metadata=dataclasses.replace(self.p.committed_metadata, generation="other-generation"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("generation", receipt=wrong_generation,
                                               refresh_metadata=False, expected_revision=2))
        self.assertEqual(ctx.exception.code, "stale")
        self.assertEqual(ctx.exception.current_metadata.revision, 2)
        self.assertEqual(ctx.exception.current_metadata.generation, self.p.committed_metadata.generation)

        wrong_epoch = dataclasses.replace(
            self.p, committed_metadata=dataclasses.replace(self.p.committed_metadata, epoch="epoch-other"))
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("epoch", receipt=wrong_epoch,
                                               refresh_metadata=False, expected_revision=2))
        self.assertEqual(ctx.exception.code, "stale_epoch")
        self.assertEqual(len(self.events()), events_before)
        self.assertEqual(dump(self.f.store), before_dump)

    def test_missing_expected_revision_and_unknown_plan_rejected(self):
        command = dataclasses.replace(self.revise("no cas"), expected_revision=None)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        self.assertEqual(ctx.exception.code, "invalid")
        missing = dataclasses.replace(self.revise("missing plan"), plan_id="plan-missing")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(missing)
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_orchestrator_capability_is_authenticated_before_mutation(self):
        before_dump = dump(self.f.store)
        for token in (None, "bogus-token"):
            with self.subTest(token=token):
                command = dataclasses.replace(self.revise("auth"), orchestrator_token=token)
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(command)
                self.assertEqual(ctx.exception.code, "forbidden")
        self.assertEqual(dump(self.f.store), before_dump)

    def test_future_store_hold_command_remains_disabled(self):
        command = self.f.command("resolve_store_hold", {"backup_cutoff": "not-restored", "inventory_complete": True,
            "absent_work_inventory": (), "resource_resolutions": (), "reason": "future command remains disabled"}, receipt=self.p)
        before = counts(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        self.assertEqual(ctx.exception.code, "invalid")
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
        with self.assertRaises(DomainError) as ctx:
            self.f.create(steps=too_many, creation_id="too-many")
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        oversized = dataclasses.replace(draft(), objective="Oversized plan", facts=("x" * 1_000_000,) * 11)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.f.command(
                "create_plan", {"creation_id": "oversized", "draft": oversized, "new_plan_intent": "x"}))
        self.assertEqual(ctx.exception.code, "limit_exceeded")
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
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(self.plan_id)
        self.assertEqual(ctx.exception.code, "limit_exceeded")
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
        self.assertEqual(view.status, "blocked")
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


class TestPlanOnlyLifecycle(PlanServiceTestCase):
    def plan_only(self, name, steps=None):
        receipt = self.f.create(mode="plan-only", creation_id=f"po-{name}", steps=steps)
        return receipt, receipt.current_metadata.plan_id

    def change(self, receipt, plan_id, reason, *, change=None, retire=(), **draft_changes):
        base = dataclasses.replace(self.f.service.read_plan(plan_id).draft, **draft_changes)
        return self.f.command(
            "revise_plan",
            {"draft": base, "reason": reason, "authorization_change": change, "retire_steps": tuple(retire)},
            receipt=receipt)

    def test_plan_only_explicit_stop_and_restart(self):
        receipt, plan_id = self.plan_only("stop")
        before = self.f.service.read_plan(plan_id)
        self.f.service.execute(self.change(receipt, plan_id, "stop", stop_reason="user stopped"))
        stopped = self.f.service.read_plan(plan_id)
        self.assertEqual((stopped.status, stopped.legacy_snapshot["status"]), ("stopped", "stopped"))
        self.assertEqual(stopped.draft.stop_reason, "user stopped")
        self.assertEqual(stopped.draft.authorization, before.draft.authorization)
        self.assertIsNone(stopped.legacy_publication)
        with self.assertRaises(PlanValidationError):
            validate_published_snapshot(stopped.legacy_snapshot)

        self.f.service.execute(self.change(receipt, plan_id, "restart", stop_reason=None))
        restarted = self.f.service.read_plan(plan_id)
        self.assertEqual(restarted.status, "planning")
        self.assertIsNone(restarted.draft.stop_reason)
        self.assertEqual(restarted.draft.authorization, before.draft.authorization)
        self.assertEqual(restarted.draft.authorization.mode, "plan-only")
        validate_published_snapshot(restarted.legacy_publication)

    def test_plan_only_all_unassigned_retirement_completes_the_plan(self):
        receipt, plan_id = self.plan_only("retire")
        before = self.f.service.read_plan(plan_id)
        retire = tuple(RetirementRequest(step_id, f"obsolete {step_id}") for step_id in "abc")
        self.f.service.execute(self.change(receipt, plan_id, "retire everything", retire=retire))
        view = self.f.service.read_plan(plan_id)
        self.assertEqual(view.status, "complete")
        self.assertEqual([(s["id"], s["status"], s["retiredReason"]) for s in view.legacy_snapshot["steps"]],
                         [("a", "retired", "obsolete a"), ("b", "retired", "obsolete b"),
                          ("c", "retired", "obsolete c")])
        self.assertIsNone(view.legacy_snapshot["nextStepId"])
        self.assertEqual(view.draft.authorization, before.draft.authorization)
        self.assertIsNone(view.legacy_publication)
        self.assertNotIn(plan_id, {v.plan_id for v in self.f.service.list_plans()})
        self.assertIn(plan_id, {v.plan_id for v in self.f.service.list_plans(include_terminal=True)})

    def test_plan_only_running_stays_rejected_atomically(self):
        receipt, plan_id = self.plan_only("running-step")
        set_step_state(self.f.store, plan_id, "a", "in-progress")
        other_receipt, other_id = self.plan_only("running-attempt")
        insert_attempt(self.f.store, other_id, "att-b", "b", "active")
        for pid, rcpt in ((plan_id, receipt), (other_id, other_receipt)):
            with self.subTest(plan=pid):
                before = dump(self.f.store)
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.change(rcpt, pid, "unbacked running"))
                self.assertEqual(ctx.exception.code, "invalid")
                self.assertEqual(dump(self.f.store), before)

    def test_plan_only_stop_preserves_authority_work_and_reservations_exactly(self):
        for state in ("assigned", "active", "submitted", "stale"):
            with self.subTest(state=state):
                receipt, plan_id = self.plan_only(state, steps=(step("a"), step("b")))
                if state != "assigned":
                    set_step_state(self.f.store, plan_id, "a", "in-progress")
                insert_attempt(self.f.store, plan_id, f"att-{state}", "a", state)
                insert_owned_use(self.f.store, plan_id, "b")
                insert_hold(self.f.store, plan_id, f"hold-{state}", "b")
                obligations = table_rows(self.f.store, *OBLIGATION_TABLES)
                authorization = self.f.service.read_plan(plan_id).draft.authorization
                self.f.service.execute(self.change(receipt, plan_id, "stop", stop_reason="user stopped"))
                view = self.f.service.read_plan(plan_id)
                self.assertEqual(view.status, "stopped")
                self.assertEqual(view.draft.authorization, authorization)
                self.assertEqual(table_rows(self.f.store, *OBLIGATION_TABLES), obligations)
                self.assertEqual(view.coordination.total_counts["attempts"], 2)
                self.assertEqual(view.coordination.total_counts["reservations"], 1)
                self.assertEqual(view.coordination.total_counts["holds"], 1)

    def test_simultaneous_plan_only_renewal_rejects_unfinished_claimed_work_atomically(self):
        for state in ("assigned", "active", "submitted", "stale"):
            with self.subTest(state=state):
                receipt = self.f.create(creation_id=f"renew-{state}", steps=(step("a"), step("b")))
                plan_id = receipt.current_metadata.plan_id
                if state != "assigned":
                    set_step_state(self.f.store, plan_id, "a", "in-progress")
                insert_attempt(self.f.store, plan_id, f"att-{state}", "a", state)
                before = dump(self.f.store)
                renewal = AuthorizationChange("plan-only", "stop and hold the plan")
                with self.assertRaises(DomainError) as ctx:
                    self.f.service.execute(self.change(
                        receipt, plan_id, "stop and renew", change=renewal, stop_reason="halt"))
                self.assertEqual(ctx.exception.code, "invalid")
                self.assertEqual(dump(self.f.store), before)

                self.f.service.execute(self.change(receipt, plan_id, "stop only", stop_reason="halt"))
                view = self.f.service.read_plan(plan_id)
                self.assertEqual(view.status, "stopped")
                self.assertEqual(view.draft.authorization.mode, "complete-task")
                self.assertEqual(view.coordination.attempts[0].state, state)

    def test_plan_only_renewal_with_stop_succeeds_when_nothing_is_claimed(self):
        receipt = self.f.create(creation_id="renew-clean", steps=(step("a"), step("b")))
        plan_id = receipt.current_metadata.plan_id
        renewal = AuthorizationChange("plan-only", "pause for planning")
        self.f.service.execute(self.change(receipt, plan_id, "stop and renew", change=renewal, stop_reason="halt"))
        view = self.f.service.read_plan(plan_id)
        auth = view.draft.authorization
        self.assertEqual(view.status, "stopped")
        self.assertEqual((auth.mode, auth.since_revision, auth.baseline, auth.claimed),
                         ("plan-only", 2, (), None))

    def test_plan_only_in_progress_requires_matching_backing(self):
        cases = (
            ("stop-active", "stop", "active", None, True), ("stop-submitted", "stop", "submitted", None, True),
            ("stop-stale", "stop", "stale", None, True), ("stop-step-hold", "stop", None, "a", True),
            ("stop-global-hold", "stop", None, "*", True), ("stop-unrelated-hold", "stop", None, "b", False),
            ("stop-nothing", "stop", None, None, False),
            ("block-stale", "block", "stale", None, True), ("block-step-hold", "block", None, "a", True),
            ("block-global-hold", "block", None, "*", True), ("block-active", "block", "active", None, False),
            ("block-submitted", "block", "submitted", None, False),
            ("block-unrelated-hold", "block", None, "b", False), ("block-nothing", "block", None, None, False),
        )
        for name, lifecycle, attempt_state, hold, valid in cases:
            with self.subTest(case=name):
                receipt, plan_id = self.plan_only(name, steps=(step("a"), step("b")))
                set_step_state(self.f.store, plan_id, "a", "in-progress")
                if attempt_state:
                    insert_attempt(self.f.store, plan_id, "att-a", "a", attempt_state)
                if hold:
                    insert_hold(self.f.store, plan_id, f"hold-{plan_id}", None if hold == "*" else hold,
                                kind="identity" if hold == "*" else "reassessment")
                reason = {"stop_reason": "halt"} if lifecycle == "stop" else {"block_reason": "waiting"}
                before = dump(self.f.store)
                command = self.change(receipt, plan_id, lifecycle, **reason)
                if not valid:
                    with self.assertRaises(DomainError) as ctx:
                        self.f.service.execute(command)
                    self.assertEqual(ctx.exception.code, "invalid")
                    self.assertEqual(dump(self.f.store), before)
                    continue
                self.f.service.execute(command)
                view = self.f.service.read_plan(plan_id)
                self.assertEqual(view.status, "stopped" if lifecycle == "stop" else "blocked")
                self.assertEqual(next(s for s in view.legacy_snapshot["steps"] if s["id"] == "a")["status"],
                                 "in-progress")
                self.assertIsNone(view.legacy_publication)

    def test_plan_only_in_progress_is_backed_by_store_hold(self):
        stopping, stop_id = self.plan_only("store-stop", steps=(step("a"), step("b")))
        blocking, block_id = self.plan_only("store-block", steps=(step("a"), step("b")))
        for plan_id in (stop_id, block_id):
            set_step_state(self.f.store, plan_id, "a", "in-progress")
        insert_store_hold(self.f.store, self.f.epoch)
        self.f.service.execute(self.change(stopping, stop_id, "stop", stop_reason="halt"))
        self.f.service.execute(self.change(blocking, block_id, "block", block_reason="waiting"))
        self.assertEqual(self.f.service.read_plan(stop_id).status, "stopped")
        self.assertEqual(self.f.service.read_plan(block_id).status, "blocked")


class TestCompletionDerivation(PlanServiceTestCase):
    def terminal_plan(self, name):
        receipt = self.f.create(creation_id=f"terminal-{name}", steps=(step("a"), step("b")))
        plan_id = receipt.current_metadata.plan_id
        complete_all_steps(self.f.store, plan_id, ("a", "b"))
        return receipt, plan_id

    def noop_revision(self, receipt, plan_id, reason="no-op", **draft_changes):
        base = dataclasses.replace(self.f.service.read_plan(plan_id).draft, **draft_changes)
        return self.f.command(
            "revise_plan",
            {"draft": base, "reason": reason, "authorization_change": None, "retire_steps": ()},
            receipt=receipt)

    def test_all_terminal_graph_without_obligations_is_complete(self):
        _, plan_id = self.terminal_plan("clean")
        view = self.f.service.read_plan(plan_id)
        self.assertEqual((view.status, view.legacy_snapshot["status"]), ("complete", "complete"))

    def test_all_terminal_graph_with_owned_obligation_stays_blocked(self):
        def attempt(state):
            return lambda pid: insert_attempt(self.f.store, pid, "att-live", "a", state)

        cases = {
            "assigned attempt": attempt("assigned"), "active attempt": attempt("active"),
            "submitted attempt": attempt("submitted"), "stale attempt": attempt("stale"),
            "unresolved use": lambda pid: insert_owned_use(self.f.store, pid, "b"),
            "step hold": lambda pid: insert_hold(self.f.store, pid, f"hold-{pid}", "b"),
            "plan hold": lambda pid: insert_hold(self.f.store, pid, f"hold-{pid}", None, kind="identity"),
        }
        for name, add in cases.items():
            with self.subTest(case=name):
                receipt, plan_id = self.terminal_plan(name.replace(" ", "-"))
                add(plan_id)
                obligations = table_rows(self.f.store, *OBLIGATION_TABLES)
                events_before = len(self.f.service.read_events(plan_id, limit=1000))
                view = self.f.service.read_plan(plan_id)
                self.assertEqual((view.status, view.legacy_snapshot["status"]), ("blocked", "blocked"))
                self.assertIn(plan_id, {v.plan_id for v in self.f.service.list_plans()})
                self.assertEqual(table_rows(self.f.store, *OBLIGATION_TABLES), obligations)

                self.f.service.execute(self.noop_revision(receipt, plan_id))
                persisted = self.f.store.read(lambda db: db.execute(
                    "SELECT status FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()[0])
                self.assertEqual(persisted, "blocked")
                self.assertEqual(table_rows(self.f.store, *OBLIGATION_TABLES), obligations)
                self.assertEqual(len(self.f.service.read_events(plan_id, limit=1000)), events_before + 1)

    def test_resolved_or_terminal_obligations_do_not_block_completion(self):
        _, plan_id = self.terminal_plan("resolved")
        for index, state in enumerate(("accepted", "released", "rejected")):
            insert_attempt(self.f.store, plan_id, f"att-{index}", "a", state)
        insert_owned_use(self.f.store, plan_id, "b", status="released")
        insert_hold(self.f.store, plan_id, "hold-off", "b")
        self.f.store.write(lambda db: db.execute("UPDATE holds SET active = 0 WHERE hold_id = 'hold-off'"))
        self.assertEqual(self.f.service.read_plan(plan_id).status, "complete")

    def test_store_hold_blocks_all_terminal_graph(self):
        _, plan_id = self.terminal_plan("store-hold")
        insert_store_hold(self.f.store, self.f.epoch)
        self.assertEqual(self.f.service.read_plan(plan_id).status, "blocked")

    def test_foreign_plan_obligations_do_not_block_all_terminal_graph(self):
        _, plan_id = self.terminal_plan("target")
        foreign = self.f.create(creation_id="foreign", steps=(step("o"),)).current_metadata.plan_id
        insert_attempt(self.f.store, foreign, "att-o", "o", "active")
        insert_owned_use(self.f.store, foreign, "o")
        insert_hold(self.f.store, foreign, "hold-o", "o")
        view = self.f.service.read_plan(plan_id)
        self.assertEqual(view.status, "complete")
        self.assertEqual(view.coordination.total_counts["attempts"], 0)
        self.assertEqual(self.f.service.read_plan(foreign).status, "running")

    def test_explicit_stop_takes_precedence_over_blocking_obligations(self):
        receipt, plan_id = self.terminal_plan("stop")
        insert_attempt(self.f.store, plan_id, "att-live", "a", "active")
        insert_hold(self.f.store, plan_id, "hold-1", "b")
        obligations = table_rows(self.f.store, *OBLIGATION_TABLES)
        self.f.service.execute(self.noop_revision(receipt, plan_id, "stop", stop_reason="user stopped"))
        self.assertEqual(self.f.service.read_plan(plan_id).status, "stopped")
        self.assertEqual(table_rows(self.f.store, *OBLIGATION_TABLES), obligations)


class TestHistoryProvenance(PlanServiceTestCase):
    def test_history_projects_actual_event_summaries_and_timestamps(self):
        for reason in ("alpha", "beta"):
            self.f.clock.advance(60)
            self.f.service.execute(self.revise(reason))
        history = self.f.service.read_plan(self.plan_id).legacy_snapshot["history"]
        self.assertEqual([(h["revision"], h["timestamp"], h["summary"]) for h in history], [
            (1, "2026-10-06T12:00:00+00:00", "Plan created"),
            (2, "2026-10-06T12:01:00+00:00", "alpha"),
            (3, "2026-10-06T12:02:00+00:00", "beta")])

    def insert_event_rows(self, revision, rows):
        def run(db):
            for published_at, summary, note in rows:
                payload = {"summary": summary}
                if note is not None:
                    payload["authorization_note"] = note
                db.execute(
                    "INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) "
                    "VALUES (?, ?, 'revise_plan', 'fixture', ?, ?)",
                    (self.plan_id, revision, published_at, json.dumps(payload)))
        self.f.store.write(run)

    def test_same_revision_events_stay_separate_with_row_owned_notes(self):
        self.f.clock.advance(60)
        self.f.service.execute(self.revise("alpha"))
        self.insert_event_rows(2, (
            ("2026-10-06T12:05:00+00:00", "beta", "note for beta only"),
            ("2026-10-06T12:06:00+00:00", "gamma", None),
            ("2026-10-06T12:07:00+00:00", "delta", None)))
        before = dump(self.f.store)
        view = self.f.service.read_plan(self.plan_id)
        history = view.legacy_snapshot["history"]
        self.assertEqual([(h["revision"], h["timestamp"], h["summary"], h.get("authorizationNote")) for h in history], [
            (1, "2026-10-06T12:00:00+00:00", "Plan created", draft().authorization.note),
            (2, "2026-10-06T12:01:00+00:00", "alpha", None),
            (2, "2026-10-06T12:05:00+00:00", "beta", "note for beta only"),
            (2, "2026-10-06T12:06:00+00:00", "gamma", None),
            (2, "2026-10-06T12:07:00+00:00", "delta", None)])
        self.assertEqual(view.legacy_snapshot["changeSummary"], "delta")
        self.assertIsNone(view.legacy_publication)
        self.assertEqual(len(self.events()), 5)
        self.assertEqual(dump(self.f.store), before)

    def test_history_is_latest_hundred_events_by_sequence_when_revisions_repeat(self):
        expected = []
        first_rows = tuple((f"2026-10-06T12:{i // 60:02d}:{i % 60:02d}+00:00", f"early {i}",
                            "early note" if i % 25 == 0 else None) for i in range(40))
        self.insert_event_rows(1, first_rows)
        self.f.clock.advance(3600)
        self.f.service.execute(self.revise("real revision two"))
        late_rows = tuple((f"2026-10-06T14:{i // 60:02d}:{i % 60:02d}+00:00", f"late {i}",
                           "late note" if i % 30 == 0 else None) for i in range(80))
        self.insert_event_rows(2, late_rows)
        expected = [(1, ts, summary, note) for ts, summary, note in first_rows]
        expected.append((2, "2026-10-06T13:00:00+00:00", "real revision two", None))
        expected.extend((2, ts, summary, note) for ts, summary, note in late_rows)
        expected = expected[-100:]
        before = dump(self.f.store)
        view = self.f.service.read_plan(self.plan_id)
        history = view.legacy_snapshot["history"]
        self.assertEqual(len(self.events()), 122)
        self.assertEqual(len(history), 100)
        self.assertEqual([(h["revision"], h["timestamp"], h["summary"], h.get("authorizationNote")) for h in history],
                         expected)
        self.assertEqual(history[0]["summary"], "early 21")
        self.assertEqual(history[-1]["summary"], "late 79")
        self.assertIsNone(view.legacy_publication)
        self.assertEqual(dump(self.f.store), before)

    def test_missing_intermediate_event_is_omitted_never_invented(self):
        for reason in ("first", "second", "third"):
            self.f.clock.advance(60)
            self.f.service.execute(self.revise(reason))
        self.f.store.write(lambda db: db.execute(
            "DELETE FROM events WHERE plan_id = ? AND plan_revision = 2", (self.plan_id,)))
        before = dump(self.f.store)
        view = self.f.service.read_plan(self.plan_id)
        history = view.legacy_snapshot["history"]
        self.assertEqual([(h["revision"], h["summary"]) for h in history],
                         [(1, "Plan created"), (3, "second"), (4, "third")])
        self.assertEqual([h["timestamp"] for h in history], [
            "2026-10-06T12:00:00+00:00", "2026-10-06T12:02:00+00:00", "2026-10-06T12:03:00+00:00"])
        self.assertIsNone(view.legacy_publication)
        self.assertEqual(dump(self.f.store), before)

    def test_missing_current_revision_event_is_a_structured_error_without_repair(self):
        self.f.service.execute(self.revise("second"))
        self.f.store.write(lambda db: db.execute(
            "DELETE FROM events WHERE plan_id = ? AND plan_revision = 2", (self.plan_id,)))
        before = dump(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_plan(self.plan_id)
        self.assertEqual(ctx.exception.code, "history_incomplete")
        with self.assertRaises(DomainError) as ctx:
            self.f.service.list_plans()
        self.assertEqual(ctx.exception.code, "history_incomplete")
        self.assertEqual(dump(self.f.store), before)


class TestServiceContext(PlanServiceTestCase):
    def read_outside_context(self):
        return self.f.store.read(lambda db: plans.project_plan(db, self.plan_id))

    def test_domain_handlers_fail_outside_service_context_without_mutation(self):
        before = dump(self.f.store)
        with self.assertRaises(RuntimeError):
            self.read_outside_context()
        command = self.revise("outside context")
        with self.assertRaises(RuntimeError):
            self.f.store.write(lambda db: plans.revise_plan(db, command))
        self.assertEqual(dump(self.f.store), before)

    def test_service_calls_use_the_injected_base_url_and_clock(self):
        self.f.clock.advance(90)
        self.f.service.execute(self.revise("clocked"))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.metadata.dashboard_url, f"http://127.0.0.1:8790/plans/{self.plan_id}")
        self.assertEqual(view.published_at, "2026-10-06T12:01:30+00:00")

    def test_service_context_is_restored_after_success_failure_and_nesting(self):
        with plans.service_context("http://outer", self.f.clock):
            with plans.service_context("http://inner", self.f.clock):
                self.assertEqual(self.read_outside_context().metadata.dashboard_url,
                                 f"http://inner/plans/{self.plan_id}")
            self.assertEqual(self.read_outside_context().metadata.dashboard_url,
                             f"http://outer/plans/{self.plan_id}")
        with self.assertRaises(RuntimeError):
            self.read_outside_context()
        with self.assertRaises(ValueError):
            with plans.service_context("http://failing", self.f.clock):
                raise ValueError("failure inside context")
        with self.assertRaises(RuntimeError):
            self.read_outside_context()

    def test_canonical_serialization_needs_no_service_context(self):
        command = self.revise("serialized")
        self.assertEqual(plans.canonical_bytes({"b": (1, 2), "a": "é"}), '{"a":"é","b":[1,2]}'.encode("utf-8"))
        self.assertEqual(plans.command_digest(command), plans.sha256_hex(plans.canonical_bytes(command)))
        self.assertEqual(len(plans.command_digest(command)), 64)


class TestResponseBounds(PlanServiceTestCase):
    LIMIT = "adaptive_planning.plans.MAX_PAGE_BYTES"

    def test_full_graph_over_ten_megabytes_fails_the_mutation_atomically(self):
        big = tuple(dataclasses.replace(step(f"s{i}"), title="t" * 10000, completion_check="c" * 10000)
                    for i in range(200))
        base = self.f.service.read_plan(self.plan_id).draft
        before = dump(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(self.revise("huge graph", base=dataclasses.replace(base, steps=big)))
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        self.assertEqual(dump(self.f.store), before)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, 1)

    def test_read_plan_counts_the_complete_serialized_view_at_the_boundary(self):
        view = self.f.service.read_plan(self.plan_id)
        self.assertIsNotNone(view.legacy_publication)
        size = wire_bytes(view)
        with mock.patch(self.LIMIT, size):
            self.assertEqual(wire_bytes(self.f.service.read_plan(self.plan_id)), size)
        with mock.patch(self.LIMIT, size - 1):
            with self.assertRaises(DomainError) as ctx:
                self.f.service.read_plan(self.plan_id)
        self.assertEqual(ctx.exception.code, "limit_exceeded")

    def test_read_plan_returns_only_rows_that_fit_the_remaining_budget(self):
        for index in range(3):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", "active")
        full = self.f.service.read_plan(self.plan_id)
        self.assertEqual(len(full.coordination.attempts), 3)
        self.assertIsNone(full.coordination.next_cursor)
        full_size = wire_bytes(full)
        cursor = self.f.service.read_coordination(self.plan_id, limit=1).next_cursor
        summary_only = dataclasses.replace(
            full, coordination=dataclasses.replace(full.coordination, attempts=(), next_cursor=cursor))
        taken = []
        for limit in range(wire_bytes(summary_only) + 8, full_size + 1):
            with mock.patch(self.LIMIT, limit):
                view = self.f.service.read_plan(self.plan_id)
            self.assertLessEqual(wire_bytes(view), limit)
            self.assertEqual(view.coordination.next_cursor is None, len(view.coordination.attempts) == 3)
            taken.append(len(view.coordination.attempts))
        self.assertEqual(taken, sorted(taken))
        self.assertEqual((taken[0], taken[-1]), (0, 3))

        graph_only = wire_bytes(dataclasses.replace(full, coordination=dataclasses.replace(
            full.coordination, attempts=())))
        with mock.patch(self.LIMIT, graph_only):
            with self.assertRaises(DomainError) as ctx:
                self.f.service.read_plan(self.plan_id)
        self.assertEqual(ctx.exception.code, "limit_exceeded")

    def test_coordination_page_counts_its_envelope_and_always_progresses(self):
        for index in range(3):
            insert_attempt(self.f.store, self.plan_id, f"att-{index}", "a", "active")
        one_row = wire_bytes(self.f.service.read_coordination(self.plan_id, limit=1))
        everything = self.f.service.read_coordination(self.plan_id, limit=500)
        self.assertEqual(len(everything.items), 3)
        self.assertIsNone(everything.next_cursor)
        for limit in range(one_row - 5, wire_bytes(everything) + 1):
            with mock.patch(self.LIMIT, limit):
                if limit < one_row:
                    with self.assertRaises(DomainError) as ctx:
                        self.f.service.read_coordination(self.plan_id, limit=500)
                    self.assertEqual(ctx.exception.code, "limit_exceeded")
                    continue
                page = self.f.service.read_coordination(self.plan_id, limit=500)
            self.assertLessEqual(wire_bytes(page), limit)
            self.assertTrue(page.items)
            self.assertEqual(page.next_cursor is None, len(page.items) == 3)

        with mock.patch(self.LIMIT, one_row):
            cursor, seen_cursors, ids = None, set(), []
            while True:
                page = self.f.service.read_coordination(self.plan_id, after=cursor, limit=500)
                self.assertTrue(page.items)
                ids.extend(item.value.attempt_id for item in page.items)
                if page.next_cursor is None:
                    break
                self.assertNotIn(page.next_cursor, seen_cursors)
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor
        self.assertEqual(ids, ["att-0", "att-1", "att-2"])

    def test_strict_page_rejects_an_unfit_first_row_instead_of_repeating_its_cursor(self):
        insert_attempt(self.f.store, self.plan_id, "att-small", "a", "active")
        insert_attempt(self.f.store, self.plan_id, "att-huge", "a", "active",
                       {"last_activity": "x" * (11 * 1024 * 1024)})
        first = self.f.service.read_coordination(self.plan_id)
        self.assertEqual([i.value.attempt_id for i in first.items], ["att-small"])
        self.assertIsNotNone(first.next_cursor)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_coordination(self.plan_id, after=first.next_cursor)
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        summary = self.f.service.read_plan(self.plan_id)
        self.assertEqual([a.attempt_id for a in summary.coordination.attempts], ["att-small"])
        self.assertIsNotNone(summary.coordination.next_cursor)
        self.assertLessEqual(wire_bytes(summary), 10 * 1024 * 1024)

    def test_oversized_authority_yields_a_structured_size_error(self):
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO result_versions (plan_id, result_version, step_id, identity, created_at) "
            "VALUES (?, ?, 'a', 'repo', 'ts')", (self.plan_id, "rv-" + "x" * (11 * 1024 * 1024))))
        before = dump(self.f.store)
        with self.assertRaises(DomainError) as ctx:
            self.f.service.read_plan(self.plan_id)
        self.assertEqual(ctx.exception.code, "limit_exceeded")
        self.assertEqual(dump(self.f.store), before)


if __name__ == "__main__":
    unittest.main()
