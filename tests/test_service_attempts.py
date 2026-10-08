"""Atomic assignment, frozen definitions and exact private grant recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from adaptive_planning import plans
from adaptive_planning.ownership import current_private, hash_token
from adaptive_planning.types import DocumentRef, DomainError, MutationReceipt, PrivateGrant, RetirementRequest, ResourceClaim
from tests.service_fixtures import context, make_service, seed_orphan_use, step, workspace


class TestService(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = make_service(Path(directory.name))
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    def rows(self, table):
        return self.f.store.read(lambda db: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall())

    def command(self, receipt=None, *, step_id="a", execution=None, key=None, assignee="worker-a"):
        return self.f.command("assign_step", {
            "step_id": step_id, "assignee": assignee,
            "context": context(None, writes=False) if execution is None else execution,
        }, receipt=self.p if receipt is None else receipt, key=key)

    def assign(self, command=None):
        try:
            return self.f.service.execute(self.command() if command is None else command)
        except DomainError as error:
            if error.code == "unsupported":
                self.fail("Assignment is not enabled")
            raise

    def revise(self, *, draft=None, retire=()):
        current = self.f.service.read_plan(self.plan_id).draft
        return self.f.command("revise_plan", {
            "draft": current if draft is None else draft, "reason": "Synthetic revision",
            "authorization_change": None, "retire_steps": tuple(retire),
        }, receipt=self.p)

    def attempt_payload(self, attempt_id):
        return self.f.store.read(lambda db: json.loads(db.execute(
            "SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
            (self.plan_id, attempt_id)).fetchone()[0]))

    def worker(self, assigned, *, relay=None):
        command = self.f.command("start_step", {
            "origin_worker": "worker-a", "relay": relay,
            "observed_activity": "2026-10-06T12:00:00+00:00",
        }, receipt=self.p, attempt=assigned)
        return replace(command, expected_revision=None if relay is None else command.expected_revision,
                       ownership_version=None if relay is None else command.ownership_version)

    def require(self, command):
        from adaptive_planning.attempts import require_attempt
        return self.f.service._run_read(lambda db: require_attempt(db, command))

    def cancel(self, assigned):
        from adaptive_planning.attempts import cancel_attempt
        self.f.service._run_write(lambda db: cancel_attempt(
            db, self.plan_id, assigned.committed["attempt_id"], "Synthetic cancellation"))

    def set_attempt(self, assigned, **fields):
        attempt_id = assigned.committed["attempt_id"]
        def mutate(db):
            data = json.loads(db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                         (self.plan_id, attempt_id)).fetchone()[0])
            data.update(fields.pop("payload", {}))
            for name, value in fields.items():
                db.execute(f"UPDATE attempts SET {name}=? WHERE plan_id=? AND attempt_id=?",
                           (value, self.plan_id, attempt_id))
            db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data), self.plan_id, attempt_id))
        self.f.store.write(mutate)

    def test_same_step_one_assignment_winner(self):
        rid = self.f.register(workspace("/synthetic/task12/race", 1201), self.p)
        commands = (self.command(execution=context(rid), key="race-a"),
                    self.command(execution=context(rid), key="race-b"))
        second = make_service(self.f.directory, self.f.clock)
        barrier = threading.Barrier(2, timeout=30)
        def assign(index):
            barrier.wait()
            try:
                return (self.f, second)[index].service.execute(commands[index])
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(assign, index) for index in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(isinstance(r, MutationReceipt) for r in results), 1)
        self.assertIn("conflict", results)
        self.assertEqual(len(self.rows("attempts")), 1)
        self.assertEqual(len([r for r in self.rows("capabilities") if r[3] == "attempt"]), 1)
        self.assertEqual(len(self.rows("resource_uses")), 1)
        self.assertEqual(sum(e.kind == "assign_step" for e in self.f.service.read_events(self.plan_id)), 1)

    def test_concurrent_exact_assignment_retry_returns_original_grant(self):
        command = self.command(key="same-key-race")
        second = make_service(self.f.directory, self.f.clock)
        barrier = threading.Barrier(2, timeout=30)
        def assign(fixture):
            barrier.wait()
            return fixture.service.execute(command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(assign, f) for f in (self.f, second)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sorted(r.replayed for r in results), [False, True])
        self.assertEqual(results[0].committed, results[1].committed)
        self.assertTrue(results[0].private == results[1].private, "Exact retry changed the grant")
        self.assertEqual(len(self.rows("attempts")), 1)
        self.assertEqual(sum(e.kind == "assign_step" for e in self.f.service.read_events(self.plan_id)), 1)

    def test_independent_workers_cross_plan_conflicts(self):
        repository = self.f.register(ResourceClaim("synthetic-host", "repository", 7, 1202,
                                                  "/synthetic/task12/repo", False, None, None), self.p)
        a = self.f.register(workspace("/synthetic/task12/worktree-a", 1203, repository_id=repository), self.p)
        b = self.f.register(workspace("/synthetic/task12/worktree-b", 1204, repository_id=repository), self.p)
        second_plan = self.f.create(creation_id="second-plan")
        first = self.assign(self.command(execution=context(a, repository_id=repository)))
        second = self.assign(self.command(second_plan, execution=context(b, repository_id=repository),
                                          assignee="worker-b"))
        self.assertNotEqual(first.committed["attempt_id"], second.committed["attempt_id"])
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(second_plan, step_id="b", execution=context(a, repository_id=repository)))
        self.assertEqual(caught.exception.code, "resource_conflict")
        self.assertEqual(self.snapshot(), before)

    def test_repository_exclusive_and_external_writable_uses_conflict(self):
        repository = self.f.register(ResourceClaim("synthetic-host", "repository", 7, 1205,
                                                  "/synthetic/task12/repo2", False, None, None), self.p)
        rid = self.f.register(workspace("/synthetic/task12/worktree-c", 1206, repository_id=repository), self.p)
        external = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                                False, None, "synthetic-shared-external"), self.p)
        other = self.f.create(creation_id="other-exclusive-plan")
        self.assign(self.command(execution=context(rid, repository_id=repository, external_ids=(external,))))
        contexts = (replace(context(None, repository_id=repository), repository_exclusive=True),
                    context(None, external_ids=(external,)))
        for execution in contexts:
            with self.subTest(kind=execution.kind):
                before = self.snapshot()
                with self.assertRaises(DomainError) as caught:
                    self.assign(self.command(other, execution=execution))
                self.assertEqual(caught.exception.code, "resource_conflict")
                self.assertEqual(self.snapshot(), before)

    def test_attempt_definition_frozen(self):
        document = DocumentRef("/synthetic/task12/spec.md", "synthetic-branch", "synthetic-commit", "doc-v1", "a")
        original = replace(step("a"), document=document)
        self.p = self.f.create(steps=(original, step("b")), creation_id="document-plan")
        self.plan_id = self.p.current_metadata.plan_id
        assigned = self.assign()
        frozen = self.attempt_payload(assigned.committed["attempt_id"])["definition"]
        self.assertEqual(frozen, plans.to_jsonable(original))
        changes = (replace(original, title="Changed title"), replace(original, dependencies=("b",)),
                   replace(original, completion_check="Changed check"),
                   replace(original, document=replace(document, version="doc-v2")),
                   replace(original, definition_version="v2"), replace(original, review_required=True),
                   replace(original, final_gate=True))
        for changed in changes:
            with self.subTest(field=changed):
                draft = replace(self.f.service.read_plan(self.plan_id).draft, steps=(changed, step("b")))
                before = self.snapshot()
                with self.assertRaises(DomainError) as caught:
                    self.f.service.execute(self.revise(draft=draft))
                self.assertEqual(caught.exception.code, "invalid")
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.attempt_payload(assigned.committed["attempt_id"])["definition"], frozen)

    def test_cancel_then_revise_reassign_preserves_old_attempt(self):
        own = self.f.register(workspace("/synthetic/task12/cancel-own", 1213), self.p)
        unrelated = self.f.register(workspace("/synthetic/task12/cancel-unrelated", 1214), self.p)
        orphan_resource = self.f.register(workspace("/synthetic/task12/cancel-orphan", 1215), self.p)
        assigned = self.assign(self.command(execution=context(own)))
        old_id = assigned.committed["attempt_id"]
        frozen = self.attempt_payload(old_id)["definition"]
        worker = self.worker(assigned)
        survivor = self.assign(self.command(step_id="b", execution=context(unrelated)))
        orphan = seed_orphan_use(self.f, orphan_resource)
        survivor_worker = self.worker(survivor)
        unchanged_uses = [r for r in self.rows("resource_uses") if r[3] != old_id]
        self.cancel(assigned)
        self.assertEqual(self.require(survivor_worker).attempt_id, survivor.committed["attempt_id"])
        self.assertEqual([r for r in self.rows("resource_uses") if r[3] != old_id], unchanged_uses)
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT status FROM resource_uses WHERE use_id=?",
                                                               (orphan.use_id,)).fetchone()[0]), "unresolved")
        draft = self.f.service.read_plan(self.plan_id).draft
        changed = replace(draft.steps[0], title="Reassigned definition", definition_version="v2")
        self.f.service.execute(self.revise(draft=replace(draft, steps=(changed, *draft.steps[1:]))))
        new = self.assign()
        self.assertNotEqual(new.committed["attempt_id"], old_id)
        self.assertEqual(self.attempt_payload(old_id)["definition"], frozen)
        with self.assertRaises(DomainError) as caught:
            self.require(worker)
        self.assertEqual(caught.exception.code, "fenced")

    def test_ready_is_advisory(self):
        rid = self.f.register(workspace("/synthetic/task12/advisory", 1207), self.p)
        command = self.command(execution=context(rid))
        ready = {r.step_id: r for r in self.f.service.list_ready(self.plan_id)}
        self.assertTrue(ready["a"].ready)
        other = self.f.create(creation_id="advisory-other")
        self.assign(self.command(other, execution=context(rid)))
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(command)
        self.assertEqual(caught.exception.code, "resource_conflict")
        self.assertEqual(self.snapshot(), before)
        orphan_resource = self.f.register(workspace("/synthetic/task12/orphan", 1211), self.p)
        orphan = seed_orphan_use(self.f, orphan_resource)
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="b", execution=context(orphan_resource)))
        self.assertEqual(caught.exception.code, "resource_conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(orphan.unresolved)

    def test_plan_only_denies_assignment_without_attempt_or_resource(self):
        planning = self.f.create("plan-only", creation_id="planning-only")
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.command(planning))
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.f.service.read_plan(planning.current_metadata.plan_id).coordination.attempts, ())

    def test_first_step_latch_is_atomic_and_survives_cancellation(self):
        self.p = self.f.create("first-step", steps=(step("a"), step("b")), creation_id="first-step")
        self.plan_id = self.p.current_metadata.plan_id
        assigned = self.assign()
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.draft.authorization.claimed, "a")
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "pending")
        self.cancel(assigned)
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="b"))
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.f.service.read_plan(self.plan_id).draft.authorization.claimed, "a")
        self.assign()

    def test_holds_stop_assignment_and_read_only_context_reserves_nothing(self):
        rid = self.f.register(workspace("/synthetic/task12/read-only", 1208), self.p)
        self.assign(self.command(execution=context(rid, writes=False)))
        self.assertEqual(self.rows("resource_uses"), [])
        for scope in (None, "b"):
            with self.subTest(scope=scope):
                self.f.store.write(lambda db: db.execute(
                    "INSERT OR REPLACE INTO holds(hold_id,plan_id,reason,active) VALUES('hold',?,?,1)",
                    (self.plan_id, json.dumps({"kind": "identity", "step_id": scope,
                                               "source_id": "synthetic", "reason": "Synthetic hold"}))))
                before = self.snapshot()
                with self.assertRaises(DomainError) as caught:
                    self.assign(self.command(step_id="b"))
                self.assertEqual(caught.exception.code, "held")
                self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute("UPDATE holds SET active=0"))
        self.f.store.write(lambda db: db.execute("UPDATE installation SET store_hold=1"))
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="b"))
        self.assertEqual(caught.exception.code, "held")

    def test_pending_and_complete_prerequisites_rechecked_inside_assignment(self):
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="c"))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute("UPDATE steps SET state='blocked' WHERE plan_id=? AND step_id='a'",
                                                (self.plan_id,)))
        with self.assertRaises(DomainError) as caught:
            self.assign()
        self.assertEqual(caught.exception.code, "invalid")
        # Bounded synthetic historical completion: definition/provenance, retained
        # evidence and the normal graph validator establish logical history only.
        provenance = DocumentRef("/synthetic/task12/history.md", "history-branch", "history-commit", "history-v1", "a")
        self.p = self.f.create(steps=(replace(step("a"), document=provenance), step("c", ("a",))),
                               creation_id="historical-prerequisite")
        self.plan_id = self.p.current_metadata.plan_id
        def complete(db):
            data = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                         (self.plan_id,)).fetchone()[0])
            data["evidence"] = ["Synthetic validated historical completion at history-commit/history-v1"]
            db.execute("UPDATE steps SET state='complete',payload=? WHERE plan_id=? AND step_id='a'",
                       (json.dumps(data), self.plan_id))
        self.f.store.write(complete)
        from adaptive_planning.core import validate_service_graph
        self.f.service._run_read(lambda db: validate_service_graph(
            plans._proposal(plans.project_plan(db, self.plan_id).legacy_snapshot), None,
            plans._validation_coordination(db, self.plan_id)))
        self.assign(self.command(step_id="c"))

    def test_contradictory_live_prerequisite_and_blank_completion_evidence_deny_assignment(self):
        assigned = self.assign()
        def complete(db, evidence):
            data = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                         (self.plan_id,)).fetchone()[0])
            data["evidence"] = evidence
            db.execute("UPDATE steps SET state='complete',payload=? WHERE plan_id=? AND step_id='a'",
                       (json.dumps(data), self.plan_id))
        self.f.store.write(lambda db: complete(db, ["Synthetic contradictory completion"]))
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="c"))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)
        self.set_attempt(assigned, state="accepted")
        self.f.store.write(lambda db: complete(db, []))
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.assign(self.command(step_id="c"))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)

    def test_event_and_request_failures_roll_back_every_assignment_effect(self):
        self.p = self.f.create("first-step", steps=(step("a"),), creation_id="rollback-plan")
        self.plan_id = self.p.current_metadata.plan_id
        rid = self.f.register(workspace("/synthetic/task12/rollback", 1209), self.p)
        command = self.command(execution=context(rid), key="rollback-assignment")
        before = self.snapshot()
        insert = plans._insert_event
        def fail_event(*args, **kwargs):
            insert(*args, **kwargs)
            raise RuntimeError("Synthetic event failure")
        with patch.object(plans, "_insert_event", side_effect=fail_event):
            with self.assertRaisesRegex(RuntimeError, "Synthetic event failure"):
                self.assign(command)
        self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute(
            "CREATE TRIGGER task12_request_failure BEFORE INSERT ON requests "
            "WHEN NEW.key='rollback-assignment' BEGIN SELECT RAISE(ABORT,'Synthetic request failure'); END"))
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Synthetic request failure"):
            self.assign(command)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.f.service.read_plan(self.plan_id).draft.authorization.claimed)
        self.f.store.write(lambda db: db.execute("DROP TRIGGER task12_request_failure"))
        self.assertFalse(self.assign(command).replayed)

    def test_assignment_token_is_private_and_hashed_only_outside_request_receipt(self):
        assigned = self.assign()
        token = assigned.private.attempt_token
        self.assertTrue(token)
        self.assertIsNone(assigned.private.orchestrator_token)
        public = [assigned.committed, self.f.service.read_plan(self.plan_id),
                  self.f.service.read_events(self.plan_id), self.f.service.read_coordination(self.plan_id),
                  self.f.service.list_ready(self.plan_id)]
        self.assertFalse(token in json.dumps(plans.to_jsonable(public)), "Private grant leaked into public data")
        self.assertFalse(token in repr(assigned), "Private grant leaked into repr")
        for table, in self.f.store.read(lambda db: db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()):
            encoded = repr(self.rows(table))
            self.assertEqual(token in encoded, table == "requests", "Private grant storage boundary changed")
        row = self.f.store.read(lambda db: db.execute(
            "SELECT secret_hash,payload FROM capabilities WHERE role='attempt'").fetchone())
        self.assertTrue(row[0] == hash_token(token), "Capability did not store the token hash")
        binding = json.loads(row[1])
        self.assertEqual(binding["attempt_id"], assigned.committed["attempt_id"])
        self.assertEqual(binding["generation"], assigned.committed_metadata.generation)

    def test_exact_replay_reopens_original_grant_before_new_command_guards(self):
        command = self.command(key="lost-response")
        assigned = self.assign(command)
        draft = self.f.service.read_plan(self.plan_id).draft
        self.f.service.execute(self.revise(draft=replace(draft, next_check="Intervening revision")))
        self.set_attempt(assigned, version=4)
        self.f.store.write(lambda db: db.execute("UPDATE plans SET ownership_version=ownership_version+1 WHERE plan_id=?",
                                                (self.plan_id,)))
        self.f.clock.advance(3600)
        before = self.snapshot()
        reopened = make_service(self.f.directory, self.f.clock)
        replay = reopened.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed, assigned.committed)
        self.assertEqual(replay.committed_metadata, assigned.committed_metadata)
        self.assertGreater(replay.current_metadata.revision, replay.committed_metadata.revision)
        self.assertGreater(replay.current_metadata.ownership_version, replay.committed_metadata.ownership_version)
        self.assertTrue(replay.private == assigned.private, "Reopen replay changed the original grant")
        self.assertEqual(self.snapshot(), before)

    def test_replay_recovery_allows_unfenced_terminal_stale_and_held_grants(self):
        command = self.command(key="state-recovery")
        assigned = self.assign(command)
        for state in ("assigned", "active", "submitted", "accepted", "stale"):
            with self.subTest(state=state):
                self.set_attempt(assigned, state=state, version=3, payload={"prior_state": "active"})
                self.f.store.write(lambda db: db.execute("UPDATE installation SET store_hold=1"))
                self.f.store.write(lambda db: db.execute(
                    "INSERT OR REPLACE INTO holds(hold_id,plan_id,reason,active) VALUES('recover-hold',?,?,1)",
                    (self.plan_id, json.dumps({"kind": "stale", "step_id": "a", "reason": "Synthetic hold"}))))
                before = self.snapshot()
                replay = self.f.service.execute(command)
                self.assertTrue(replay.private == assigned.private, "Recovery withheld an unfenced exact grant")
                self.assertEqual(self.require(replace(self.worker(assigned), attempt_version=1)).state, state)
                self.assertEqual(self.snapshot(), before)

    def test_cancelled_output_replays_public_receipt_without_replacement_grant(self):
        command = self.command(key="cancelled-output")
        assigned = self.assign(command)
        self.cancel(assigned)
        replacement = self.assign()
        before = self.snapshot()
        replay = self.f.service.execute(command)
        self.assertEqual(replay.committed, assigned.committed)
        self.assertIsNone(replay.private)
        self.assertNotEqual(replacement.committed["attempt_id"], assigned.committed["attempt_id"])
        self.assertEqual(self.snapshot(), before)

    def test_takeover_fences_assignment_replay_but_preserves_worker_capability(self):
        command = self.command(key="owner-replay")
        assigned = self.assign(command)
        direct = self.worker(assigned)
        relay = self.worker(assigned, relay="synthetic-orchestrator")
        taken = self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": 1, "reason": "Synthetic takeover"}, receipt=self.p))
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(command)
        self.assertEqual(caught.exception.code, "fenced")
        self.assertEqual(self.require(direct).attempt_id, assigned.committed["attempt_id"])
        with self.assertRaises(DomainError) as caught:
            self.require(relay)
        self.assertEqual(caught.exception.code, "fenced")
        substituted = replace(command, orchestrator_token=taken.private.orchestrator_token, ownership_version=2)
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(substituted)
        self.assertEqual(caught.exception.code, "conflict")

    def test_require_attempt_authenticates_exact_scope_and_direct_envelope(self):
        assigned = self.assign()
        worker = self.worker(assigned)
        self.assertEqual(self.require(worker).attempt_id, assigned.committed["attempt_id"])
        changed = ((replace(worker, epoch="wrong-epoch"), "stale_epoch"),
                   (replace(worker, generation="wrong-generation"), "stale"),
                   (replace(worker, plan_id="wrong-plan"), "forbidden"),
                   (replace(worker, attempt_id="wrong-attempt"), "forbidden"),
                   (replace(worker, attempt_token="synthetic-wrong-token"), "forbidden"),
                   (replace(worker, actor="other-worker"), "forbidden"),
                   (replace(worker, payload={**worker.payload, "origin_worker": "other-worker"}), "forbidden"),
                   (replace(worker, orchestrator_token=self.p.private.orchestrator_token), "invalid"),
                   (replace(worker, expected_revision=1), "invalid"),
                   (replace(worker, ownership_version=1), "invalid"))
        before = self.snapshot()
        for command, code in changed:
            with self.subTest(code=code):
                with self.assertRaises(DomainError) as caught:
                    self.require(command)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.snapshot(), before)
        self.set_attempt(assigned, version=3)
        self.assertEqual(self.require(replace(worker, attempt_version=1, definition_version="old")).version, 3)

    def test_private_grant_binding_is_exact_and_fail_closed(self):
        command = self.command(key="binding-recovery")
        assigned = self.assign(command)
        original = self.f.store.read(lambda db: db.execute(
            "SELECT capability_id,epoch,plan_id,role,valid_until,revoked,payload FROM capabilities WHERE role='attempt'").fetchone())
        cid, epoch, pid, role, expiry, revoked, raw = original
        binding = json.loads(raw)
        changes = ({"epoch": "wrong-epoch"}, {"plan_id": None}, {"role": "orchestrator"},
                   {"valid_until": "2099-01-01T00:00:00+00:00"}, {"revoked": 1},
                   {"secret_hash": "f" * 64},
                   {"payload": "not-json"}, {"payload": "[]"},
                   {"payload": json.dumps({**binding, "attempt_id": "wrong-attempt"})},
                   {"payload": json.dumps({**binding, "generation": "wrong-generation"})},
                   {"payload": json.dumps({"attempt_id": binding["attempt_id"]})},
                   {"payload": json.dumps({"generation": binding["generation"]})})
        for changed in changes:
            with self.subTest(fields=tuple(changed)):
                self.f.store.write(lambda db: db.execute(
                    "UPDATE capabilities SET epoch=?,plan_id=?,role=?,valid_until=?,revoked=?,payload=?,secret_hash=? WHERE capability_id=?",
                    (epoch, pid, role, expiry, revoked, raw, hash_token(assigned.private.attempt_token), cid)))
                def alter(db):
                    for name, value in changed.items():
                        db.execute(f"UPDATE capabilities SET {name}=? WHERE capability_id=?", (value, cid))
                self.f.store.write(alter)
                before = self.snapshot()
                self.assertIsNone(self.f.service.execute(command).private)
                self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute(
            "UPDATE capabilities SET epoch=?,plan_id=?,role=?,valid_until=?,revoked=?,payload=?,secret_hash=? WHERE capability_id=?",
            (epoch, pid, role, expiry, revoked, raw, hash_token(assigned.private.attempt_token), cid)))
        original_attempt = self.attempt_payload(assigned.committed["attempt_id"])
        for changes in ({"epoch": "wrong-epoch"}, {"generation": "wrong-generation"},
                        {"definition": {**original_attempt["definition"], "step_id": "b"}},
                        {"definition": {**original_attempt["definition"], "definition_version": "v2"}}):
            with self.subTest(attempt_fields=tuple(changes)):
                payload = {**original_attempt, **changes}
                self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                                                        (json.dumps(payload), self.plan_id, assigned.committed["attempt_id"])))
                before = self.snapshot()
                self.assertIsNone(self.f.service.execute(command).private)
                self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                                                (json.dumps(original_attempt), self.plan_id, assigned.committed["attempt_id"])))
        for state in ("rejected", "released", "replaced", "cancelled"):
            with self.subTest(state=state):
                self.set_attempt(assigned, state=state)
                self.assertIsNone(self.f.service.execute(command).private)

    def test_stored_receipt_binding_never_discloses_unrelated_or_mixed_grant(self):
        command = self.command(key="receipt-binding")
        assigned = self.assign(command)
        original = self.f.store.read(lambda db: db.execute(
            "SELECT response_payload FROM requests WHERE key=?", (command.key,)).fetchone()[0])
        data = json.loads(original)
        unrelated = self.assign(self.command(step_id="b"))
        mutations = [lambda d: d["committed"].update(attempt_id="other-attempt"),
                     lambda d: d["committed"].pop("attempt_id"),
                     lambda d: d["committed_metadata"].update(plan_id="other-plan"),
                     lambda d: d["committed_metadata"].update(epoch="other-epoch"),
                     lambda d: d["committed_metadata"].update(generation="other-generation"),
                     lambda d: d["private"].update(orchestrator_token=self.p.private.orchestrator_token),
                     lambda d: d["private"].update(orchestrator_token=self.p.private.orchestrator_token, attempt_token=None),
                     lambda d: d["private"].update(attempt_token=unrelated.private.attempt_token),
                     lambda d: d["private"].update(attempt_token="synthetic-wrong-token")]
        for mutate in mutations:
            with self.subTest(mutation=mutate.__name__):
                changed = json.loads(original)
                mutate(changed)
                self.f.store.write(lambda db: db.execute("UPDATE requests SET response_payload=? WHERE key=?",
                                                        (json.dumps(changed), command.key)))
                before = self.snapshot()
                self.assertIsNone(self.f.service.execute(command).private)
                self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute("UPDATE requests SET response_payload=? WHERE key=?", (original, command.key)))
        self.assertTrue(self.f.service.execute(command).private == assigned.private, "Valid exact binding was withheld")
        for malformed in ("not-json", "[]", "{}"):
            with self.subTest(malformed=malformed):
                self.f.store.write(lambda db: db.execute("UPDATE requests SET response_payload=? WHERE key=?",
                                                        (malformed, command.key)))
                before = self.snapshot()
                with self.assertRaises(DomainError) as caught:
                    self.f.service.execute(command)
                self.assertEqual(caught.exception.code, "invalid")
                self.assertEqual(self.snapshot(), before)

    def test_current_private_requires_exact_attempt_row_and_receipt_binding(self):
        assigned = self.assign()
        attempt_id = assigned.committed["attempt_id"]
        generation = assigned.committed_metadata.generation
        def disclose(db, **kwargs):
            return current_private(db, self.plan_id, assigned.private, **kwargs)
        self.assertIsNone(self.f.store.read(disclose))
        self.assertIsNone(self.f.store.read(lambda db: disclose(db, generation=generation, attempt_id="missing")))
        self.assertTrue(self.f.store.read(lambda db: disclose(db, generation=generation, attempt_id=attempt_id))
                        == assigned.private, "Exact current grant was withheld")
        self.f.store.write(lambda db: db.execute("DELETE FROM attempts WHERE plan_id=? AND attempt_id=?",
                                                (self.plan_id, attempt_id)))
        self.assertIsNone(self.f.store.read(lambda db: disclose(db, generation=generation, attempt_id=attempt_id)))

    def test_claimed_retirement_preserves_frozen_history_uses_and_latch(self):
        self.p = self.f.create("first-step", steps=(step("a"),), creation_id="claimed-retirement")
        self.plan_id = self.p.current_metadata.plan_id
        rid = self.f.register(workspace("/synthetic/task12/retirement", 1210), self.p)
        unresolved = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                                   False, None, "synthetic-unresolved-use"), self.p)
        released = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                                 False, None, "synthetic-released-use"), self.p)
        assigned = self.assign(self.command(execution=context(rid, external_ids=(unresolved, released))))
        self.f.store.write(lambda db: db.execute("UPDATE resource_uses SET status='unresolved' WHERE resource_id=?",
                                                (unresolved,)))
        self.f.store.write(lambda db: db.execute("UPDATE resource_uses SET status='released' WHERE resource_id=?",
                                                (released,)))
        worker = self.worker(assigned)
        frozen = self.attempt_payload(assigned.committed["attempt_id"])["definition"]
        uses = self.rows("resource_uses")
        self.f.service.execute(self.revise(retire=(RetirementRequest("a", "Synthetic retirement"),)))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "retired")
        self.assertEqual(view.draft.authorization.claimed, "a")
        self.assertEqual(self.attempt_payload(assigned.committed["attempt_id"])["definition"], frozen)
        after_uses = self.rows("resource_uses")
        self.assertEqual([(r[:6], r[7:]) for r in after_uses], [(r[:6], r[7:]) for r in uses])
        self.assertEqual({r[1]: r[6] for r in after_uses},
                         {rid: "unresolved", unresolved: "unresolved", released: "released"})
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.coordination.attempts[0].state, "released")
        payload = self.attempt_payload(assigned.committed["attempt_id"])
        self.assertEqual(payload["cancelled_from"], "assigned")
        self.assertEqual(payload["cancellation_reason"], "Synthetic retirement")
        with self.assertRaises(DomainError) as caught:
            self.require(worker)
        self.assertEqual(caught.exception.code, "fenced")

    def test_retirement_rejects_simultaneous_frozen_definition_edit(self):
        self.assign()
        draft = self.f.service.read_plan(self.plan_id).draft
        revised = replace(draft, steps=(replace(draft.steps[0], title="Changed while retiring"), *draft.steps[1:]))
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.revise(draft=revised, retire=(RetirementRequest("a", "Synthetic retirement"),)))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)

    def test_in_progress_retirement_validates_two_transitions_and_preserves_evidence(self):
        self.p = self.f.create(steps=(step("a"),), creation_id="active-retirement")
        self.plan_id = self.p.current_metadata.plan_id
        assigned = self.assign()
        self.set_attempt(assigned, state="stale", payload={"prior_state": "active", "recovery_required": True})
        def seed(db):
            payload = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                            (self.plan_id,)).fetchone()[0])
            payload["evidence"] = ["Synthetic retained evidence"]
            db.execute("UPDATE steps SET state='in-progress',payload=? WHERE plan_id=? AND step_id='a'",
                       (json.dumps(payload), self.plan_id))
        self.f.store.write(seed)
        from adaptive_planning.core import PlanValidationError, validate_plan
        before = self.f.service.read_plan(self.plan_id).legacy_snapshot
        direct = json.loads(json.dumps(before))
        direct["steps"][0].update(status="retired", retiredReason="Synthetic retirement")
        with self.assertRaises(PlanValidationError):
            validate_plan(plans._proposal(direct), plans._proposal(before))
        self.f.service.execute(self.revise(retire=(RetirementRequest("a", "Synthetic retirement"),)))
        after = self.f.service.read_plan(self.plan_id)
        self.assertEqual(after.legacy_snapshot["steps"][0]["status"], "retired")
        self.assertEqual(after.legacy_snapshot["steps"][0]["evidence"], ["Synthetic retained evidence"])
        payload = self.attempt_payload(assigned.committed["attempt_id"])
        self.assertEqual(payload["prior_state"], "active")
        self.assertEqual(payload["cancelled_from"], "stale")

    def test_claimed_retirement_batch_is_atomic_with_surviving_dependencies_and_writer_obligations(self):
        self.p = self.f.create("first-step", creation_id="claimed-batch")
        self.plan_id = self.p.current_metadata.plan_id
        rid = self.f.register(workspace("/synthetic/task12/claimed-batch", 1212), self.p)
        assigned = self.assign(self.command(execution=context(rid)))
        attempt_id = assigned.committed["attempt_id"]
        frozen = self.attempt_payload(attempt_id)["definition"]
        self.set_attempt(assigned, state="active")
        def seed(db):
            data = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'",
                                         (self.plan_id,)).fetchone()[0])
            data["evidence"] = ["Synthetic retained in-progress observation"]
            db.execute("UPDATE steps SET state='in-progress',payload=? WHERE plan_id=? AND step_id='a'",
                       (json.dumps(data), self.plan_id))
        self.f.store.write(seed)
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.revise(retire=(RetirementRequest("a", "Partial retirement"),)))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.require(self.worker(assigned)).state, "active")
        self.f.service.execute(self.revise(retire=tuple(RetirementRequest(s, "Whole retirement") for s in ("a", "b", "c"))))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual([s["status"] for s in view.legacy_snapshot["steps"]], ["retired", "retired", "retired"])
        self.assertEqual(view.legacy_snapshot["steps"][0]["evidence"], ["Synthetic retained in-progress observation"])
        self.assertEqual(view.draft.authorization.claimed, "a")
        self.assertEqual(self.attempt_payload(attempt_id)["definition"], frozen)
        self.assertEqual(view.coordination.attempts[0].state, "released")
        self.assertTrue(view.coordination.reservations[0].writer_active)
        self.assertEqual(view.status, "blocked")

    def test_completed_history_cannot_retire_or_cancel(self):
        self.p = self.f.create(steps=(step("a"),), creation_id="completed-history")
        self.plan_id = self.p.current_metadata.plan_id
        assigned = self.assign()
        self.set_attempt(assigned, state="accepted")
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.revise(retire=(RetirementRequest("a", "Synthetic retirement"),)))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)
        self.f.store.write(lambda db: db.execute("UPDATE steps SET state='complete' WHERE plan_id=? AND step_id='a'",
                                                (self.plan_id,)))
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.cancel(assigned)
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.revise(retire=(RetirementRequest("a", "Synthetic retirement"),)))
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(self.snapshot(), before)

    def test_current_attempt_is_scoped_public_lookup_and_start_stays_disabled(self):
        from adaptive_planning.attempts import current_attempt, ready_steps
        assigned = self.assign()
        view = self.f.service._run_read(lambda db: current_attempt(db, self.plan_id, "a"))
        self.assertEqual(view.attempt_id, assigned.committed["attempt_id"])
        self.assertFalse(assigned.private.attempt_token in repr(view), "Public lookup leaked grant")
        ready = self.f.service._run_read(lambda db: ready_steps(db, self.plan_id))
        self.assertEqual(ready, self.f.service.list_ready(self.plan_id))
        with self.assertRaises(DomainError) as caught:
            self.f.service._run_read(lambda db: current_attempt(db, self.plan_id, "b"))
        self.assertEqual(caught.exception.code, "not_found")
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(self.worker(assigned))
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertEqual(self.snapshot(), before)

    def test_sdk_assignment_returns_private_grant_and_secret_free_reads(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        command = self.command(key="sdk-assignment")
        async def exercise():
            result = await server.call_tool("assign_step", {"command": plans.to_jsonable(command)})
            self.assertFalse(result.is_error, "SDK assignment failed")
            receipt = result.structured_content
            token = receipt["private"]["attempt_token"]
            self.assertTrue(token)
            self.assertIsNone(receipt["private"]["orchestrator_token"])
            self.assertEqual(receipt["committed"]["version"], 1)
            for name in ("read_plan", "read_events", "read_coordination", "list_ready"):
                public = await server.call_tool(name, {"plan_id": self.plan_id})
                self.assertFalse(public.is_error, "SDK read failed")
                encoded = public.model_dump_json()
                self.assertFalse(token in encoded, "Assignment grant leaked into SDK read")
                self.assertNotIn("attempt_token", encoded)
            repeated = await server.call_tool("assign_step", {"command": plans.to_jsonable(command)})
            self.assertFalse(repeated.is_error, "SDK assignment replay failed")
            self.assertTrue(repeated.structured_content["private"] == receipt["private"], "SDK replay changed grant")
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
