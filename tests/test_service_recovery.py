"""Explicit stale ownership recovery preserves evidence and physical obligations."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from adaptive_planning.app import PlanningService
from adaptive_planning.types import DomainError, Evidence, ResourceClaim, Review
from tests.service_fixtures import checkpoint, context, make_service, step, workspace


class TestService(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = make_service(Path(directory.name))
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    def view(self):
        return self.f.service.read_plan(self.plan_id)

    def command(self, kind, payload, *, attempt=None, receipt=None, **kwargs):
        command = self.f.command(kind, payload, receipt=receipt or self.p, attempt=attempt, **kwargs)
        if kind == "record_checkpoint" and payload["checkpoint"].relay is not None:
            command = replace(command, actor=payload["checkpoint"].relay)
        if kind in ("start_step", "report_activity", "record_checkpoint") and command.orchestrator_token is None:
            command = replace(command, expected_revision=None, ownership_version=None)
        return command

    def state(self):
        # Private capability/receipt content is compared by digest, never assertion output.
        return self.f.store.read(lambda db: hashlib.sha256(repr(tuple(db.iterdump())).encode()).hexdigest())

    def payload(self, attempt):
        return self.f.store.read(lambda db: json.loads(db.execute(
            "SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
            (self.plan_id, attempt.committed["attempt_id"])).fetchone()[0]))

    def refused(self, command, code):
        before = self.state()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(command)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.state(), before)

    def sweep(self):
        return self.f.service.execute(self.command("sweep_stale", {}))

    def resolve_command(self, attempt, resolution="confirm-owner", **changes):
        payload = {"resolution": resolution, "reason": "Synthetic observed owner explicitly confirms ownership",
                   "next_step_state": "pending", "stopped_resource_ids": (), "replacement_assignee": None,
                   "replacement_context": None, "attached_context": None, **changes}
        return self.command("resolve_attempt", payload, attempt=attempt)

    def live(self, *, execution=None, step_id="a"):
        assigned = self.f.service.execute(self.command("assign_step", {
            "step_id": step_id, "assignee": "worker-a", "context": execution or context(None, writes=False)}))
        return self.f.start(self.p, assigned)

    def stale(self, *, execution=None):
        active = self.live(execution=execution)
        self.f.clock.advance(901)
        self.sweep()
        return active

    def review(self, value, step_id="a"):
        return self.f.service.execute(self.command("record_review", {"step_id": step_id,
            "review": Review("synthetic-reviewer", value.artifact, "passed", (), value.evidence, None)}))

    def reserve_command(self, attempt, execution, *, handoff=None):
        return self.command("reserve_verification", {"step_id": "a", "verifier": "synthetic-verifier",
            "context": execution, "handoff_from": handoff,
            "stopped_reason": None if handoff is None else "Observed whole implementation actor stopped"}, attempt=attempt)

    def release(self, attempt, use_id, resource_ids):
        return self.f.service.execute(self.command("release_resource_use", {"use_id": use_id,
            "resource_ids": tuple(resource_ids), "reason": "Synthetic explicit stopped-use observation",
            "stopped": True}, attempt=attempt))

    def test_stale_heartbeat_does_not_reactivate(self):
        # Removing the public sweep route or its retained stale transition breaks this test.
        active = self.f.start(self.p, self.f.assign(self.p))
        before = self.f.service.read_plan(self.plan_id)
        self.f.clock.advance(901)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata, before.metadata)
        command = self.f.command("sweep_stale", {}, receipt=self.p)
        try:
            swept_receipt = self.f.service.execute(command)
        except DomainError as error:
            self.fail(f"Explicit sweep_stale must record stale ownership; actual route returned {error.code}")
        swept = self.f.service.read_plan(self.plan_id)
        attempt = swept.coordination.attempts[0]
        self.assertEqual((attempt.state, attempt.prior_state), ("stale", "active"))
        self.assertEqual(attempt.version, before.coordination.attempts[0].version + 1)
        self.assertEqual(attempt.last_activity, before.coordination.attempts[0].last_activity)
        self.assertEqual(swept.metadata.revision, before.metadata.revision + 1)
        self.assertEqual(swept_receipt.committed["attempt_ids"], (active.committed["attempt_id"],))
        self.assertEqual(len(swept.coordination.holds), 1)
        self.assertEqual(swept.coordination.holds[0].kind, "stale")
        heartbeat = self.f.command("report_activity", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "returned",
        }, receipt=self.p, attempt=active)
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(replace(heartbeat, expected_revision=None, ownership_version=None))
        self.assertEqual(error.exception.code, "stale")
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata, swept.metadata)
        self.f.service.execute(self.f.command("sweep_stale", {}, receipt=self.p))
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata, swept.metadata)

    def test_custom_inclusive_batch_sweep_preserves_all_three_states(self):
        self.f.service = PlanningService(self.f.store, base_url="http://127.0.0.1:8790",
                                         clock=self.f.clock, stale_seconds=6)
        self.p = self.f.create(steps=(step("a"), step("b"), step("c")), creation_id="batch")
        self.plan_id = self.p.current_metadata.plan_id
        assigned = self.f.assign(self.p, "a")
        active = self.live(step_id="b")
        submitted = self.live(step_id="c")
        self.f.submit(self.p, submitted, checkpoint())
        before = self.view()
        before_payloads = {a.attempt_id: self.payload(replace(assigned, committed={"attempt_id": a.attempt_id}))
                           for a in before.coordination.attempts}
        self.f.clock.advance(5)
        self.sweep()
        self.assertEqual(self.view().metadata, before.metadata)
        self.f.clock.advance(1)
        result = self.sweep()
        view = self.view()
        self.assertEqual(set(result.committed["attempt_ids"]), {a.attempt_id for a in before.coordination.attempts})
        self.assertEqual(view.metadata.revision, before.metadata.revision + 1)
        self.assertEqual(view.coordination.store_sequence, before.coordination.store_sequence + 1)
        self.assertEqual({a.prior_state for a in view.coordination.attempts}, {"assigned", "active", "submitted"})
        self.assertEqual(len(view.coordination.holds), 3)
        self.assertEqual({hold.kind for hold in view.coordination.holds}, {"stale"})
        for attempt in view.coordination.attempts:
            original = next(a for a in before.coordination.attempts if a.attempt_id == attempt.attempt_id)
            self.assertEqual((attempt.state, attempt.version, attempt.last_activity),
                             ("stale", original.version + 1, original.last_activity))
            payload = self.payload(replace(assigned, committed={"attempt_id": attempt.attempt_id}))
            for key, value in before_payloads[attempt.attempt_id].items():
                if key != "recovery_required":
                    self.assertEqual(payload[key], value)
        self.assertEqual(view.draft.authorization, before.draft.authorization)
        self.assertEqual(len([e for e in self.f.service.read_events(self.plan_id) if e.kind == "sweep_stale"]), 1)

    def test_unswept_progress_and_checkpoint_cannot_reset_elapsed_ownership(self):
        active = self.live()
        self.f.clock.advance(900)
        for kind, payload in (("report_activity", {"origin_worker": "worker-a", "relay": None,
                                                  "observed_activity": "late heartbeat"}),
                              ("record_checkpoint", {"checkpoint": checkpoint()})):
            self.refused(self.command(kind, payload, attempt=active), "stale")
        before = self.state()
        self.f.service.read_plan(self.plan_id)
        self.f.service.list_ready(self.plan_id)
        self.f.service.read_coordination(self.plan_id)
        self.f.service.read_events(self.plan_id)
        self.assertEqual(self.state(), before)
        self.f.register(workspace("/synthetic/recovery/unrelated", 2101), self.p)
        self.assertEqual(self.view().coordination.attempts[0].state, "active")
        self.assertFalse(self.view().coordination.holds)

    def test_malformed_activity_aborts_entire_explicit_plan_batch_without_writes(self):
        self.p = self.f.create(steps=(step("a"), step("b")), creation_id="malformed-sweep")
        self.plan_id = self.p.current_metadata.plan_id
        self.f.assign(self.p, "a")
        second = self.f.assign(self.p, "b")
        self.f.clock.advance(900)
        for activity in (None, "not-an-activity-time", "2026-10-06T12:00:00"):
            def corrupt(db):
                row = db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                 (self.plan_id, second.committed["attempt_id"])).fetchone()
                data = json.loads(row[0])
                if activity is None:
                    data.pop("last_activity", None)
                else:
                    data["last_activity"] = activity
                db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                           (json.dumps(data), self.plan_id, second.committed["attempt_id"]))
            self.f.store.write(corrupt)
            self.refused(self.command("sweep_stale", {}), "invalid")
            self.assertEqual([a.state for a in self.view().coordination.attempts], ["assigned", "assigned"])
            self.assertFalse(self.view().coordination.holds)

    def test_recovery_checkpoint_is_event_only_and_confirmation_preserves_original_review(self):
        self.p = self.f.create(steps=(step("a", review_required=True),), creation_id="review-cycle")
        self.plan_id = self.p.current_metadata.plan_id
        active = self.live()
        value = checkpoint()
        submitted = self.f.submit(self.p, active, value)
        original_review = self.review(value)
        self.f.clock.advance(900)
        self.sweep()
        before = self.view()
        self.assertEqual([hold.kind for hold in before.coordination.holds], ["stale"])
        prior_payload = self.payload(active)
        prior_step = before.legacy_snapshot["steps"][0]
        recovery = self.f.submit(self.p, active, value)
        after = self.view()
        self.assertTrue(recovery.committed["recovery_only"])
        self.assertIn("recovery_event_id", recovery.committed)
        self.assertNotIn("checkpoint_id", recovery.committed)
        self.assertEqual(after.coordination.attempts[0].version, before.coordination.attempts[0].version + 1)
        self.assertEqual(after.coordination.attempts[0].last_activity, before.coordination.attempts[0].last_activity)
        self.assertEqual(after.legacy_snapshot["steps"][0], prior_step)
        self.assertEqual(after.coordination.holds, before.coordination.holds)
        self.assertEqual(self.payload(active), prior_payload)
        rows = self.f.store.read(lambda db: db.execute("SELECT sequence FROM checkpoints WHERE plan_id=?", (self.plan_id,)).fetchall())
        self.assertEqual(rows, [(int(submitted.committed["checkpoint_id"]),)])
        self.f.service.execute(self.resolve_command(active))
        self.assertEqual(self.view().coordination.attempts[0].state, "submitted")
        self.assertFalse(self.view().coordination.holds)
        accepted = self.f.accept(self.p, active, value)
        self.assertEqual(self.view().coordination.attempts[0].state, "accepted")
        event = next(e for e in self.f.service.read_events(self.plan_id) if e.kind == "accept_step")
        self.assertEqual(event.payload["checkpoint_sequence"], int(submitted.committed["checkpoint_id"]))
        self.assertEqual(event.payload["review_id"], original_review.committed["review_id"])
        self.assertEqual(accepted.committed["attempt_id"], active.committed["attempt_id"])

    def test_recovery_material_holds_survive_confirmation_and_resolve_exact_source(self):
        active = self.stale()
        self.assertEqual([hold.kind for hold in self.view().coordination.holds], ["stale"])
        report = self.f.submit(self.p, active, checkpoint(affected=("a",)))
        source = report.committed["recovery_event_id"]
        holds = self.view().coordination.holds
        self.assertEqual({hold.kind for hold in holds}, {"stale", "reassessment"})
        self.assertEqual({hold.source_id for hold in holds if hold.kind == "reassessment"}, {source})
        self.assertEqual({h.step_id for h in holds if h.kind == "reassessment"}, {"a", "c"})
        self.f.service.execute(self.resolve_command(active))
        self.assertEqual({h.kind for h in self.view().coordination.holds}, {"reassessment"})
        command = self.command("resolve_reassessment", {"checkpoint_id": source, "decision": "reject",
            "reason": "Observed effects explicitly resolved", "draft": None, "affected_steps": (), "unbounded": False})
        self.f.service.execute(command)
        self.assertFalse(self.view().coordination.holds)
        self.assertEqual(self.view().coordination.attempts[0].state, "active")

    def test_recovery_relay_has_original_attribution_and_no_fabricated_activity(self):
        active = self.stale()
        before = self.view().coordination.attempts[0]
        relay = "synthetic-orchestrator"
        command = self.command("record_checkpoint", {"checkpoint": checkpoint(relay=relay)}, attempt=active)
        result = self.f.service.execute(command)
        event = next(e for e in self.f.service.read_events(self.plan_id) if e.kind == "recovery_checkpoint")
        self.assertEqual((event.origin_worker, event.relay, event.actor), ("worker-a", relay, relay))
        self.assertEqual(self.view().coordination.attempts[0].last_activity, before.last_activity)
        self.assertEqual(result.committed["recovery_event_id"], str(event.sequence))
        self.refused(replace(command, key="relay-bad-cas", expected_revision=1), "conflict")

    def test_assigned_recovery_observation_keeps_global_material_hold_after_confirmation(self):
        assigned = self.f.assign(self.p)
        self.f.clock.advance(900)
        self.sweep()
        before = self.view()
        original = self.payload(assigned)
        report = self.f.submit(self.p, assigned, checkpoint(unbounded=True))
        observed = self.view()
        self.assertEqual((observed.coordination.attempts[0].state, observed.coordination.attempts[0].prior_state),
                         ("stale", "assigned"))
        self.assertEqual(observed.coordination.attempts[0].version, before.coordination.attempts[0].version + 1)
        self.assertEqual(self.payload(assigned), original)
        self.assertEqual(observed.legacy_snapshot["steps"][0], before.legacy_snapshot["steps"][0])
        self.assertEqual(len(observed.coordination.holds), 2)
        self.f.service.execute(self.resolve_command(assigned))
        confirmed = self.view()
        self.assertEqual(confirmed.coordination.attempts[0].state, "assigned")
        self.assertEqual([(h.kind, h.step_id, h.source_id) for h in confirmed.coordination.holds],
                         [("reassessment", None, report.committed["recovery_event_id"])])
        self.refused(self.command("start_step", {"origin_worker": "worker-a", "relay": None,
            "observed_activity": "Synthetic observed assigned actor starts"}, attempt=assigned), "held")
        self.f.service.execute(self.command("resolve_reassessment", {"checkpoint_id": report.committed["recovery_event_id"],
            "decision": "reject", "reason": "Explicitly disposition global recovery observations", "draft": None,
            "affected_steps": (), "unbounded": False}))
        self.f.start(self.p, assigned)
        self.assertEqual(self.view().coordination.attempts[0].state, "active")

    def test_confirm_uses_actual_clock_and_preserves_same_worker_grant(self):
        active = self.stale()
        before = self.view()
        actual = self.f.clock().isoformat()
        future = (self.f.clock() + timedelta(days=1)).isoformat()
        self.f.store.write(lambda db: db.execute("UPDATE plans SET updated_at=? WHERE plan_id=?", (future, self.plan_id)))
        command = self.resolve_command(active)
        result = self.f.service.execute(command)
        attempt = self.view().coordination.attempts[0]
        self.assertEqual((attempt.state, attempt.last_activity, attempt.version),
                         ("active", actual, before.coordination.attempts[0].version + 1))
        self.assertEqual(self.view().draft.authorization, before.draft.authorization)
        self.assertFalse(self.view().coordination.holds)
        self.assertIsNone(result.private)
        heartbeat = self.command("report_activity", {"origin_worker": "worker-a", "relay": None,
            "observed_activity": "actual observed work continues"}, attempt=active)
        self.f.service.execute(heartbeat)

    def test_confirm_rejects_contradictory_fields_and_missing_native_capability(self):
        active = self.stale()
        for changes in ({"next_step_state": "blocked"}, {"replacement_assignee": "other"},
                        {"replacement_context": context(None, writes=False)}, {"stopped_resource_ids": ("foreign",)}):
            self.refused(self.resolve_command(active, **changes), "invalid")
        self.f.store.write(lambda db: db.execute("UPDATE capabilities SET revoked=1 WHERE role='attempt' AND plan_id=?", (self.plan_id,)))
        for resolution, changes in (("confirm-owner", {}), ("reject", {"next_step_state": "blocked"}),
                                    ("release", {}), ("replace", {"replacement_assignee": "replacement-worker",
                                     "replacement_context": context(None, writes=False)}),
                                    ("attach-context", {"attached_context": context(None, writes=False)})):
            self.refused(self.resolve_command(active, resolution, **changes), "unsupported")

    def test_submitted_overdue_requires_explicit_confirmation_then_reopen_same_attempt(self):
        active = self.live()
        value = checkpoint()
        self.f.submit(self.p, active, value)
        self.f.clock.advance(901)
        self.refused(self.command("reopen_attempt", {"reason": "Observed correction needed"}, attempt=active), "stale")
        self.refused(self.command("accept_step", {"step_id": "a", "artifact": value.artifact,
            "stopped_resource_ids": (), "stopped_reason": None}, attempt=active), "stale")
        self.sweep()
        self.f.service.execute(self.resolve_command(active))
        self.f.service.execute(self.command("reopen_attempt", {"reason": "Observed correction dispatched"}, attempt=active))
        value = checkpoint(artifact_version="v2")
        self.f.submit(self.p, active, value)
        self.f.accept(self.p, active, value)
        self.assertEqual(len(self.view().coordination.attempts), 1)
        self.assertEqual(self.view().coordination.attempts[0].state, "accepted")

    def test_isolated_replacement_keeps_old_resources_and_fences_checkpoint_retry(self):
        x = self.f.register(workspace("/synthetic/recovery/X", 2110), self.p)
        y = self.f.register(workspace("/synthetic/recovery/Y", 2111), self.p)
        database = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
            False, None, "synthetic-recovery-shared-db"), self.p)
        active = self.live(execution=context(x, external_ids=(database,)))
        prior_checkpoint = self.command("record_checkpoint", {"checkpoint": checkpoint()}, attempt=active)
        self.f.service.execute(prior_checkpoint)
        self.f.clock.advance(901)
        self.sweep()
        before = self.view()
        old_context = self.payload(active)["context"]
        command = self.resolve_command(active, "replace", replacement_assignee="worker-a", replacement_context=context(y))
        replacement = self.f.service.execute(command)
        after = self.view()
        self.assertEqual(after.metadata.revision, before.metadata.revision + 2)
        self.assertEqual(after.coordination.store_sequence, before.coordination.store_sequence + 2)
        self.assertEqual(len(after.coordination.attempts), 2)
        old = next(a for a in after.coordination.attempts if a.attempt_id == active.committed["attempt_id"])
        new = next(a for a in after.coordination.attempts if a.attempt_id != old.attempt_id)
        self.assertEqual((old.state, old.version, new.state), ("released", before.coordination.attempts[0].version + 1, "assigned"))
        self.assertEqual(self.payload(active)["context"], old_context)
        self.assertEqual({r.resource_ids[0] for r in after.coordination.reservations if r.attempt_id == old.attempt_id}, {x, database})
        self.assertTrue(all(r.unresolved for r in after.coordination.reservations if r.attempt_id == old.attempt_id))
        self.assertIsNotNone(replacement.private.attempt_token)
        replay = self.f.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertTrue(replay.private.attempt_token == replacement.private.attempt_token)
        self.assertEqual(self.view().metadata, after.metadata)
        self.refused(prior_checkpoint, "fenced")
        from adaptive_planning import resources
        for attempt in (old, new):
            self.f.store.read(lambda db: resources.execution_uses(db, self.plan_id, attempt.attempt_id))
        other = self.f.create(creation_id="third-plan")
        separate = make_service(self.f.directory, self.f.clock)
        for rid in (x, database):
            command = self.command("assign_step", {"step_id": "a", "assignee": "worker-a", "context": context(
                rid if rid == x else None, external_ids=(rid,) if rid == database else ())}, receipt=other)
            with self.assertRaises(DomainError) as error:
                separate.service.execute(command)
            self.assertEqual(error.exception.code, "resource_conflict")

    def test_overlapping_replacement_rolls_back_all_fencing_releases_and_receipt(self):
        x = self.f.register(workspace("/synthetic/recovery/overlap", 2120), self.p)
        active = self.stale(execution=context(x))
        command = self.resolve_command(active, "replace", replacement_assignee="worker-a", replacement_context=context(x))
        self.refused(command, "resource_conflict")
        # Rollback must retain the original authenticated grant, not just the public row.
        recovery = self.f.submit(self.p, active, checkpoint())
        self.assertTrue(recovery.committed["recovery_only"])

    def test_late_replacement_failure_rolls_back_prior_explicit_stop(self):
        x = self.f.register(workspace("/synthetic/recovery/stopped-X", 2121), self.p)
        y = self.f.register(workspace("/synthetic/recovery/occupied-Y", 2122), self.p)
        other = self.f.create(creation_id="occupied-plan")
        self.f.service.execute(self.command("assign_step", {"step_id": "a", "assignee": "worker-a", "context": context(y)}, receipt=other))
        active = self.stale(execution=context(x))
        command = self.resolve_command(active, "replace", replacement_assignee="worker-a", replacement_context=context(y),
                                       stopped_resource_ids=(x,))
        self.refused(command, "resource_conflict")
        self.assertTrue(all(r.unresolved is False for r in self.view().coordination.reservations))
        self.f.submit(self.p, active, checkpoint())

    def test_same_step_replacement_preserves_first_step_latch(self):
        self.p = self.f.create(mode="first-step", creation_id="first-step-recovery")
        self.plan_id = self.p.current_metadata.plan_id
        active = self.stale()
        claimed = self.view().draft.authorization
        replacement = self.f.service.execute(self.resolve_command(active, "replace", replacement_assignee="worker-a",
                                                                  replacement_context=context(None, writes=False)))
        self.assertEqual(self.view().draft.authorization, claimed)
        self.assertEqual(claimed.claimed, "a")
        self.refused(self.command("assign_step", {"step_id": "b", "assignee": "worker-a",
            "context": context(None, writes=False)}), "forbidden")
        self.assertEqual(replacement.committed["step_id"], "a")

    def test_replacement_cannot_clear_material_or_sibling_stale_hold(self):
        active = self.stale()
        self.f.submit(self.p, active, checkpoint(affected=("a",)))
        self.refused(self.resolve_command(active, "replace", replacement_assignee="worker-a",
            replacement_context=context(None, writes=False)), "held")
        # Independent stale sibling still blocks ordinary assignment after the target cancellation.
        self.f.service.execute(self.resolve_command(active))
        self.f.service.execute(self.command("resolve_reassessment", {"checkpoint_id": str(
            next(e.sequence for e in self.f.service.read_events(self.plan_id) if e.kind == "recovery_checkpoint")),
            "decision": "reject", "reason": "Effects resolved", "draft": None, "affected_steps": (), "unbounded": False}))
        sibling = self.f.assign(self.p, "b")
        self.f.clock.advance(901)
        self.sweep()
        self.refused(self.resolve_command(active, "replace", replacement_assignee="worker-a",
            replacement_context=context(None, writes=False)), "held")
        self.assertEqual(next(a for a in self.view().coordination.attempts if a.attempt_id == sibling.committed["attempt_id"]).state, "stale")

    def test_reject_is_blocked_and_release_retired_uses_real_retirement(self):
        active = self.stale()
        self.refused(self.resolve_command(active, "reject", next_step_state="pending"), "invalid")
        self.f.service.execute(self.resolve_command(active, "reject", next_step_state="blocked"))
        self.assertEqual((self.view().coordination.attempts[0].state, self.view().legacy_snapshot["steps"][0]["status"]),
                         ("rejected", "blocked"))
        self.p = self.f.create(steps=(step("a"),), creation_id="retire-release")
        self.plan_id = self.p.current_metadata.plan_id
        active = self.stale()
        before = self.view()
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="retired"))
        view = self.view()
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "retired")
        self.assertEqual(view.coordination.attempts[0].version, before.coordination.attempts[0].version + 1)
        self.assertEqual(view.metadata.revision, before.metadata.revision + 1)

    def test_release_pending_or_blocked_keeps_evidence_and_logical_history(self):
        for state in ("pending", "blocked"):
            with self.subTest(state=state):
                self.p = self.f.create(steps=(step("a"),), creation_id="release-" + state)
                self.plan_id = self.p.current_metadata.plan_id
                active = self.live()
                self.f.submit(self.p, active, checkpoint(outcome="failed"))
                self.f.clock.advance(900)
                self.sweep()
                before = self.view()
                self.f.service.execute(self.resolve_command(active, "release", next_step_state=state))
                view = self.view()
                self.assertEqual(view.legacy_snapshot["steps"][0]["status"], state)
                self.assertEqual(view.legacy_snapshot["steps"][0]["evidence"], before.legacy_snapshot["steps"][0]["evidence"])
                self.assertEqual(view.coordination.attempts[0].state, "released")

    def test_stopped_original_recovery_keeps_real_release_subevent(self):
        x = self.f.register(workspace("/synthetic/recovery/stop", 2130), self.p)
        active = self.stale(execution=context(x))
        before = self.view()
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="pending", stopped_resource_ids=(x,)))
        view = self.view()
        self.assertEqual(view.metadata.revision, before.metadata.revision + 2)
        self.assertEqual(view.coordination.store_sequence, before.coordination.store_sequence + 2)
        self.assertTrue(self.f.store.read(lambda db: db.execute("SELECT status FROM resource_uses WHERE plan_id=?", (self.plan_id,)).fetchone()[0]) == "released")
        self.assertEqual([e.kind for e in self.f.service.read_events(self.plan_id)][-2:], ["release_resource_use", "resolve_attempt"])

    def test_verifier_must_release_anchored_full_group_before_original(self):
        x = self.f.register(workspace("/synthetic/recovery/parent", 2131), self.p)
        active = self.live(execution=context(x))
        self.f.submit(self.p, active, checkpoint())
        original = self.view().coordination.reservations[0]
        verification = self.f.service.execute(self.reserve_command(active, context(x), handoff=original.use_id))
        self.f.clock.advance(900)
        self.sweep()
        command = self.resolve_command(active, "release", next_step_state="pending", stopped_resource_ids=(x,))
        self.refused(command, "resource_conflict")
        self.release(active, verification.committed["use_id"], (x,))
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="pending", stopped_resource_ids=(x,)))
        self.assertTrue(self.f.store.read(lambda db: all(row[0] == "released" for row in db.execute(
            "SELECT status FROM resource_uses WHERE plan_id=?", (self.plan_id,)))))

    def test_stop_ids_never_release_foreign_or_duplicate_resources(self):
        x = self.f.register(workspace("/synthetic/recovery/owned", 2132), self.p)
        foreign = self.f.register(workspace("/synthetic/recovery/foreign", 2133), self.p)
        active = self.stale(execution=context(x))
        for ids in ((foreign,), (x, x)):
            self.refused(self.resolve_command(active, "release", stopped_resource_ids=ids), "invalid")

    def test_late_owner_evidence_only_appends_note_and_stop_report(self):
        x = self.f.register(workspace("/synthetic/recovery/late", 2140), self.p)
        active = self.stale(execution=context(x))
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="pending"))
        old_payload = self.payload(active)
        before = self.view()
        evidence = (Evidence("Observed historical worker stopped", None, None, "Host observation", "observation", (), ()),)
        command = self.command("append_late_evidence", {"origin_attempt_id": active.committed["attempt_id"],
            "evidence": evidence, "stopped_resource_ids": (x,), "reason": "Observed late stop report"}, attempt=active)
        note = self.f.service.execute(command)
        self.assertFalse(note.committed["releases_performed"])
        self.assertEqual(self.payload(active), old_payload)
        self.assertEqual(self.view().coordination.attempts, before.coordination.attempts)
        self.assertEqual(self.view().coordination.reservations, before.coordination.reservations)
        self.assertEqual(self.view().metadata.revision, before.metadata.revision + 1)
        self.assertEqual(self.view().coordination.store_sequence, before.coordination.store_sequence + 1)
        self.assertTrue(self.f.service.execute(command).replayed)
        self.refused(self.command("append_late_evidence", {**command.payload, "origin_attempt_id": "foreign"},
                                  attempt=active), "invalid")
        self.release(active, before.coordination.reservations[0].use_id, (x,))

    def test_late_historical_definition_survives_new_current_definition(self):
        active = self.stale()
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="pending"))
        draft = self.view().draft
        revised = replace(draft, steps=tuple(replace(s, title="Revised current definition", definition_version="v2")
                                              if s.step_id == "a" else s for s in draft.steps))
        self.f.service.execute(self.command("revise_plan", {"draft": revised, "reason": "Explicit new definition",
            "authorization_change": None, "retire_steps": ()}))
        evidence = (Evidence("Historical failed observation", None, None, "Observation", "failed", (), ("Observed failure",)),)
        note = self.f.service.execute(self.command("append_late_evidence", {"origin_attempt_id": active.committed["attempt_id"],
            "evidence": evidence, "stopped_resource_ids": (), "reason": "Retain original observation"}, attempt=active))
        event = next(e for e in self.f.service.read_events(self.plan_id) if e.kind == "append_late_evidence")
        self.assertEqual(event.payload["definition"]["definition_version"], "v1")
        self.assertFalse(note.committed["releases_performed"])

    def test_new_writable_verification_overdue_refuses_but_readonly_review_and_exact_replay_work(self):
        x = self.f.register(workspace("/synthetic/recovery/verifier-source", 2150), self.p)
        y = self.f.register(workspace("/synthetic/recovery/verifier-isolated", 2151), self.p)
        active = self.live(execution=context(x))
        value = checkpoint()
        self.f.submit(self.p, active, value)
        command = self.reserve_command(active, context(y))
        self.f.clock.advance(899)
        first = self.f.service.execute(command)
        self.f.clock.advance(1)
        self.assertTrue(self.f.service.execute(command).replayed)
        original = next(r for r in self.view().coordination.reservations if r.actor == "worker-a")
        for execution, handoff in ((context(y), None), (context(x), original.use_id)):
            self.refused(self.reserve_command(active, execution, handoff=handoff), "stale")
        self.review(value)
        self.assertEqual(self.view().coordination.attempts[0].state, "submitted")
        self.assertEqual(first.committed["attempt_id"], active.committed["attempt_id"])

    def test_replacement_replay_withholds_original_grant_after_later_replacement(self):
        active = self.stale()
        first_command = self.resolve_command(active, "replace", replacement_assignee="worker-a",
                                              replacement_context=context(None, writes=False))
        first = self.f.service.execute(first_command)
        self.f.clock.advance(900)
        self.sweep()
        self.f.service.execute(self.resolve_command(first, "replace", replacement_assignee="worker-a",
                                                     replacement_context=context(None, writes=False)))
        before = self.state()
        replay = self.f.service.execute(first_command)
        self.assertTrue(replay.replayed)
        self.assertIsNone(replay.private)
        self.assertEqual(self.state(), before)
        takeover = self.f.service.execute(self.command("takeover", {
            "expected_ownership_version": self.view().metadata.ownership_version, "reason": "Synthetic owner takeover"}))
        self.refused(first_command, "fenced")
        self.assertIsNotNone(takeover.private.orchestrator_token)

    def test_recovery_rejects_stale_assertions_and_fabricated_stale_source(self):
        active = self.stale()
        command = self.resolve_command(active)
        self.refused(replace(command, attempt_version=command.attempt_version - 1), "stale")
        self.refused(replace(command, definition_version="foreign-definition"), "stale")
        data = self.payload(active)
        data["stale_source"]["event_id"] = "1"
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
            (json.dumps(data), self.plan_id, active.committed["attempt_id"])))
        self.refused(self.resolve_command(active), "invalid")
        self.refused(self.command("record_checkpoint", {"checkpoint": checkpoint()}, attempt=active), "invalid")

    def test_recovery_material_source_remains_resolvable_after_affected_step_removed(self):
        active = self.stale()
        report = self.f.submit(self.p, active, checkpoint(affected=("b",)))
        self.f.service.execute(self.resolve_command(active, "release", next_step_state="pending"))
        draft = self.view().draft
        self.f.service.execute(self.command("revise_plan", {"draft": replace(draft, steps=tuple(s for s in draft.steps if s.step_id != "b")),
            "reason": "Explicitly remove unclaimed affected step", "authorization_change": None, "retire_steps": ()}))
        self.assertTrue(self.view().coordination.holds)
        self.f.service.execute(self.command("resolve_reassessment", {"checkpoint_id": report.committed["recovery_event_id"],
            "decision": "reject", "reason": "Original effects explicitly reconciled", "draft": None,
            "affected_steps": (), "unbounded": False}))
        self.assertFalse(self.view().coordination.holds)

    def test_transport_runs_real_recovery_routes_without_warning_mcp_tool(self):
        from adaptive_planning import plans
        from adaptive_planning.transport import build_mcp
        active = self.live()
        self.f.clock.advance(900)
        async def check():
            server = build_mcp(self.f.service)
            names = {tool.name for tool in await server.list_tools()}
            self.assertTrue({"sweep_stale", "resolve_attempt", "append_late_evidence"} <= names)
            self.assertIn("resolve_store_hold", names)
            self.assertFalse({"recovery_warnings", "read_recovery_warnings"} & names)
            sweep = self.command("sweep_stale", {})
            result = await server.call_tool("sweep_stale", {"command": plans.to_jsonable(sweep)})
            self.assertFalse(result.is_error)
            self.assertEqual(self.view().coordination.attempts[0].state, "stale")
            resolve = self.resolve_command(active, "release", next_step_state="pending")
            wire = plans.to_jsonable(resolve)
            # The public parser already defaults this field to null; the typed tool must agree.
            del wire["payload"]["attached_context"]
            result = await server.call_tool("resolve_attempt", {"command": wire})
            self.assertFalse(result.is_error)
            note = self.command("append_late_evidence", {"origin_attempt_id": active.committed["attempt_id"],
                "evidence": (Evidence("Late observed result", None, None, "Observe retained worker", "observation", (), ()),),
                "stopped_resource_ids": (), "reason": "Retain late observation"}, attempt=active)
            result = await server.call_tool("append_late_evidence", {"command": plans.to_jsonable(note)})
            self.assertFalse(result.is_error)
            self.assertFalse(result.structured_content["committed"]["releases_performed"])
        asyncio.run(check())

    def test_confirm_preserves_unassessable_unrelated_global_hold(self):
        active = self.stale()
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO holds(hold_id,plan_id,reason,active,created_at) VALUES('unknown-hold',?,'[]',1,?)",
            (self.plan_id, self.f.clock().isoformat())))
        self.f.service.execute(self.resolve_command(active))
        view = self.view()
        self.assertEqual(view.coordination.attempts[0].state, "active")
        self.assertEqual([h.hold_id for h in view.coordination.holds], ["unknown-hold"])
        self.refused(self.command("assign_step", {"step_id": "b", "assignee": "worker-a",
            "context": context(None, writes=False)}), "held")

    def test_recovery_source_numeric_recovery_flag_cannot_resolve_material_hold(self):
        active = self.stale()
        report = self.f.submit(self.p, active, checkpoint(affected=("a",)))
        identity = int(report.committed["recovery_event_id"])
        def corrupt(db):
            data = json.loads(db.execute("SELECT payload FROM events WHERE plan_id=? AND sequence=?",
                                         (self.plan_id, identity)).fetchone()[0])
            data["recovery_only"] = 1
            db.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?", (json.dumps(data), self.plan_id, identity))
        self.f.store.write(corrupt)
        self.refused(self.command("resolve_reassessment", {"checkpoint_id": str(identity), "decision": "reject",
            "reason": "Forged source cannot release holds", "draft": None, "affected_steps": (), "unbounded": False}), "invalid")

    def test_sweep_event_records_supplied_authenticated_actor(self):
        self.live()
        self.f.clock.advance(900)
        self.f.service.execute(replace(self.command("sweep_stale", {}), actor="synthetic-observing-owner"))
        self.assertEqual(next(e.actor for e in self.f.service.read_events(self.plan_id) if e.kind == "sweep_stale"),
                         "synthetic-observing-owner")

    def test_retirement_dependent_failure_rolls_back_old_grant_and_all_history(self):
        active = self.stale()
        self.refused(self.resolve_command(active, "release", next_step_state="retired"), "invalid")
        self.assertTrue(self.f.submit(self.p, active, checkpoint()).committed["recovery_only"])

    def test_final_replacement_failure_rolls_back_real_release_assignment_and_grant(self):
        from adaptive_planning import checkpoints
        x = self.f.register(workspace("/synthetic/recovery/fault-X", 2160), self.p)
        y = self.f.register(workspace("/synthetic/recovery/fault-Y", 2161), self.p)
        active = self.stale(execution=context(x))
        command = self.resolve_command(active, "replace", replacement_assignee="worker-a", replacement_context=context(y),
                                       stopped_resource_ids=(x,))
        original = checkpoints._event_limits
        def fail_final(event):
            if isinstance(event, dict) and event.get("old_token_fenced") is True:
                raise DomainError("limit_exceeded", "Synthetic final publication failure")
            return original(event)
        with patch("adaptive_planning.recovery.checkpoints._event_limits", side_effect=fail_final):
            self.refused(command, "limit_exceeded")
        self.assertTrue(self.f.submit(self.p, active, checkpoint()).committed["recovery_only"])

    def test_custom_policy_verification_gate_is_inclusive(self):
        self.f.service = PlanningService(self.f.store, base_url="http://127.0.0.1:8790", clock=self.f.clock, stale_seconds=6)
        y = self.f.register(workspace("/synthetic/recovery/custom-verifier", 2162), self.p)
        active = self.live()
        self.f.submit(self.p, active, checkpoint())
        self.f.clock.advance(6)
        self.refused(self.reserve_command(active, context(y)), "stale")

    def test_sweep_and_late_notes_require_current_owner_before_exact_replay(self):
        active = self.stale()
        command = self.command("append_late_evidence", {"origin_attempt_id": active.committed["attempt_id"],
            "evidence": (Evidence("Retained observation", None, None, "Observe", "observation", (), ()),),
            "stopped_resource_ids": (), "reason": "Current owner observation"}, attempt=active)
        self.f.service.execute(command)
        self.refused(replace(command, orchestrator_token=None), "forbidden")
        self.refused(replace(command, attempt_token="synthetic-forbidden-extra-attempt-token"), "invalid")
        self.f.service.execute(self.command("takeover", {"expected_ownership_version": self.view().metadata.ownership_version,
            "reason": "Synthetic new current owner"}))
        self.refused(command, "fenced")
