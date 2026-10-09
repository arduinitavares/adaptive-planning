"""Artifact/cycle-bound observations and retained exclusive verification uses."""
from __future__ import annotations
from contextlib import ExitStack

import asyncio
import json
import sqlite3
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from adaptive_planning import checkpoints, plans, reviews
from adaptive_planning.app import PlanningService
from adaptive_planning.store import Store
from adaptive_planning.types import DomainError, Evidence, ResourceClaim, RetirementRequest, Review
from tests.service_fixtures import (
    checkpoint, context, make_service, seed_accepted_result, seed_orphan_use, seed_store_hold, step, workspace,
)


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())
        self.addCleanup(self.f.cleanup)
        self.p = self.f.create(steps=(step("a", review_required=True), step("b"), step("c", ("a",))))
        self.plan_id = self.p.current_metadata.plan_id
        self.value = checkpoint()

    def command(self, kind, payload, **kwargs):
        command = self.f.command(kind, payload, receipt=self.p, **kwargs)
        if kind in ("start_step", "report_activity", "record_checkpoint") and command.orchestrator_token is None:
            command = replace(command, expected_revision=None, ownership_version=None)
        return command

    def live(self, execution=None):
        if execution is None:
            assigned = self.f.assign(self.p)
        else:
            assigned = self.f.service.execute(self.command(
                "assign_step", {"step_id": "a", "assignee": "worker-a", "context": execution}))
        self.a = self.f.start(self.p, assigned)
        self.submitted = self.f.submit(self.p, self.a, self.value)
        return self.submitted

    def review(self, *, value=None, verdict="passed", reviewer="reviewer", use_id=None, evidence=None, findings=()):
        value = value or self.value
        review = Review(reviewer, value.artifact, verdict, findings,
                        value.evidence if evidence is None else evidence, use_id)
        return self.f.service.execute(self.command("record_review", {"step_id": "a", "review": review}))

    def reserve(self, execution, *, handoff=None, reason=None, verifier="reviewer"):
        return self.f.service.execute(self.command("reserve_verification", {
            "step_id": "a", "verifier": verifier, "context": execution,
            "handoff_from": handoff, "stopped_reason": reason}))

    def release(self, use_id, resources, *, stopped=True):
        return self.f.service.execute(self.command("release_resource_use", {
            "use_id": use_id, "resource_ids": tuple(resources), "reason": "Confirmed verifier stopped",
            "stopped": stopped}))

    def reopen(self):
        return self.f.service.execute(self.command("reopen_attempt", {"reason": "Repair observed defect"}, attempt=self.a))

    def satisfied(self, artifact=None, *, plan_id=None, attempt_id=None):
        from adaptive_planning.reviews import review_satisfied
        pid = plan_id or self.plan_id
        aid = attempt_id or (self.a.committed["attempt_id"] if hasattr(self, "a") else self.accepted.attempt_id)
        return self.f.store.read(lambda db: review_satisfied(db, pid, "a", aid, "v1", artifact or self.value.artifact))

    def view(self):
        return self.f.service.read_plan(self.plan_id)

    def state(self):
        # Request responses and capability material deliberately stay outside assertion diagnostics.
        return self.f.store.read(lambda db: {
            table: tuple(db.execute(sql)) for table, sql in {
                "plans": "SELECT plan_id,generation,revision,updated_at,payload FROM plans ORDER BY rowid",
                "steps": "SELECT plan_id,step_id,state,payload FROM steps ORDER BY rowid",
                "attempts": "SELECT plan_id,attempt_id,state,version,started_at,stopped_at,payload FROM attempts ORDER BY rowid",
                "uses": "SELECT use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at FROM resource_uses ORDER BY rowid",
                "events": "SELECT sequence,plan_id,plan_revision,kind,payload FROM events ORDER BY sequence",
                "reviews": "SELECT review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,verification_use_id FROM reviews ORDER BY rowid",
                "checkpoints": "SELECT plan_id,attempt_id,sequence,outcome,artifact FROM checkpoints ORDER BY rowid",
                "holds": "SELECT hold_id,plan_id,reason,active FROM holds ORDER BY rowid",
                "requests": "SELECT request_id,plan_id,key,command_digest FROM requests ORDER BY rowid",
            }.items()})

    def refused(self, operation, code="invalid"):
        before = self.state()
        with self.assertRaises(DomainError) as error:
            operation()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.state(), before)

    def original(self):
        return next(r for r in self.view().coordination.reservations if r.actor == "worker-a")

    def accepted_invalidation(self):
        self.accepted = seed_accepted_result(self.f, self.plan_id, "a", self.value.artifact)
        return self.f.service.execute(self.command("invalidate_result", {
            "step_id": "a", "result_version": self.accepted.result_version,
            "reason": "Revalidate exact accepted output", "evidence": self.value.evidence}))

    def legacy_final_gate_submission(self):
        current = self.view().draft
        legacy = replace(current, steps=(replace(current.steps[0], final_gate=True, review_required=False),
                                        *current.steps[1:]))
        # TEST-ONLY pre-invariant fixture: only the pure flag implication is disabled.
        # All publication, auth, checkpoint, artifact and review proof is real.
        with patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            self.f.service.execute(self.command("revise_plan", {
                "draft": legacy, "reason": "Reproduce the old permissive final flags",
                "authorization_change": None, "retire_steps": (),
            }))
            self.live()
            command = self.command("record_review", {"step_id": "a", "review": Review(
                "reviewer", self.value.artifact, "passed", (), self.value.evidence, None)}, key="legacy-review")
            self.f.service.execute(command)
        return command

    def test_final_gate_first_review_is_not_circular(self):
        current = self.view().draft
        self.f.service.execute(self.command("revise_plan", {
            "draft": replace(current, steps=(replace(current.steps[0], final_gate=True), *current.steps[1:])),
            "reason": "Configure the ordinary final-review step", "authorization_change": None, "retire_steps": (),
        }))
        self.live()
        self.assertFalse(self.satisfied())
        first = self.review()
        self.assertTrue(self.satisfied())
        self.assertEqual(self.view().coordination.reviews[0].review.artifact, self.value.artifact)
        self.assertEqual(first.committed["attempt_id"], self.a.committed["attempt_id"])

    def test_legacy_final_gate_submission_acceptance_fails_closed(self):
        self.legacy_final_gate_submission()
        self.refused(lambda: self.f.accept(self.p, self.a, self.value))
        self.assertFalse(self.view().draft.steps[0].review_required)

    def test_legacy_final_gate_submission_review_fails_closed(self):
        self.legacy_final_gate_submission()
        self.refused(lambda: self.review())

    def test_legacy_final_gate_submission_reservation_fails_closed(self):
        self.legacy_final_gate_submission()
        rid = self.f.register(workspace("/synthetic/legacy-final-verifier", 550), self.p)
        self.refused(lambda: self.reserve(context(rid)))

    def test_legacy_final_gate_submission_reopen_fails_closed(self):
        self.legacy_final_gate_submission()
        self.refused(lambda: self.reopen())

    def test_legacy_final_gate_submission_cannot_satisfy_new_review(self):
        self.legacy_final_gate_submission()
        before = self.state()
        self.assertFalse(self.satisfied())
        self.assertEqual(self.state(), before)

    def test_legacy_final_gate_review_exact_replay_precedes_live_guard(self):
        command = self.legacy_final_gate_submission()
        before = self.state()
        replay = self.f.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.state(), before)
        self.refused(lambda: self.f.service.execute(self.command(
            "record_review", command.payload, key="new-legacy-review")))

    def test_review_bound_to_current_artifact(self):
        self.live()
        self.review()
        self.assertTrue(self.satisfied())
        self.assertFalse(self.satisfied(checkpoint(artifact_version="v2").artifact))
        row = self.view().coordination.reviews[0]
        self.assertEqual((row.plan_id, row.step_id, row.attempt_id, row.definition_version, row.review.artifact),
                         (self.plan_id, "a", self.a.committed["attempt_id"], "v1", self.value.artifact))
        other = self.f.create(steps=(step("a", review_required=True),), creation_id="other-review-plan")
        other_attempt = self.f.start(other, self.f.assign(other))
        self.f.submit(other, other_attempt, self.value)
        self.assertFalse(self.satisfied(plan_id=other.current_metadata.plan_id,
                                        attempt_id=other_attempt.committed["attempt_id"]))

    def test_labels_and_findings_have_literal_recorded_semantics(self):
        self.live()
        for label in ("worker-a", "   "):
            self.refused(lambda: self.review(reviewer=label))
        self.refused(lambda: self.review(findings=(" ",)))
        self.review(reviewer=" worker-a ", findings=("Nonblocking limitation retained",))
        self.assertTrue(self.satisfied())
        self.assertEqual(self.view().coordination.reviews[-1].review.reviewer, " worker-a ")

    def test_latest_original_verdict_controls_current_review(self):
        self.live()
        first = self.review()
        self.review(verdict="failed", findings=("Exact artifact regression",))
        self.assertFalse(self.satisfied())
        self.f.store.write(lambda db: plans._insert_event(db, self.plan_id, None, "generic_note", "owner",
                            self.f.clock().isoformat(), {"review_id": first.committed["review_id"]}))
        self.assertFalse(self.satisfied())
        self.review(findings=("Known nonblocking limitation",))
        self.assertTrue(self.satisfied())

    def test_selected_inadequate_pass_does_not_fall_back(self):
        self.live()
        self.review()
        newest = self.review()
        self.f.store.write(lambda db: db.execute("UPDATE reviews SET evidence='[]' WHERE review_id=?",
                                                 (newest.committed["review_id"],)))
        self.assertFalse(self.satisfied())

    def test_selected_blank_finding_is_inadequate_without_older_pass_fallback(self):
        self.live()
        self.review()
        newest = self.review()
        self.f.store.write(lambda db: db.execute("UPDATE reviews SET findings='[\"   \"]' WHERE review_id=?",
                                                 (newest.committed["review_id"],)))
        self.assertFalse(self.satisfied())

    def test_optional_contradictory_original_audit_links_are_not_ignored(self):
        self.live()
        self.review()
        def contradict(db):
            sequence, raw = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='record_review'",
                                       (self.plan_id,)).fetchone()
            data = json.loads(raw)
            data["producer_step_id"] = "foreign-producer"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(data), sequence))
        self.f.store.write(contradict)
        self.assertFalse(self.satisfied())

    def test_original_recorded_verdict_cannot_contradict_the_review_row(self):
        self.live()
        self.review()
        def contradict(db):
            sequence, raw = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='record_review'",
                                       (self.plan_id,)).fetchone()
            event = json.loads(raw)
            event["verdict"] = "failed"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(event), sequence))
        self.f.store.write(contradict)
        self.assertFalse(self.satisfied())

    def test_contradictory_first_review_link_cannot_be_repaired(self):
        self.live()
        observed = self.review()
        def corrupt(db):
            seq, raw = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='record_review'",
                                  (self.plan_id,)).fetchone()
            event = json.loads(raw)
            event["attempt_id"] = "foreign-attempt"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(event), seq))
            event["attempt_id"] = self.a.committed["attempt_id"]
            plans._insert_event(db, self.plan_id, None, "later_note", "owner", self.f.clock().isoformat(), event)
        self.f.store.write(corrupt)
        self.assertFalse(self.satisfied())
        self.assertEqual(len(self.view().coordination.reviews), 1)

    def test_observation_preserves_cycle_reopen_ends_it(self):
        self.live()
        self.review()
        before = self.view().coordination.attempts[0]
        self.f.clock.advance(1)
        self.f.service.execute(self.command("report_activity", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "Observed submitted output"}, attempt=self.a))
        self.assertTrue(self.satisfied())
        observed = self.view().coordination.attempts[0]
        self.assertEqual(observed.version, before.version + 1)
        reopened = self.reopen()
        self.assertFalse(self.satisfied())
        active = self.view().coordination.attempts[0]
        self.assertEqual((active.attempt_id, active.version, active.state, active.last_activity),
                         (observed.attempt_id, observed.version + 1, "active", observed.last_activity))
        self.assertIsNone(reopened.private)
        self.f.submit(self.p, self.a, checkpoint(artifact_version="v2"))
        self.assertFalse(self.satisfied(checkpoint(artifact_version="v2").artifact))
        self.review(value=checkpoint(artifact_version="v2"))
        self.assertTrue(self.satisfied(checkpoint(artifact_version="v2").artifact))

    def test_new_same_artifact_version_rejected_in_domain_and_service(self):
        self.live()
        self.reopen()
        changed = replace(self.value, artifact=replace(self.value.artifact, identity="changed", summary="changed"),
                          evidence=(replace(self.value.evidence[0], artifact=replace(self.value.artifact, identity="changed", summary="changed")),))
        command = self.command("record_checkpoint", {"checkpoint": changed}, attempt=self.a)
        self.refused(lambda: self.f.service.execute(command))
        self.refused(lambda: self.f.service._run_write(lambda db: checkpoints.record_checkpoint(db, command)))

    def test_version_guard_looks_past_artifactless_cycles_and_allows_adjacent_reuse(self):
        self.live()
        self.reopen()
        empty = replace(self.value, artifact=None, evidence=(replace(self.value.evidence[0], artifact=None),))
        self.f.submit(self.p, self.a, empty)
        self.refused(lambda: self.review())
        rid = self.f.register(workspace("/synthetic/optional-review", 910), self.p)
        self.refused(lambda: self.reserve(context(rid)))
        self.reopen()
        self.refused(lambda: self.f.submit(self.p, self.a, self.value))
        self.f.submit(self.p, self.a, checkpoint(artifact_version="v2"))
        self.reopen()
        self.f.submit(self.p, self.a, self.value)
        self.assertFalse(self.satisfied())
        self.review()
        self.assertTrue(self.satisfied())

    def test_first_explicit_artifact_after_optional_diagnostic_is_legal(self):
        self.value = replace(checkpoint("failed"), artifact=None,
                             evidence=(replace(checkpoint("failed").evidence[0], artifact=None),))
        self.live()
        self.reopen()
        self.f.submit(self.p, self.a, checkpoint())
        self.review(value=checkpoint())
        self.assertTrue(self.satisfied(checkpoint().artifact))

    def test_malformed_newest_checkpoint_has_no_older_fallback(self):
        self.live()
        self.review()
        self.reopen()
        newer = self.f.submit(self.p, self.a, checkpoint(artifact_version="v2"))
        self.f.store.write(lambda db: db.execute("UPDATE events SET payload='{}' WHERE sequence=?",
                                                 (int(newer.committed["checkpoint_id"]),)))
        self.assertFalse(self.satisfied(checkpoint(artifact_version="v2").artifact))
        self.refused(lambda: self.review(value=checkpoint(artifact_version="v2")))

    def test_failed_checkpoint_can_receive_durable_failed_diagnostics(self):
        self.value = checkpoint("failed")
        self.live()
        self.review(verdict="failed", findings=("Checkpoint failure inspected",))
        self.assertFalse(self.satisfied())
        self.assertEqual(self.view().coordination.attempts[0].state, "submitted")
        self.assertEqual(self.view().coordination.reviews[0].review.findings, ("Checkpoint failure inspected",))
        self.refused(lambda: self.review())
        passed = replace(self.value.evidence[0], result="passed")
        self.review(evidence=(passed,))
        self.assertTrue(self.satisfied())  # This predicate does not certify checkpoint success.

    def test_review_and_use_observations_do_not_change_attempt_counters(self):
        self.live()
        before = self.view()
        self.f.clock.advance(1)
        self.review()
        rid = self.f.register(workspace("/synthetic/counter-review", 901), self.p)
        prior = self.view()
        reserved = self.reserve(context(rid))
        self.assertEqual(self.view().metadata.revision, prior.metadata.revision + 1)
        self.release(reserved.committed["use_id"], (rid,), stopped=False)
        self.assertEqual(self.view().metadata.revision, prior.metadata.revision + 2)
        self.assertEqual(self.view().coordination.attempts, before.coordination.attempts)
        self.assertTrue(next(r for r in self.view().coordination.reservations if r.use_id == reserved.committed["use_id"]).writer_active)

    def test_review_handoff_never_frees_exclusive_workspace(self):
        rid = self.f.register(workspace("/synthetic/exclusive-review", 902), self.p)
        self.live(context(rid))
        original = self.original()
        authorization = self.view().draft.authorization
        other = self.f.create(steps=(step("a"),), creation_id="third-assignment")
        def third():
            return self.f.service.execute(self.f.command("assign_step", {
                "step_id": "a", "assignee": "third", "context": context(rid)}, receipt=other))
        self.refused(third, "resource_conflict")
        self.review()
        self.assertEqual(self.original(), original)
        reserved = self.reserve(context(rid), handoff=original.use_id, reason="Implementation actor stopped across entire cohort")
        after = self.original()
        self.assertEqual((after.use_id, after.actor, after.resource_ids), (original.use_id, "worker-a", (rid,)))
        self.assertFalse(after.writer_active)
        self.refused(third, "resource_conflict")
        self.refused(self.reopen, "resource_conflict")
        self.release(reserved.committed["use_id"], (rid,), stopped=False)
        self.refused(self.reopen, "resource_conflict")
        self.refused(lambda: self.release(original.use_id, (rid,)), "resource_conflict")
        self.release(reserved.committed["use_id"], (rid,))
        self.refused(third, "resource_conflict")
        self.reopen()
        self.assertTrue(self.original().writer_active)
        self.assertEqual(self.view().draft.authorization, authorization)

    def test_worker_observation_never_reactivates_handed_off_writer(self):
        rid = self.f.register(workspace("/synthetic/stopped-observation", 903), self.p)
        self.live(context(rid))
        reserved = self.reserve(context(rid), handoff=self.original().use_id, reason="Worker stopped")
        self.f.service.execute(self.command("report_activity", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "Inspecting submitted output"}, attempt=self.a))
        self.assertFalse(self.original().writer_active)
        self.refused(self.reopen, "resource_conflict")
        self.release(reserved.committed["use_id"], (rid,))
        self.review(use_id=reserved.committed["use_id"])
        self.assertTrue(self.satisfied())

    def test_multiresource_group_anchor_projection_paging_and_exact_release(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/multigroup", 904), self.p)
        external = []
        for key in ("bucket-one", "bucket-two"):
            external.append(self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                                         False, None, key), self.p))
        selected = (rid, *external)
        reserved = self.reserve(context(rid, external_ids=tuple(external)))
        anchor = reserved.committed["use_id"]
        groups = self.view().coordination.reservations
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].use_id, groups[0].actor, set(groups[0].resource_ids)), (anchor, "reviewer", set(selected)))
        page, collected = None, []
        while True:
            page = self.f.service.read_coordination(self.plan_id, after=None if page is None else page.next_cursor, limit=1)
            collected.extend(i.value for i in page.items if i.kind == "reservation")
            self.assertEqual(page.total_counts["reservations"], 1)
            if page.next_cursor is None:
                break
        self.assertEqual(collected, list(groups))
        rows = self.f.store.read(lambda db: tuple(db.execute("SELECT use_id,resource_id FROM resource_uses WHERE attempt_id=?",
                                                            (self.a.committed["attempt_id"],))))
        self.assertEqual(len(rows), 3)
        for member, resource in rows:
            self.refused(lambda: self.release(anchor, (resource,)))
            if member != anchor:
                self.refused(lambda: self.release(member, selected))
        extra = self.f.register(workspace("/synthetic/group-extra", 905), self.p)
        self.refused(lambda: self.release(anchor, (*selected, extra)))
        self.review(use_id=anchor)
        self.release(anchor, tuple(reversed(selected)))
        self.assertEqual(self.view().coordination.total_counts["reservations"], 1)
        self.assertFalse(self.view().coordination.reservations[0].writer_active)
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT COUNT(*) FROM resource_uses WHERE status='released'").fetchone()[0]), 3)
        self.review(use_id=anchor)

    def test_each_physical_member_blocks_ordinary_conflicts(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/member-review", 906), self.p)
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "shared-system"), self.p)
        self.reserve(context(rid, external_ids=(ext,)))
        other = self.f.create(steps=(step("a"),), creation_id="ordinary-conflicts")
        for execution in (context(rid), context(None, external_ids=(ext,))):
            self.refused(lambda: self.f.service.execute(self.f.command("assign_step", {
                "step_id": "a", "assignee": "third", "context": execution}, receipt=other)), "resource_conflict")

    def test_full_parent_cohort_handoff_and_other_same_attempt_conflict(self):
        rid = self.f.register(workspace("/synthetic/cohort-review", 907), self.p)
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "cohort-system"), self.p)
        self.live(context(rid, external_ids=(ext,)))
        original = self.original()
        reserved = self.reserve(context(rid, external_ids=(ext,)), handoff=original.use_id, reason="Single implementer stopped on both resources")
        parent = [r for r in self.view().coordination.reservations if r.actor == "worker-a"]
        self.assertEqual(len(parent), 2)
        self.assertTrue(all(not r.writer_active for r in parent))
        self.refused(lambda: self.reserve(context(rid), handoff=original.use_id, reason="Worker remains stopped"), "resource_conflict")
        self.refused(lambda: self.reserve(context(rid)), "resource_conflict")
        self.release(reserved.committed["use_id"], (rid, ext))
        self.reopen()
        self.assertTrue(all(r.writer_active for r in self.view().coordination.reservations if r.actor == "worker-a"))

    def test_separate_group_does_not_break_original_coverage_on_reopen(self):
        rid = self.f.register(workspace("/synthetic/implementer-only", 908), self.p)
        separate = self.f.register(workspace("/synthetic/separate-verifier", 909), self.p)
        self.live(context(rid))
        self.reserve(context(separate))
        self.reopen()
        self.assertEqual(self.view().coordination.attempts[0].context, context(rid))

    def test_group_reference_requires_anchor_actor_cycle_and_original_creation(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/provenance-review", 911), self.p)
        reserved = self.reserve(context(rid))
        use_id = reserved.committed["use_id"]
        self.refused(lambda: self.review(use_id=use_id, reviewer="another-reviewer"))
        self.review(use_id=use_id)
        self.release(use_id, (rid,))
        self.reopen()
        self.f.submit(self.p, self.a, checkpoint(artifact_version="v2"))
        self.refused(lambda: self.review(value=checkpoint(artifact_version="v2"), use_id=use_id))

    def test_writable_verification_rejected_during_store_hold(self):
        self.accepted_invalidation()
        seed_store_hold(self.f, "synthetic incomplete restore inventory")
        rid = self.f.register(workspace("/synthetic/held-review", 703), self.p)
        self.refused(lambda: self.reserve(context(rid)), "held")
        before = self.f.store.read(lambda db: db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.accepted.attempt_id,)).fetchone()[0])
        self.review()
        after = self.f.store.read(lambda db: db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.accepted.attempt_id,)).fetchone()[0])
        self.assertEqual(after, before)
        self.assertTrue(self.f.service.read_service_info().store_hold)

    def test_readonly_reservation_rejected_without_synthesizing_use(self):
        self.live()
        self.refused(lambda: self.reserve(context(None, writes=False)))
        self.review()
        self.assertFalse(self.view().coordination.reservations)

    def test_accepted_remediation_is_grant_independent_and_preserves_terminal_history(self):
        self.accepted_invalidation()
        original = self.f.store.read(lambda db: db.execute("SELECT state,version,started_at,stopped_at,payload FROM attempts WHERE attempt_id=?",
                                                          (self.accepted.attempt_id,)).fetchone())
        self.review()
        rid = self.f.register(workspace("/synthetic/accepted-review", 912), self.p)
        reserved = self.reserve(context(rid))
        self.review(use_id=reserved.committed["use_id"], verdict="failed", findings=("Accepted defect remains",))
        self.release(reserved.committed["use_id"], (rid,))
        after = self.f.store.read(lambda db: db.execute("SELECT state,version,started_at,stopped_at,payload FROM attempts WHERE attempt_id=?",
                                                       (self.accepted.attempt_id,)).fetchone())
        self.assertEqual(after[:4], original[:4])
        original_payload, after_payload = json.loads(original[4]), json.loads(after[4])
        self.assertEqual({key: after_payload[key] for key in original_payload}, original_payload)
        self.assertEqual(self.view().legacy_snapshot["steps"][0]["status"], "complete")
        self.assertTrue(self.view().coordination.holds)
        self.assertFalse(self.satisfied())
        accepted_receipt = replace(self.p, committed={"attempt_id": self.accepted.attempt_id, "version": 4, "definition_version": "v1"})
        self.refused(lambda: self.f.service.execute(self.command("reopen_attempt", {"reason": "Forbidden terminal resume"}, attempt=accepted_receipt)))

    def test_multiple_invalidation_boundary_is_monotonic_when_one_hold_closes(self):
        first = self.accepted_invalidation()
        second = self.f.service.execute(self.command("invalidate_result", {
            "step_id": "a", "result_version": self.accepted.result_version,
            "reason": "Another original invalidation", "evidence": self.value.evidence}))
        self.review()
        self.assertTrue(self.satisfied())
        self.f.service.execute(self.command("resolve_invalidation", {
            "invalidation_id": second.committed["invalidation_id"], "affected_step_id": "a", "disposition": "revalidated",
            "corrective_step_id": None, "corrected_result_version": self.accepted.result_version,
            "evidence": self.value.evidence, "reason": "Explicit producer proof for second hold"}))
        self.assertTrue(self.satisfied())
        self.f.service.execute(self.command("invalidate_result", {
            "step_id": "a", "result_version": self.accepted.result_version,
            "reason": "New applicable boundary", "evidence": self.value.evidence}))
        self.assertFalse(self.satisfied())
        self.review()
        self.assertTrue(self.satisfied())

    def test_accepted_review_requires_open_applicable_invalidation(self):
        self.accepted = seed_accepted_result(self.f, self.plan_id, "a", self.value.artifact)
        self.refused(lambda: self.review())

    def test_accepted_consumer_review_and_group_bind_original_producer_invalidation(self):
        producer = seed_accepted_result(self.f, self.plan_id, "a", self.value.artifact)
        consumer_artifact = replace(self.value.artifact, identity="accepted-consumer", version="consumer-v1")
        consumer = seed_accepted_result(self.f, self.plan_id, "c", consumer_artifact)
        invalidate = lambda: self.f.service.execute(self.command("invalidate_result", {
            "step_id": "a", "result_version": producer.result_version,
            "reason": "Revalidate accepted dependency and consumer", "evidence": self.value.evidence}))
        invalidate()
        rid = self.f.register(workspace("/synthetic/accepted-consumer-review", 933), self.p)
        group = self.f.service.execute(self.command("reserve_verification", {
            "step_id": "c", "verifier": "consumer-reviewer", "context": context(rid),
            "handoff_from": None, "stopped_reason": None}))
        evidence = (replace(self.value.evidence[0], artifact=consumer_artifact),)
        def observe(use_id):
            return self.f.service.execute(self.command("record_review", {"step_id": "c", "review": Review(
                "consumer-reviewer", consumer_artifact, "passed", (), evidence, use_id)}))
        def satisfied():
            from adaptive_planning.reviews import review_satisfied
            return self.f.store.read(lambda db: review_satisfied(db, self.plan_id, "c", consumer.attempt_id, "v1", consumer_artifact))
        self.release(group.committed["use_id"], (rid,))
        observe(group.committed["use_id"])
        self.assertTrue(satisfied())
        invalidate()
        self.assertFalse(satisfied())
        self.refused(lambda: observe(group.committed["use_id"]))
        observe(None)
        self.assertTrue(satisfied())
        self.assertEqual({r.state for r in self.view().coordination.attempts}, {"accepted"})

    def test_exact_replay_retains_old_cycle_receipts_and_checks_authentication_first(self):
        self.a = self.f.start(self.p, self.f.assign(self.p))
        checkpoint_command = self.command("record_checkpoint", {"checkpoint": self.value}, attempt=self.a)
        submitted = self.f.service.execute(checkpoint_command)
        review_command = self.command("record_review", {"step_id": "a", "review": Review(
            "reviewer", self.value.artifact, "passed", (), self.value.evidence, None)})
        reviewed = self.f.service.execute(review_command)
        self.reopen()
        self.f.submit(self.p, self.a, checkpoint(artifact_version="v2"))
        before = self.state()
        for command, receipt in ((checkpoint_command, submitted), (review_command, reviewed)):
            replay = self.f.service.execute(command)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.committed, receipt.committed)
            self.assertEqual(replay.committed_metadata, receipt.committed_metadata)
        self.assertEqual(self.state(), before)
        self.refused(lambda: self.f.service.execute(replace(review_command, orchestrator_token="invalid-grant")), "forbidden")

    def test_envelope_target_assertions_are_validated_before_mutation(self):
        self.live()
        base = self.command("record_review", {"step_id": "a", "review": Review(
            "reviewer", self.value.artifact, "passed", (), self.value.evidence, None)}, attempt=self.a)
        for fields in ({"attempt_id": "foreign"}, {"attempt_version": 999}, {"definition_version": "other"}):
            self.refused(lambda: self.f.service.execute(replace(base, **fields)), "stale")
        self.refused(lambda: self.f.service.execute(replace(base, attempt_token="forbidden-extra")), "invalid")

    def test_release_validates_optional_exact_owned_attempt_assertions(self):
        rid = self.f.register(workspace("/synthetic/release-assertions", 931), self.p)
        self.live(context(rid))
        reserved = self.reserve(context(rid), handoff=self.original().use_id, reason="Implementation actor stopped")
        command = self.command("release_resource_use", {
            "use_id": reserved.committed["use_id"], "resource_ids": (rid,), "reason": "Verifier stopped", "stopped": True},
            attempt=self.a)
        for fields in ({"attempt_id": "foreign"}, {"attempt_version": 999}, {"definition_version": "other"}):
            self.refused(lambda: self.f.service.execute(replace(command, **fields)), "stale")
        released = self.f.service.execute(command)
        self.assertTrue(released.committed["released"])

    def test_orphan_release_cannot_claim_an_owned_attempt_target(self):
        rid = self.f.register(workspace("/synthetic/orphan-assertions", 932), self.p)
        orphan = seed_orphan_use(self.f, rid)
        command = self.command("release_resource_use", {
            "use_id": orphan.use_id, "resource_ids": (rid,), "reason": "Absent host work stopped", "stopped": True})
        for fields in ({"attempt_id": "foreign"}, {"attempt_version": 4}, {"definition_version": "v1"}):
            self.refused(lambda: self.f.service.execute(replace(command, **fields)), "invalid")

    def test_reopen_rejects_overdue_held_and_released_originals(self):
        rid = self.f.register(workspace("/synthetic/reopen-proof", 913), self.p)
        self.live(context(rid))
        self.release(self.original().use_id, (rid,))
        self.refused(self.reopen, "resource_conflict")
        seed_store_hold(self.f, "Inventory unresolved")
        self.refused(self.reopen, "held")

    def test_handoff_requires_actual_named_overlap_and_stopped_confirmation(self):
        rid = self.f.register(workspace("/synthetic/handoff-proof", 914), self.p)
        separate = self.f.register(workspace("/synthetic/handoff-separate", 915), self.p)
        self.live(context(rid))
        source = self.original().use_id
        self.refused(lambda: self.reserve(context(rid), handoff=source))
        self.refused(lambda: self.reserve(context(separate), handoff=source, reason="Worker stopped"))
        self.refused(lambda: self.reserve(context(separate), reason="No named handoff"))

    def test_unclassified_or_corrupt_groups_remain_visible_and_cannot_authorize_mutation(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/corrupt-group", 916), self.p)
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "corrupt-system"), self.p)
        reserved = self.reserve(context(rid, external_ids=(ext,)))
        def corrupt(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.a.committed["attempt_id"],)).fetchone()[0]
            data = json.loads(raw)
            data["execution_uses"]["v"] = 999
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), self.a.committed["attempt_id"]))
        self.f.store.write(corrupt)
        rows = self.view().coordination.reservations
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.writer_active and r.actor == "" for r in rows))
        self.assertEqual(self.view().coordination.total_counts["reservations"], 2)
        self.refused(lambda: self.release(reserved.committed["use_id"], (rid, ext)))
        self.refused(lambda: self.review(use_id=reserved.committed["use_id"]))
        self.refused(self.reopen)

    def test_whole_group_proof_is_required_before_hiding_any_physical_member(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/full-group-proof", 934), self.p)
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "full-group-system"), self.p)
        reserved = self.reserve(context(rid, external_ids=(ext,)))
        anchor, aid = reserved.committed["use_id"], self.a.committed["attempt_id"]
        original = self.f.store.read(lambda db: db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (aid,)).fetchone()[0])
        variants = {
            "missing": lambda group: group["members"].pop(),
            "duplicate": lambda group: group["members"].append(dict(group["members"][0])),
            "foreign": lambda group: group["members"][0].update(use_id="foreign-use"),
            "scope": lambda group: group["members"][0].update(scope="repository"),
            "creation": lambda group: group["creation"].update(sequence=1),
            "source": lambda group: group.update(source_use_ids=["foreign-source"]),
            "unknown-writer": lambda group: group["writer"].update(state="unknown"),
            "malformed-member": lambda group: group["members"][0].update(use_id=[]),
        }
        for label, change in variants.items():
            with self.subTest(proof=label):
                data = json.loads(original)
                change(data["execution_uses"]["groups"][anchor])
                self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), aid)))
                rows = self.view().coordination.reservations
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row.writer_active and row.actor == "" for row in rows))
                self.assertEqual(self.view().coordination.total_counts["reservations"], 2)
                page, paged = None, []
                while True:
                    page = self.f.service.read_coordination(self.plan_id, after=None if page is None else page.next_cursor, limit=2)
                    paged.extend(item.value for item in page.items if item.kind == "reservation")
                    if page.next_cursor is None:
                        break
                self.assertEqual(paged, list(rows))
                self.refused(lambda: self.release(anchor, (rid, ext)))
                self.refused(lambda: self.review(use_id=anchor))
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (original, aid)))
        self.assertEqual(len(self.view().coordination.reservations), 1)

    def creation_case(self, *, accepted=False, handoff=False, later_writer=False):
        # Each corruption starts from its own coherent history, so a broken RED refusal
        # cannot mutate a subsequent case's group, rows or writer state.
        self.f = self.enterContext(make_service())
        self.addCleanup(self.f.cleanup)
        self.p = self.f.create(steps=(step("a", review_required=True),))
        self.plan_id, self.value = self.p.current_metadata.plan_id, checkpoint()
        for attribute in ("a", "accepted"):
            if hasattr(self, attribute):
                delattr(self, attribute)
        rid = self.f.register(workspace("/synthetic/original-creation", 940), self.p)
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "creation-verifier"), self.p)
        parent = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "creation-implementation"), self.p) if handoff else None
        if accepted:
            self.accepted_invalidation()
        else:
            self.live(context(rid, external_ids=(parent,)) if handoff else None)
        handle = self.original().use_id if handoff else None
        group = self.reserve(context(rid, external_ids=(ext,)), handoff=handle,
                             reason="Implementation stopped across its original cohort" if handoff else None)
        anchor = group.committed["use_id"]
        self.review(use_id=anchor)
        self.release(anchor, (rid, ext))
        review_anchor = anchor
        if later_writer:
            self.reopen()
            self.value = checkpoint(artifact_version="v2")
            self.f.submit(self.p, self.a, self.value)
            latest = self.reserve(context(rid, external_ids=(ext,)), handoff=handle,
                                  reason="Implementation stopped again after explicit reacquisition")
            review_anchor = latest.committed["use_id"]
            self.review(use_id=review_anchor)
        self.assertTrue(self.satisfied())
        sequence, raw = self.f.store.read(lambda db: next(
            (sequence, raw) for sequence, raw in db.execute(
                "SELECT sequence,payload FROM events WHERE plan_id=? AND kind='reserve_verification' ORDER BY sequence", (self.plan_id,))
            if json.loads(raw).get("use_id") == anchor))
        return anchor, review_anchor, (rid, ext), sequence, raw

    def corrupt_original_creation(self, sequence, original, change):
        aid = self.a.committed["attempt_id"] if hasattr(self, "a") else self.accepted.attempt_id
        retained = self.f.store.read(lambda db: (
            db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (aid,)).fetchone()[0],
            tuple(db.execute("SELECT * FROM resource_uses WHERE attempt_id=? ORDER BY rowid", (aid,)))))
        def corrupt(db):
            event = json.loads(original)
            change(event)
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(event), sequence))
            plans._insert_event(db, self.plan_id, None, "later_clean_anchor_reference", "owner",
                                self.f.clock().isoformat(), json.loads(original))
        self.f.store.write(corrupt)
        after = self.f.store.read(lambda db: (
            db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (aid,)).fetchone()[0],
            tuple(db.execute("SELECT * FROM resource_uses WHERE attempt_id=? ORDER BY rowid", (aid,)))))
        self.assertEqual(after, retained)  # No nested group/namespace/physical-row alteration supplies this attack.

    def creation_refused(self, anchor, review_anchor, selected, physical_count):
        with self.subTest(probe="existing-cited-review"):
            self.assertFalse(self.satisfied())
        with self.subTest(probe="physical-projection-count-page"):
            rows = self.view().coordination.reservations
            self.assertEqual(len(rows), physical_count)
            self.assertTrue(all(row.actor == "" for row in rows))
            self.assertEqual(self.view().coordination.total_counts["reservations"], physical_count)
            page, paged = None, []
            while True:
                page = self.f.service.read_coordination(self.plan_id, after=None if page is None else page.next_cursor, limit=2)
                self.assertEqual(page.total_counts["reservations"], physical_count)
                paged.extend(item.value for item in page.items if item.kind == "reservation")
                if page.next_cursor is None:
                    break
            self.assertEqual(paged, list(rows))
        with self.subTest(probe="atomic-cited-review-refusal"):
            self.refused(lambda: self.review(use_id=review_anchor))
        with self.subTest(probe="atomic-group-release-refusal"):
            self.refused(lambda: self.release(anchor, selected))

    def test_original_submission_creation_links_cannot_be_repaired_by_later_reference(self):
        variants = {
            "artifact-version": lambda event: event["artifact"].update(version="contradictory"),
            "artifact-digest": lambda event: event["artifact"].update(digest="contradictory"),
            "cycle": lambda event: event["cycle"].update(checkpoint_sequence=1),
            "definition-version": lambda event: event.update(definition_version="other-definition"),
            "typed-frozen-definition": lambda event: event["definition"].update(review_required=1),
            "verifier": lambda event: event.update(verifier="another-reviewer"),
            "optional-plan": lambda event: event.update(plan_id="foreign-plan"),
            "optional-result": lambda event: event.update(result_version="unaccepted-result"),
            "optional-producer": lambda event: event.update(producer_step_id="foreign-producer"),
            "missing-artifact": lambda event: event.pop("artifact"),
            "missing-cycle": lambda event: event.pop("cycle"),
            "missing-definition-version": lambda event: event.pop("definition_version"),
            "missing-verifier": lambda event: event.pop("verifier"),
            "separate-handoff": lambda event: event.update(handoff_from="unowned-source"),
            "separate-stop": lambda event: event.update(stopped_reason="No source actor exists"),
            "separate-stop-transition": lambda event: event["execution_transitions"].append({
                **event["execution_transitions"][0], "role": "implementation", "anchor": None, "use_ids": [], "state": "stopped"}),
        }
        for label, change in variants.items():
            with self.subTest(link=label):
                anchor, review_anchor, selected, sequence, raw = self.creation_case()
                self.corrupt_original_creation(sequence, raw, change)
                self.creation_refused(anchor, review_anchor, selected, 2)

    def test_original_accepted_creation_result_and_producer_links_are_exact(self):
        for label, change in {
            "accepted-result": lambda event: event.update(result_version="foreign-result"),
            "accepted-producer": lambda event: event.update(producer_step_id="foreign-producer"),
        }.items():
            with self.subTest(link=label):
                anchor, review_anchor, selected, sequence, raw = self.creation_case(accepted=True)
                self.corrupt_original_creation(sequence, raw, change)
                self.creation_refused(anchor, review_anchor, selected, 2)

    def test_original_handoff_stop_proof_survives_later_writer_changes_without_repair(self):
        def stop(event):
            return next(item for item in event["execution_transitions"] if item["role"] == "implementation")
        variants = {
            "foreign-handle": lambda event: event.update(handoff_from="foreign-source"),
            "nonoverlapping-original-handle": lambda event: event.update(handoff_from=event["verification_group"]["source_use_ids"][1]),
            "missing-handle": lambda event: event.pop("handoff_from"),
            "blank-stop-reason": lambda event: event.update(stopped_reason="   "),
            "different-stop-reason": lambda event: event.update(stopped_reason="Contradicts original cohort stop"),
            "missing-stop-reason": lambda event: event.pop("stopped_reason"),
            "missing-original-stop": lambda event: event.update(execution_transitions=[item for item in event["execution_transitions"] if item["role"] != "implementation"]),
            "partial-original-cohort": lambda event: stop(event).update(use_ids=stop(event)["use_ids"][:1]),
            "original-stop-state": lambda event: stop(event).update(state="active"),
            "original-stop-reason": lambda event: stop(event).update(reason="Another assertion"),
            "original-stop-epoch": lambda event: stop(event).update(epoch="foreign-epoch"),
            "original-stop-generation": lambda event: stop(event).update(generation="foreign-generation"),
            "original-stop-ownership": lambda event: stop(event).update(ownership_version=999),
            "original-stop-ownership-type": lambda event: stop(event).update(ownership_version=True),
            "original-stop-actor": lambda event: stop(event).update(actor="foreign-owner"),
        }
        for label, change in variants.items():
            with self.subTest(link=label):
                anchor, review_anchor, selected, sequence, raw = self.creation_case(handoff=True, later_writer=True)
                self.corrupt_original_creation(sequence, raw, change)
                self.creation_refused(anchor, review_anchor, selected, 6)

    def test_coherent_original_released_handoff_remains_attributable_after_new_cycle(self):
        anchor, review_anchor, selected, sequence, raw = self.creation_case(handoff=True, later_writer=True)
        self.assertNotEqual(anchor, review_anchor)
        group = next(row for row in self.view().coordination.reservations if row.use_id == anchor)
        self.assertEqual((group.actor, group.writer_active, set(group.resource_ids)), ("reviewer", False, set(selected)))
        self.assertEqual(self.view().coordination.total_counts["reservations"], 4)
        self.release(anchor, selected, stopped=False)
        self.assertTrue(self.satisfied())
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT payload FROM events WHERE sequence=?", (sequence,)).fetchone()[0]), raw)

    def test_coherent_optional_original_creation_links_preserve_delayed_observations(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                anchor, review_anchor, selected, sequence, raw = self.creation_case(accepted=accepted)
                event = json.loads(raw)
                event.update(plan_id=self.plan_id, producer_step_id="a",
                             result_version=self.accepted.result_version if accepted else None,
                             checkpoint_sequence=event["cycle"].get("checkpoint_sequence"))
                self.f.store.write(lambda db: db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(event), sequence)))
                self.assertTrue(self.satisfied())
                self.review(use_id=review_anchor)
                self.release(anchor, selected, stopped=False)
                self.assertTrue(self.satisfied())

    def test_missing_namespace_with_verification_history_is_not_legacy(self):
        self.live()
        rid = self.f.register(workspace("/synthetic/missing-group", 917), self.p)
        self.reserve(context(rid))
        def remove(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.a.committed["attempt_id"],)).fetchone()[0]
            data = json.loads(raw)
            del data["execution_uses"]
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), self.a.committed["attempt_id"]))
        self.f.store.write(remove)
        self.assertTrue(self.view().coordination.reservations[0].writer_active)
        self.assertEqual(self.view().coordination.reservations[0].actor, "")
        self.refused(self.reopen)

    def test_missing_explicit_assignment_namespace_is_not_inferred_as_legacy(self):
        rid = self.f.register(workspace("/synthetic/missing-explicit-role", 930), self.p)
        self.live(context(rid))
        def remove(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.a.committed["attempt_id"],)).fetchone()[0]
            data = json.loads(raw)
            del data["execution_uses"]
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), self.a.committed["attempt_id"]))
        self.f.store.write(remove)
        self.refused(self.reopen)

    def test_coherent_legacy_source_initializes_only_on_authorized_writable_operation(self):
        rid = self.f.register(workspace("/synthetic/legacy-use", 918), self.p)
        separate = self.f.register(workspace("/synthetic/legacy-review", 919), self.p)
        self.live(context(rid))
        def remove(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.a.committed["attempt_id"],)).fetchone()[0]
            data = json.loads(raw)
            data.pop("execution_uses", None)
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), self.a.committed["attempt_id"]))
            # Represent the accepted pre-Task17 assignment format, which contained no execution-role audit marker.
            sequence, raw_event = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='assign_step'",
                                            (self.plan_id,)).fetchone()
            event = json.loads(raw_event)
            event.pop("execution_transitions", None)
            event.pop("definition", None)
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(event), sequence))
        self.f.store.write(remove)
        self.assertEqual(self.original().actor, "worker-a")
        before = self.state()
        self.view()
        self.assertEqual(self.state(), before)
        self.reserve(context(separate))
        self.assertTrue(self.original().writer_active)
        self.reopen()

    def test_orphan_release_uses_any_current_owner_but_owned_use_requires_its_owner(self):
        rid = self.f.register(workspace("/synthetic/orphan-review", 920), self.p)
        orphan = seed_orphan_use(self.f, rid)
        self.release(orphan.use_id, (rid,), stopped=False)
        self.assertTrue(next(r for r in self.view().coordination.reservations if r.use_id == orphan.use_id).unresolved)
        self.release(orphan.use_id, (rid,))
        self.live(context(rid))
        original = self.original()
        other = self.f.create(steps=(step("a"),), creation_id="foreign-release-owner")
        self.refused(lambda: self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": original.use_id, "resource_ids": (rid,), "reason": "Foreign owner", "stopped": True}, receipt=other)), "forbidden")

    def test_group_alias_collapse_changes_current_references_without_rewriting_rows(self):
        self.live()
        ext1 = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "alias-one"), self.p)
        ext2 = "synthetic-retained-alias"
        def coherent_alias(db):
            raw = db.execute("SELECT physical_identity FROM resources WHERE resource_id=?", (ext1,)).fetchone()[0]
            db.execute("INSERT INTO resources(resource_id,host_id,kind,physical_identity,status,created_at) "
                       "VALUES(?,'synthetic-host','external',?,'retired',?)", (ext2, raw, self.f.clock().isoformat()))
            db.execute("INSERT INTO resource_redirects(retired_id,survivor_id,created_at) VALUES(?,?,?)", (ext2, ext1, self.f.clock().isoformat()))
        self.f.store.write(coherent_alias)
        reserved = self.reserve(context(None, external_ids=(ext1, ext2)))
        before = self.f.store.read(lambda db: tuple(db.execute("SELECT use_id,resource_id,created_at FROM resource_uses ORDER BY rowid")))
        self.assertEqual(len(before), 1)  # Two coherent alias inputs collapse to one real physical member.
        self.assertEqual(self.view().coordination.reservations[0].resource_ids, (ext1,))
        self.assertEqual(self.view().coordination.total_counts["reservations"], 1)
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute("SELECT use_id,resource_id,created_at FROM resource_uses ORDER BY rowid"))), before)
        self.release(reserved.committed["use_id"], (ext2,))

    def test_old_epoch_groups_keep_attribution_but_need_current_stop_assertion(self):
        rid = self.f.register(workspace("/synthetic/epoch-implementation", 921), self.p)
        self.live(context(rid))
        reserved = self.reserve(context(rid), handoff=self.original().use_id, reason="Stopped in original epoch")
        self.assertFalse(self.original().writer_active)
        def rotate(db):
            db.execute("UPDATE installation SET epoch='restored-epoch' WHERE id=1")
            db.execute("UPDATE plans SET generation='restored-generation' WHERE plan_id=?", (self.plan_id,))
        self.f.store.write(rotate)
        # The fixtures simulate identity rotation only; no restore/recovery implementation is claimed.
        self.assertTrue(self.original().writer_active)
        self.assertEqual(next(r for r in self.view().coordination.reservations if r.use_id == reserved.committed["use_id"]).actor, "reviewer")
        takeover_command = self.f.command("takeover", {"expected_ownership_version": 1, "reason": "Current restored authority"}, receipt=self.p)
        takeover = self.f.service.execute(replace(takeover_command, generation="restored-generation"))
        self.p = takeover
        self.release(reserved.committed["use_id"], (rid,))
        self.refused(self.reopen, "fenced")

    def test_accepted_historical_origin_does_not_block_new_current_authority_group(self):
        self.accepted_invalidation()
        def old_origin(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.accepted.attempt_id,)).fetchone()[0]
            data = json.loads(raw)
            data.update(epoch="old-origin", generation="old-generation")
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), self.accepted.attempt_id))
        self.f.store.write(old_origin)
        seed_store_hold(self.f, "Synthetic inventory hold")
        self.review()
        rid = self.f.register(workspace("/synthetic/current-historical", 922), self.p)
        self.refused(lambda: self.reserve(context(rid)), "held")
        self.f.store.write(lambda db: db.execute("UPDATE store_hold_reasons SET resolved=1"))
        self.reserve(context(rid))
        self.assertEqual(self.view().coordination.attempts[0].state, "accepted")

    def test_2000_later_rejected_attempts_never_replace_exact_accepted_producer(self):
        self.accepted_invalidation()
        def later(db):
            raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (self.accepted.attempt_id,)).fetchone()[0]
            data = json.loads(raw)
            data.pop("result_version")
            for _ in range(2000):
                aid, review_id = str(uuid.uuid4()), str(uuid.uuid4())
                db.execute("INSERT INTO attempts(plan_id,attempt_id,step_id,assignee,state,version,definition_version,payload) "
                           "VALUES(?,?,'a','later-rejected','rejected',4,'v1',?)", (self.plan_id, aid, json.dumps(data)))
                db.execute("INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,created_at) "
                           "VALUES(?,?,?,'a','later-reviewer','failed','[\"Rejected finding\"]',?,?,?)",
                           (review_id, self.plan_id, aid, json.dumps(plans.to_jsonable(self.value.evidence)),
                            json.dumps(plans.to_jsonable(self.value.artifact)), self.f.clock().isoformat()))
                plans._insert_event(db, self.plan_id, None, "rejected_history_review", "later-reviewer", self.f.clock().isoformat(),
                                    {"review_id": review_id, "attempt_id": aid, "step_id": "a", "definition": data["definition"],
                                     "definition_version": "v1", "artifact": plans.to_jsonable(self.value.artifact)})
        self.f.store.write(later)
        recorded = self.review()
        row = self.f.store.read(lambda db: db.execute("SELECT attempt_id FROM reviews WHERE review_id=?", (recorded.committed["review_id"],)).fetchone()[0])
        self.assertEqual(row, self.accepted.attempt_id)
        self.assertTrue(self.satisfied())

    def test_partial_handoff_and_request_failure_roll_back_all_state(self):
        rid = self.f.register(workspace("/synthetic/rollback-review", 923), self.p)
        self.live(context(rid))
        before = self.state()
        with patch.object(plans, "_commit_projection", side_effect=RuntimeError("Injected projection failure")):
            with self.assertRaises(RuntimeError):
                self.reserve(context(rid), handoff=self.original().use_id, reason="Stopped assertion")
        self.assertEqual(self.state(), before)
        self.f.store.write(lambda db: db.execute("CREATE TRIGGER fail_review_receipt BEFORE INSERT ON requests "
                                                "BEGIN SELECT RAISE(ABORT,'Injected receipt failure'); END"))
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.review()
        self.assertEqual(self.state(), before)

    def test_independent_connection_contention_preserves_source_on_commit_and_rollback(self):
        with ExitStack() as _owned_store_lifetimes:
            for failing in (False, True):
                with self.subTest(failing=failing):
                    f = _owned_store_lifetimes.enter_context(make_service())
                    self.addCleanup(f.cleanup)
                    p = f.create(steps=(step("a", review_required=True),))
                    rid = f.register(workspace("/synthetic/contention", 924), p)
                    assigned = f.service.execute(f.command("assign_step", {"step_id": "a", "assignee": "worker-a", "context": context(rid)}, receipt=p))
                    active = f.start(p, assigned)
                    f.submit(p, active, self.value)
                    source = f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
                    third = f.create(steps=(step("a"),), creation_id="contention-third")
                    other_store = _owned_store_lifetimes.enter_context(Store(f.store.path, f.clock, host_id="synthetic-host"))
                    other = PlanningService(other_store, base_url="http://127.0.0.1:8790", clock=f.clock)
                    handoff = f.command("reserve_verification", {"step_id": "a", "verifier": "reviewer", "context": context(rid),
                                       "handoff_from": source.use_id, "stopped_reason": "Actor stopped across source"}, receipt=p)
                    assign = f.command("assign_step", {"step_id": "a", "assignee": "third", "context": context(rid)}, receipt=third)
                    entered, release = threading.Event(), threading.Event()
                    real_projection, real_connect = plans._commit_projection, other_store._connect
                    def barrier_projection(db, pid, previous):
                        self.assertEqual(db.execute("SELECT status FROM resource_uses WHERE use_id=?", (source.use_id,)).fetchone()[0], "active")
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError("Contention synchronization timed out")
                        if failing:
                            raise RuntimeError("Injected handoff rollback")
                        return real_projection(db, pid, previous)
                    def no_wait_connect():
                        db = real_connect()
                        db.execute("PRAGMA busy_timeout=0")
                        return db
                    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(plans, "_commit_projection", barrier_projection), patch.object(other_store, "_connect", no_wait_connect):
                        future = pool.submit(f.service.execute, handoff)
                        try:
                            self.assertTrue(entered.wait(10))
                            with self.assertRaises(DomainError) as error:
                                other.execute(assign)
                            self.assertEqual(error.exception.code, "busy")
                            self.assertTrue(error.exception.message.startswith("Database is busy:"))  # Actual BEGIN IMMEDIATE, beyond connection setup.
                        finally:
                            release.set()
                        if failing:
                            with self.assertRaises(RuntimeError):
                                future.result(timeout=10)
                        else:
                            future.result(timeout=10)
                    with self.assertRaises(DomainError) as error:
                        other.execute(assign)
                    self.assertEqual(error.exception.code, "resource_conflict")
                    self.assertEqual(f.store.read(lambda db: db.execute("SELECT status FROM resource_uses WHERE use_id=?", (source.use_id,)).fetchone()[0]), "active")

    def test_transport_exposes_exact_semantic_review_operations(self):
        from adaptive_planning.transport import build_mcp
        self.live()
        async def check():
            server = build_mcp(self.f.service)
            names = {tool.name for tool in await server.list_tools()}
            self.assertTrue({"record_review", "reserve_verification", "release_resource_use", "reopen_attempt"} <= names)
            command = self.command("record_review", {"step_id": "a", "review": Review(
                "reviewer", self.value.artifact, "passed", (), self.value.evidence, None)})
            result = await server.call_tool("record_review", {"command": plans.to_jsonable(command)})
            self.assertFalse(result.is_error)
        asyncio.run(check())
        self.assertTrue(self.satisfied())

    def test_failed_review_retirement_preserves_evidence(self):
        with ExitStack() as _owned_store_lifetimes:
            self.f.cleanup()
            self.f = _owned_store_lifetimes.enter_context(make_service())
            self.addCleanup(self.f.cleanup)
            self.p = self.f.create("first-step", steps=(step("a", review_required=True),), creation_id="failed-review")
            self.plan_id = self.p.current_metadata.plan_id
            rid = self.f.register(workspace("/synthetic/failed-review", 500), self.p)
            self.live(context(rid))
            self.review(verdict="failed", findings=("synthetic defect",))
            before = self.view()
            self.f.service.execute(self.command("revise_plan", {
                "draft": before.draft, "reason": "replace unsuccessful review route", "authorization_change": None,
                "retire_steps": (RetirementRequest("a", "preserve failed result"),)}))
            after = self.view()
            self.assertEqual(after.legacy_snapshot["steps"][0]["status"], "retired")
            self.assertEqual(after.legacy_snapshot["steps"][0]["evidence"], before.legacy_snapshot["steps"][0]["evidence"])
            self.assertEqual(after.coordination.reviews, before.coordination.reviews)
            self.assertTrue(after.coordination.reservations)
            self.assertEqual(after.draft.authorization, before.draft.authorization)
            self.assertEqual(after.draft.authorization.claimed, "a")
            self.refused(lambda: self.f.service.execute(self.command("report_activity", {
                "origin_worker": "worker-a", "relay": None, "observed_activity": "late mutation"}, attempt=self.a)), "fenced")


if __name__ == "__main__":
    unittest.main()
