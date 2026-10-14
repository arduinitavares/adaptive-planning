"""Explicit owner acceptance, immutable proof, and authoritative derived status."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import unittest
from unittest import mock
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from adaptive_planning import attempts, core, plans, reviews
from adaptive_planning.types import AuthorizationChange, Availability, DomainError, Evidence, ResourceClaim, RetirementRequest, Review
from tests.service_fixtures import checkpoint, context, draft, make_service, seed_orphan_use, step, workspace


FACT_KEYS = (
    "recorded_nonstale_live_attempts", "global_holds", "owned_nonreleased_uses",
    "recorded_started_attempts", "logical_executable_pending_steps", "explicit_block_controls",
)


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = make_service()
        self.addCleanup(self.f.cleanup)

    def submitted(self, *, required=False, writable=False, steps=None, target="a"):
        p = self.f.create(steps=steps or (step("a", review_required=required),))
        rid = self.f.register(workspace("/synthetic/accept", 811), p) if writable else None
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": target, "assignee": "worker-a", "context": context(rid, writes=writable),
        }, receipt=p))
        active = self.f.start(p, assigned)
        value = checkpoint()
        published = self.f.submit(p, active, value)
        return p, assigned, active, published, value, rid

    def accept_command(self, p, a, value, *, ids=(), reason=None, key=None):
        return self.f.command("accept_step", {
            "step_id": a.committed.get("step_id", "a"), "artifact": value.artifact,
            "stopped_resource_ids": ids, "stopped_reason": reason,
        }, receipt=p, attempt=a, key=key)

    def review(self, p, value, *, verdict="passed", use=None, target="a"):
        review = Review("reviewer", value.artifact, verdict,
                        ("fix required",) if verdict == "failed" else (), value.evidence, use)
        return self.f.service.execute(self.f.command("record_review", {"step_id": target, "review": review}, receipt=p))

    def revise(self, p, **changes):
        draft = replace(self.f.service.read_plan(p.current_metadata.plan_id).draft, **changes)
        return self.f.service.execute(self.f.command("revise_plan", {
            "draft": draft, "reason": "synthetic authorized revision", "authorization_change": None, "retire_steps": (),
        }, receipt=p))

    def row(self, p, a):
        return self.f.store.read(lambda db: db.execute(
            "SELECT state,version,started_at,payload FROM attempts WHERE plan_id=? AND attempt_id=?",
            (p.current_metadata.plan_id, a.committed["attempt_id"])).fetchone())

    def authority(self):
        queries = {name: f"SELECT * FROM {name}" for name in
                   ("plans", "steps", "attempts", "resource_uses", "result_versions", "events", "holds")}
        queries.update(capabilities="SELECT capability_id,epoch,plan_id,role,revoked,payload FROM capabilities",
                       requests="SELECT request_id,epoch,key,command_digest,plan_id,created_at FROM requests")
        return self.f.store.read(lambda db: tuple((name, tuple(db.execute(sql))) for name, sql in queries.items()))

    def live_accept(self, p, target, *, bindings=(), version="v1"):
        assigned = self.f.assign(p, target)
        if bindings:
            self.f.service.execute(self.f.command("bind_availability", {
                "bindings": tuple(bindings), "checked_base": "synthetic-consumer-base",
            }, receipt=p, attempt=assigned))
        active = self.f.start(p, assigned)
        value = checkpoint(artifact_version=version)
        self.f.submit(p, active, value)
        definition = next(s for s in self.f.service.read_plan(p.current_metadata.plan_id).draft.steps if s.step_id == target)
        if definition.review_required:
            self.review(p, value, target=target)
        before = json.loads(self.row(p, active)[3])
        accepted = self.f.accept(p, active, value)
        after = json.loads(self.row(p, active)[3])
        for key in ("context", "last_activity", "definition", "availability_bindings"):
            if key in before:
                self.assertEqual(after[key], before[key])
        return accepted, value

    def binding(self, producer, value, target):
        return Availability(target, producer.committed["result_version"], "ancestor", "synthetic-consumer-base", None, value.evidence[0])

    def invalidate(self, p, producer):
        return self.f.service.execute(self.f.command("invalidate_result", {
            "step_id": producer.committed["step_id"], "result_version": producer.committed["result_version"],
            "reason": "later failed assumption", "evidence": checkpoint("failed").evidence,
        }, receipt=p))

    def resolve(self, p, invalid, target, value, *, disposition="revalidated", corrective=None, result=None):
        return self.f.service.execute(self.f.command("resolve_invalidation", {
            "invalidation_id": invalid.committed["invalidation_id"], "affected_step_id": target,
            "disposition": disposition, "corrective_step_id": corrective, "corrected_result_version": result,
            "evidence": value.evidence, "reason": "explicit proved resolution",
        }, receipt=p))

    def final_gate_state(self):
        # Omit request responses and capability secrets from rollback diagnostics.
        retained = self.f.store.read(lambda db: tuple(
            (table, tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid")))
            for table in ("checkpoints", "reviews", "creation_ids")))
        return self.authority(), retained

    def final_gate_refused(self, command, code="invalid"):
        before = self.final_gate_state()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(command)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.final_gate_state(), before)

    def gate_revision_command(self, p, *, steps=None, retirements=(), change=None, **changes):
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        revised = replace(current, steps=current.steps if steps is None else steps, **changes)
        return self.f.command("revise_plan", {
            "draft": revised, "reason": "Explicit final-review graph decision",
            "authorization_change": change, "retire_steps": retirements,
        }, receipt=p)

    def bind_gate_inputs(self, p, assigned, bindings):
        return self.f.service.execute(self.f.command("bind_availability", {
            "bindings": tuple(bindings), "checked_base": "synthetic-consumer-base",
        }, receipt=p, attempt=assigned))

    def gate_worker_command(self, kind, p, attempt, payload, *, key=None):
        command = self.f.command(kind, payload, receipt=p, attempt=attempt, key=key)
        return replace(command, expected_revision=None, ownership_version=None)

    def accepted_gate_history(self, p, target):
        pid = p.current_metadata.plan_id
        return self.f.store.read(lambda db: {
            table: tuple(db.execute(sql, (pid, target))) for table, sql in {
                "steps": "SELECT * FROM steps WHERE plan_id=? AND step_id=?",
                "attempts": "SELECT * FROM attempts WHERE plan_id=? AND step_id=? ORDER BY rowid",
                "results": "SELECT * FROM result_versions WHERE plan_id=? AND step_id=? ORDER BY rowid",
                "checkpoints": "SELECT * FROM checkpoints WHERE plan_id=?1 AND attempt_id IN "
                               "(SELECT attempt_id FROM attempts WHERE plan_id=?1 AND step_id=?2) ORDER BY rowid",
                "acceptance": "SELECT sequence,plan_revision,kind,payload FROM events WHERE plan_id=? "
                              "AND kind='accept_step' AND json_extract(payload,'$.step_id')=? ORDER BY sequence",
            }.items()})

    def test_final_gate_creation_requires_explicit_review_transactionally(self):
        malformed = draft(steps=(step("final", final_gate=True),))
        command = self.f.command("create_plan", {
            "creation_id": "final-invariant", "draft": malformed, "new_plan_intent": None,
        }, key="final-create")
        self.final_gate_refused(command)
        self.assertEqual(self.f.service.list_plans(include_terminal=True), ())
        corrected = replace(malformed, steps=(step("final", final_gate=True, review_required=True),))
        accepted = self.f.service.execute(replace(command, payload={**command.payload, "draft": corrected}))
        self.assertFalse(accepted.replayed)
        self.assertEqual(self.f.service.read_plan(accepted.current_metadata.plan_id).draft.steps, corrected.steps)

    def test_final_gate_revision_requires_explicit_review_transactionally(self):
        p = self.f.create(steps=(step("a"),))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        malformed = replace(current, steps=(*current.steps, step("final", ("a",), final_gate=True)))
        command = self.f.command("revise_plan", {
            "draft": malformed, "reason": "Add the final review gate", "authorization_change": None,
            "retire_steps": (),
        }, receipt=p, key="final-revision")
        self.final_gate_refused(command)
        corrected = replace(malformed, steps=(*current.steps, step("final", ("a",), final_gate=True, review_required=True)))
        applied = self.f.service.execute(replace(command, payload={**command.payload, "draft": corrected}))
        self.assertFalse(applied.replayed)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).draft.steps, corrected.steps)

    def test_final_gate_reassessment_apply_requires_review_but_proposal_is_diagnostic(self):
        p = self.f.create(steps=(step("a"),))
        active = self.f.start(p, self.f.assign(p))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        malformed = replace(current, steps=(*current.steps, step("final", ("a",), final_gate=True)))
        value = checkpoint(affected=("a",))
        value = replace(value, reassessment=replace(value.reassessment, proposed_draft=malformed))
        published = self.f.submit(p, active, value)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).draft, current)
        command = self.f.command("resolve_reassessment", {
            "checkpoint_id": published.committed["checkpoint_id"], "decision": "apply",
            "reason": "Apply the proposed final gate", "draft": malformed, "affected_steps": (),
            "unbounded": False,
        }, receipt=p, key="final-reassessment")
        self.final_gate_refused(command)
        corrected = replace(malformed, steps=(*current.steps, step("final", ("a",), final_gate=True, review_required=True)))
        applied = self.f.service.execute(replace(command, payload={**command.payload, "draft": corrected}))
        self.assertFalse(applied.replayed)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.draft.steps, corrected.steps)
        self.assertFalse(view.coordination.has_plan_holds)

    def test_final_review_not_circular_task_gate(self):
        p = self.f.create(steps=(step("a"), step("final", ("a",), final_gate=True, review_required=True)),
                          creation_id="final-plan")
        prior, prior_value = self.live_accept(p, "a")
        pid = p.current_metadata.plan_id
        original = self.accepted_gate_history(p, "a")
        view = self.f.service.read_plan(pid)
        self.assertEqual(view.coordination.reviews, ())
        self.assertTrue({s.step_id: s.ready for s in self.f.service.list_ready(pid)}["final"])
        self.assertNotEqual(view.status, "complete")
        assigned = self.f.assign(p, "final")
        self.final_gate_refused(self.gate_worker_command("start_step", p, assigned, {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "Start after checking inputs",
        }))
        binding = self.binding(prior, prior_value, "a")
        self.assertNotEqual(binding.result_version, prior_value.artifact.version)
        self.final_gate_refused(self.f.command("bind_availability", {
            "bindings": (replace(binding, result_version=prior_value.artifact.version),),
            "checked_base": "synthetic-consumer-base",
        }, receipt=p, attempt=assigned))
        self.bind_gate_inputs(p, assigned, (binding,))
        active = self.f.start(p, assigned)
        value = checkpoint()
        self.f.submit(p, active, value)
        self.final_gate_refused(self.accept_command(p, active, value))
        self.review(p, value, verdict="failed", target="final")
        self.final_gate_refused(self.accept_command(p, active, value))
        self.f.service.execute(self.f.command("reopen_attempt", {
            "reason": "Correct the failed final artifact explicitly",
        }, receipt=p, attempt=active))
        corrected = checkpoint(artifact_version="v2")
        self.f.submit(p, active, corrected)
        self.final_gate_refused(self.f.command("record_review", {
            "step_id": "final", "review": Review("reviewer", value.artifact, "passed", (), value.evidence, None),
        }, receipt=p))
        self.final_gate_refused(self.accept_command(p, active, corrected))
        passed = self.review(p, corrected, target="final")
        command = self.accept_command(p, active, corrected)
        self.assertIsNone(command.attempt_token)
        accepted = self.f.service.execute(command)
        proof = json.loads(self.row(p, active)[3])
        self.assertEqual(proof["review_id"], passed.committed["review_id"])
        self.assertEqual(proof["artifact"], plans.to_jsonable(corrected.artifact))
        self.assertTrue(proof["definition"]["final_gate"])
        self.assertTrue(proof["definition"]["review_required"])
        uuid.UUID(accepted.committed["result_version"])
        self.assertEqual(self.f.service.read_plan(pid).status, "complete")
        self.assertEqual(self.accepted_gate_history(p, "a"), original)

    def test_multiple_final_gates_use_ordinary_dependencies_without_sink_or_closure(self):
        p = self.f.create(steps=(step("a"), step("first-final", ("a",), final_gate=True, review_required=True),
                                step("independent-final", final_gate=True, review_required=True),
                                step("tail", ("first-final",))))
        self.live_accept(p, "independent-final")
        prior, value = self.live_accept(p, "a")
        final, final_value = self.live_accept(p, "first-final", bindings=(self.binding(prior, value, "a"),))
        self.assertNotEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")
        self.live_accept(p, "tail", bindings=(self.binding(final, final_value, "first-final"),))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_final_gate_flags_do_not_replace_core_cycle_validation(self):
        command = self.f.command("create_plan", {
            "creation_id": "final-cycle", "new_plan_intent": None,
            "draft": draft(steps=(step("a", ("final",)), step("final", ("a",), final_gate=True, review_required=True))),
        })
        self.final_gate_refused(command)

    def test_legacy_final_gate_survivor_requires_repair_when_legally_mutable(self):
        # TEST-ONLY pre-invariant creation; all other service guards remain active.
        with mock.patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            p = self.f.create(steps=(step("legacy", final_gate=True),))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        self.final_gate_refused(self.gate_revision_command(p, facts=(*current.facts, "A new observed fact")))
        repaired = replace(current.steps[0], review_required=True, definition_version="v2")
        self.f.service.execute(self.gate_revision_command(p, steps=(repaired,)))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).draft.steps, (repaired,))
        self.live_accept(p, "legacy")
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_legacy_final_gate_retirement_exception_requires_exact_existing_definition(self):
        # TEST-ONLY pre-invariant creation; retirement/cancellation are real public operations.
        with mock.patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            p = self.f.create(steps=(step("legacy", final_gate=True),))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        retirement = RetirementRequest("legacy", "End the old route without changing its definition")
        cases = (
            ((replace(current.steps[0], title="Changed old definition"),), (retirement,)),
            ((*current.steps, step("new-bad", final_gate=True)),
             (retirement, RetirementRequest("new-bad", "New malformed work cannot borrow the exception"))),
            (current.steps, (retirement, RetirementRequest("unknown", "Unknown retirement still refuses"))),
            (current.steps, (retirement, retirement)),
            (current.steps, (RetirementRequest("legacy", " "),)),
        )
        for definitions, retirements in cases:
            self.final_gate_refused(self.gate_revision_command(p, steps=definitions, retirements=retirements))
        replacement = step("replacement", final_gate=True, review_required=True)
        self.f.service.execute(self.gate_revision_command(p, steps=(*current.steps, replacement), retirements=(retirement,)))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.draft.steps, (*current.steps, replacement))
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "retired")

    def test_legacy_retired_final_gate_history_retention_is_exact(self):
        # TEST-ONLY pre-invariant flags; no history, auth or graph bypass.
        with mock.patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            p = self.f.create(steps=(step("legacy", final_gate=True),))
            self.f.service.execute(self.gate_revision_command(
                p, retirements=(RetirementRequest("legacy", "Retain the pre-invariant closed definition"),)))
        prior = self.f.service.read_plan(p.current_metadata.plan_id)
        replacement = step("replacement", final_gate=True, review_required=True)
        self.f.service.execute(self.gate_revision_command(p, steps=(*prior.draft.steps, replacement)))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        self.assertEqual(current.steps[0], prior.draft.steps[0])
        for changed in (replace(current.steps[0], title="Changed closed history"),
                        replace(current.steps[0], review_required=True)):
            with self.subTest(changed=changed):
                self.final_gate_refused(self.gate_revision_command(p, steps=(changed, *current.steps[1:])))
        self.final_gate_refused(self.gate_revision_command(p, steps=(*current.steps, step("new-bad", final_gate=True))))
        self.live_accept(p, "replacement")
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_legacy_submitted_final_gate_reassessment_cannot_borrow_retirement_intent(self):
        # TEST-ONLY pre-invariant flags; this is an actual submitted checkpoint.
        with mock.patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            p, _, active, published, value, _ = self.submitted(steps=(step("a", final_gate=True),))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        replacement = step("replacement", final_gate=True, review_required=True)
        revised = replace(current, steps=(*current.steps, replacement))
        self.final_gate_refused(self.f.command("resolve_reassessment", {
            "checkpoint_id": published.committed["checkpoint_id"], "decision": "apply",
            "reason": "Reassessment has no retirement payload", "draft": revised, "affected_steps": (), "unbounded": False,
        }, receipt=p))
        self.final_gate_refused(self.gate_revision_command(
            p, steps=(replace(current.steps[0], review_required=True),)))
        self.f.service.execute(self.gate_revision_command(
            p, steps=revised.steps, retirements=(RetirementRequest("a", "Explicitly retire the frozen legacy route"),)))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.draft.steps[0], current.steps[0])
        self.assertEqual(view.coordination.attempts[0].state, "released")
        self.assertEqual(json.loads(self.row(p, active)[3])["definition"], plans.to_jsonable(current.steps[0]))
        self.assertEqual(view.legacy_snapshot["steps"][0]["evidence"], [plans.format_evidence(e) for e in value.evidence])

    def test_legacy_accepted_final_gate_history_replay_and_invalidation_remain_available(self):
        original_draft = draft(steps=(step("legacy", final_gate=True),))
        original_create = self.f.command("create_plan", {
            "creation_id": "legacy-accepted", "draft": original_draft, "new_plan_intent": None,
        }, key="legacy-create")
        # TEST-ONLY pre-invariant fixture: a real unreviewed false-flag acceptance,
        # with all original auth, availability, checkpoint and proof checks enabled.
        with mock.patch.object(reviews, "_validate_final_gate_flags", return_value=None, create=True):
            p = self.f.service.execute(original_create)
            active = self.f.start(p, self.f.assign(p, "legacy"))
            value = checkpoint()
            original_checkpoint = self.gate_worker_command("record_checkpoint", p, active, {"checkpoint": value},
                                                           key="legacy-checkpoint")
            self.f.service.execute(original_checkpoint)
            original_accept = self.accept_command(p, active, value, key="legacy-accept")
            accepted = self.f.service.execute(original_accept)
        original = self.accepted_gate_history(p, "legacy")
        self.assertNotIn("review_id", json.loads(self.row(p, active)[3]))
        for command in (original_create, original_checkpoint, original_accept):
            with self.subTest(kind=command.kind):
                before = self.final_gate_state()
                replay = self.f.service.execute(command)
                self.assertTrue(replay.replayed)
                self.assertEqual(self.final_gate_state(), before)
        creation_replay = self.f.service.execute(replace(original_create, key="legacy-creation-id-replay"))
        self.assertTrue(creation_replay.replayed)
        self.assertEqual(creation_replay.committed_metadata.plan_id, p.current_metadata.plan_id)
        self.final_gate_refused(self.gate_revision_command(
            p, retirements=(RetirementRequest("legacy", "Accepted history remains ineligible for retirement"),)))
        current = self.f.service.read_plan(p.current_metadata.plan_id).draft
        self.final_gate_refused(self.gate_revision_command(
            p, steps=(replace(current.steps[0], review_required=True),)))
        invalidation = self.invalidate(p, accepted)
        self.review(p, value, target="legacy")
        self.resolve(p, invalidation, "legacy", value, result=accepted.committed["result_version"])
        self.assertEqual(self.accepted_gate_history(p, "legacy"), original)
        self.f.service.execute(self.gate_revision_command(p, steps=(*current.steps, step("consumer", ("legacy",)))))
        self.live_accept(p, "consumer", bindings=(self.binding(accepted, value, "legacy"),))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_failed_final_review_corrective_history(self):
        p = self.f.create("first-step", steps=(step("a"), step("final", ("a",), final_gate=True, review_required=True)),
                          creation_id="failed-final-plan")
        pid = p.current_metadata.plan_id
        prior, prior_value = self.live_accept(p, "a")
        original_history = self.accepted_gate_history(p, "a")
        self.final_gate_refused(self.f.command("assign_step", {
            "step_id": "final", "assignee": "worker-a", "context": context(None, writes=False),
        }, receipt=p), "forbidden")
        self.f.service.execute(self.gate_revision_command(
            p, change=AuthorizationChange("first-step", "User authorized execution of the final-review step")))
        rid = self.f.register(workspace("/synthetic/final-corrective", 851), p)
        eid = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None,
                                           "final-corrective-external"), p)
        execution = context(rid, external_ids=(eid,))
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "final", "assignee": "worker-a", "context": execution,
        }, receipt=p))
        self.bind_gate_inputs(p, assigned, (self.binding(prior, prior_value, "a"),))
        active = self.f.start(p, assigned)
        failed_value = checkpoint()
        published = self.f.submit(p, active, failed_value)
        original_uses = self.f.service.read_plan(pid).coordination.reservations
        verifier = self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "final", "verifier": "reviewer", "context": execution,
            "handoff_from": original_uses[0].use_id, "stopped_reason": "Implementation cohort stopped for verification",
        }, receipt=p))
        failed = self.review(p, failed_value, verdict="failed", use=verifier.committed["use_id"], target="final")
        self.assertEqual(failed.committed["attempt_id"], active.committed["attempt_id"])
        self.final_gate_refused(self.accept_command(p, active, failed_value))
        self.f.service.execute(self.f.command("resolve_reassessment", {
            "checkpoint_id": published.committed["checkpoint_id"], "decision": "hold",
            "reason": "Retain the final-review uncertainty until explicit classification", "draft": None,
            "affected_steps": ("final",), "unbounded": False,
        }, receipt=p))
        self.final_gate_refused(self.accept_command(p, active, failed_value), "held")
        before = self.f.service.read_plan(pid)
        self.assertEqual(before.draft.authorization.claimed, "final")
        old_checkpoint = self.f.store.read(lambda db: tuple(db.execute(
            "SELECT * FROM checkpoints WHERE plan_id=? AND attempt_id=?", (pid, active.committed["attempt_id"]))))
        corrective = step("fix", ("a",))
        replacement = step("final-2", ("a", "fix"), final_gate=True, review_required=True)
        retirements = (RetirementRequest("final", "Retain the failed review and replace this route"),)
        for definitions in (
                (*before.draft.steps, replace(corrective, dependencies=("final-2",)), replacement),
                (*before.draft.steps, corrective, replace(replacement, dependencies=("final", "fix")))):
            with self.subTest(dependencies=definitions[-1].dependencies):
                self.final_gate_refused(self.gate_revision_command(p, steps=definitions, retirements=retirements))
        revised_steps = (*before.draft.steps, corrective, replacement)
        self.f.service.execute(self.gate_revision_command(p, steps=revised_steps, retirements=retirements))
        retired = self.f.service.read_plan(pid)
        self.assertEqual(retired.draft.authorization, before.draft.authorization)
        self.assertEqual(retired.draft.steps[:2], before.draft.steps)
        self.assertEqual(retired.coordination.reviews, before.coordination.reviews)
        self.assertEqual(retired.legacy_snapshot["steps"][1]["status"], "retired")
        self.assertEqual(retired.legacy_snapshot["steps"][1]["evidence"], before.legacy_snapshot["steps"][1]["evidence"])
        cancelled = json.loads(self.row(p, active)[3])
        self.assertEqual(self.row(p, active)[0], "released")
        self.assertEqual(cancelled["cancelled_from"], "submitted")
        self.assertEqual(cancelled["definition"], plans.to_jsonable(before.draft.steps[1]))
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute(
            "SELECT * FROM checkpoints WHERE plan_id=? AND attempt_id=?", (pid, active.committed["attempt_id"])))), old_checkpoint)
        self.assertEqual(retired.coordination.total_counts["owned_nonreleased_uses"], 4)
        self.assertTrue(retired.coordination.has_unresolved_uses)
        self.assertTrue(retired.coordination.has_plan_holds)
        self.assertNotEqual(retired.status, "complete")
        self.final_gate_refused(self.gate_worker_command("report_activity", p, active, {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "Cancelled route cannot progress",
        }), "fenced")
        self.final_gate_refused(self.f.command("assign_step", {
            "step_id": "fix", "assignee": "worker-a", "context": context(None, writes=False),
        }, receipt=p), "forbidden")
        self.f.service.execute(self.gate_revision_command(
            p, change=AuthorizationChange("first-step", "User authorized the corrective step after failed review")))
        self.final_gate_refused(self.f.command("assign_step", {
            "step_id": "fix", "assignee": "worker-a", "context": execution,
        }, receipt=p), "resource_conflict")
        self.final_gate_refused(self.f.command("release_resource_use", {
            "use_id": original_uses[0].use_id, "resource_ids": original_uses[0].resource_ids,
            "reason": "The linked verifier is still outstanding", "stopped": True,
        }, receipt=p), "resource_conflict")
        self.final_gate_refused(self.f.command("release_resource_use", {
            "use_id": verifier.committed["use_id"], "resource_ids": (rid,),
            "reason": "A partial verifier resource set is insufficient", "stopped": True,
        }, receipt=p))
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": verifier.committed["use_id"], "resource_ids": (rid, eid),
            "reason": "Stop remains unconfirmed", "stopped": False,
        }, receipt=p))
        self.assertEqual(self.f.service.read_plan(pid).coordination.total_counts["owned_nonreleased_uses"], 4)
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": verifier.committed["use_id"], "resource_ids": (rid, eid),
            "reason": "Verifier cohort explicitly stopped", "stopped": True,
        }, receipt=p))
        for use in original_uses:
            self.f.service.execute(self.f.command("release_resource_use", {
                "use_id": use.use_id, "resource_ids": use.resource_ids,
                "reason": "Original failed-gate use explicitly stopped", "stopped": True,
            }, receipt=p))
        self.assertEqual(self.f.service.read_plan(pid).coordination.total_counts["owned_nonreleased_uses"], 0)
        fixed_assignment = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "fix", "assignee": "worker-a", "context": execution,
        }, receipt=p))
        self.bind_gate_inputs(p, fixed_assignment, (self.binding(prior, prior_value, "a"),))
        fixed_active = self.f.start(p, fixed_assignment)
        corrected_value = checkpoint(artifact_version="v2")
        self.f.submit(p, fixed_active, corrected_value)
        corrected = self.f.accept(p, fixed_active, corrected_value)
        after_fix = self.f.service.read_plan(pid)
        self.assertEqual(after_fix.draft.authorization.claimed, "fix")
        self.assertEqual(after_fix.coordination.total_counts["owned_nonreleased_uses"], 2)
        self.final_gate_refused(self.f.command("assign_step", {
            "step_id": "final-2", "assignee": "worker-a", "context": context(None, writes=False),
        }, receipt=p), "forbidden")
        self.f.service.execute(self.gate_revision_command(
            p, change=AuthorizationChange("first-step", "User authorized the replacement final-review step")))
        self.final_gate_refused(self.f.command("assign_step", {
            "step_id": "final-2", "assignee": "worker-a", "context": execution,
        }, receipt=p), "resource_conflict")
        for use in after_fix.coordination.reservations:
            if use.attempt_id == fixed_active.committed["attempt_id"]:
                self.f.service.execute(self.f.command("release_resource_use", {
                    "use_id": use.use_id, "resource_ids": use.resource_ids,
                    "reason": "Corrective implementation use explicitly stopped", "stopped": True,
                }, receipt=p))
        final_assignment = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "final-2", "assignee": "worker-a", "context": execution,
        }, receipt=p))
        self.bind_gate_inputs(p, final_assignment, (self.binding(prior, prior_value, "a"), self.binding(corrected, corrected_value, "fix")))
        final_active = self.f.start(p, final_assignment)
        self.f.submit(p, final_active, corrected_value)
        self.final_gate_refused(self.accept_command(p, final_active, corrected_value))
        final_review = self.review(p, corrected_value, target="final-2")
        final_result = self.f.accept(p, final_active, corrected_value)
        final_proof = json.loads(self.row(p, final_active)[3])
        self.assertEqual(final_proof["review_id"], final_review.committed["review_id"])
        self.assertEqual(final_proof["artifact"], plans.to_jsonable(corrected_value.artifact))
        self.assertEqual(final_proof["definition"]["dependencies"], ["a", "fix"])
        self.assertNotEqual(final_result.committed["result_version"], corrected.committed["result_version"])
        self.assertNotEqual(self.f.service.read_plan(pid).status, "complete")
        self.f.service.execute(self.f.command("resolve_reassessment", {
            "checkpoint_id": published.committed["checkpoint_id"], "decision": "reject", "draft": None,
            "reason": "Explicitly classify the retained old-route uncertainty", "affected_steps": (), "unbounded": False,
        }, receipt=p))
        pending_stop = self.f.service.read_plan(pid)
        self.assertFalse(pending_stop.coordination.has_plan_holds)
        self.assertEqual(pending_stop.coordination.total_counts["owned_nonreleased_uses"], 2)
        self.assertNotEqual(pending_stop.status, "complete")
        final_uses = tuple(u for u in pending_stop.coordination.reservations if u.attempt_id == final_active.committed["attempt_id"])
        for index, use in enumerate(final_uses):
            self.f.service.execute(self.f.command("release_resource_use", {
                "use_id": use.use_id, "resource_ids": use.resource_ids,
                "reason": "Accepted final implementation use explicitly stopped", "stopped": True,
            }, receipt=p))
            if index == 0:
                self.assertNotEqual(self.f.service.read_plan(pid).status, "complete")
        completed = self.f.service.read_plan(pid)
        self.assertEqual(completed.status, "complete")
        self.assertEqual(completed.draft.authorization.claimed, "final-2")
        self.assertEqual(self.accepted_gate_history(p, "a"), original_history)
        old_review = next(r for r in completed.coordination.reviews if r.step_id == "final")
        self.assertEqual((old_review.review.verdict, old_review.review.findings, old_review.review.artifact),
                         ("failed", ("fix required",), failed_value.artifact))
        self.assertEqual(old_review, before.coordination.reviews[0])

    def test_explicit_acceptance_records_original_proof_and_replay(self):
        p, assigned, active, cp, value, _ = self.submitted(required=True)
        review = self.review(p, value)
        command = self.accept_command(p, active, value, key="accept-once")
        self.assertIsNone(command.attempt_token)
        before = self.row(p, active)
        grants_before = self.f.store.read(lambda db: tuple(db.execute("SELECT capability_id,epoch,plan_id,role,revoked,payload FROM capabilities")))
        metadata = self.f.service.read_plan(p.current_metadata.plan_id).metadata
        result = self.f.service.execute(command)
        after = self.row(p, active)
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute("SELECT capability_id,epoch,plan_id,role,revoked,payload FROM capabilities"))), grants_before)
        self.assertEqual(after[:3], ("accepted", before[1] + 1, before[2]))
        before_payload, payload = json.loads(before[3]), json.loads(after[3])
        for key in ("last_activity", "context", "definition", "epoch", "generation", "execution_uses"):
            if key in before_payload:
                self.assertEqual(payload[key], before_payload[key])
        self.assertEqual(result.current_metadata.revision, metadata.revision + 1)
        self.assertEqual(payload["checkpoint_sequence"], int(cp.committed["checkpoint_id"]))
        self.assertEqual(payload["review_id"], review.committed["review_id"])
        self.assertEqual(payload["artifact"], plans.to_jsonable(value.artifact))
        uuid.UUID(payload["result_version"])
        self.assertNotEqual(payload["result_version"], value.artifact.version)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.status, "complete")
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "complete")
        self.assertEqual(view.coordination.result_versions["a"], payload["result_version"])
        snapshot = self.authority()
        self.assertTrue(self.f.service.execute(command).replayed)
        self.assertEqual(self.authority(), snapshot)
        worker_replay = self.f.command("record_checkpoint", {"checkpoint": value}, receipt=p, attempt=active)
        with self.assertRaises(DomainError):
            self.f.service.execute(replace(worker_replay, expected_revision=None, ownership_version=None))
        self.assertTrue(assigned.private.attempt_token == self.f._cached_attempt_grants[assigned.committed["attempt_id"]].attempt_token)
        artifact = self.f.store.read(lambda db: attempts._accepted_output(db, plans._load_plan(db, view.plan_id), "a", payload["result_version"]))
        self.assertEqual(artifact, value.artifact)

    def test_acceptance_requires_checkpoint_current_artifact_review_and_no_hold(self):
        p = self.f.create(steps=(step("a", review_required=True),))
        assigned = self.f.assign(p)
        active = self.f.start(p, assigned)
        with self.assertRaises(DomainError):
            self.f.service.execute(self.accept_command(p, active, checkpoint()))
        value = checkpoint("failed")
        self.f.submit(p, active, value)
        self.review(p, replace(value, evidence=tuple(replace(e, result="passed") for e in value.evidence)))
        with self.assertRaises(DomainError):
            self.f.service.execute(self.accept_command(p, active, value))
        self.f.service.execute(self.f.command("reopen_attempt", {"reason": "authorized fix"}, receipt=p, attempt=active))
        value = checkpoint(artifact_version="v2")
        self.f.submit(p, active, value)
        with self.assertRaises(DomainError):
            self.f.service.execute(self.accept_command(p, active, value))
        self.review(p, value)
        changed = replace(value, artifact=replace(value.artifact, version="different"))
        with self.assertRaises(DomainError):
            self.f.service.execute(self.accept_command(p, active, changed))
        self.f.accept(p, active, value)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_acceptance_target_tuple_authentication_cas_and_rollback(self):
        p, _, active, _, value, _ = self.submitted()
        command = self.accept_command(p, active, value)
        for field in ("attempt_id", "attempt_version", "definition_version"):
            with self.subTest(field=field), self.assertRaises(DomainError):
                self.f.service.execute(replace(command, **{field: None}))
        for changes, expected in (({"attempt_version": command.attempt_version - 1}, "stale"),
                                  ({"expected_revision": command.expected_revision - 1}, "conflict"),
                                  ({"orchestrator_token": "synthetic-bogus"}, "forbidden")):
            snapshot = self.authority()
            with self.assertRaises(DomainError) as error:
                self.f.service.execute(replace(command, **changes))
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(self.authority(), snapshot)
        snapshot = self.authority()
        with mock.patch.object(plans, "_commit_projection", side_effect=RuntimeError("injected publication failure")):
            with self.assertRaises(RuntimeError):
                self.f.service.execute(command)
        self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(command)

    def test_acceptance_keeps_unstopped_obligations(self):
        p, _, active, _, value, _ = self.submitted(writable=True)
        self.f.accept(p, active, value)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "complete")
        self.assertTrue(view.coordination.reservations)
        self.assertNotEqual(view.status, "complete")
        use = view.coordination.reservations[0]
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": use.use_id, "resource_ids": use.resource_ids, "reason": "synthetic recorded stopped use", "stopped": True,
        }, receipt=p))
        self.assertEqual(self.f.service.read_plan(view.plan_id).status, "complete")

    def test_acceptance_optional_release_is_one_atomic_publication(self):
        p, _, active, _, value, rid = self.submitted(writable=True)
        command = self.accept_command(p, active, value, ids=(rid,), reason="implementation stopped")
        snapshot = self.authority()
        with mock.patch.object(plans, "_commit_projection", side_effect=RuntimeError("injected publication failure")):
            with self.assertRaises(RuntimeError):
                self.f.service.execute(command)
        self.assertEqual(self.authority(), snapshot)
        before = self.f.service.read_plan(p.current_metadata.plan_id).metadata.revision
        result = self.f.service.execute(command)
        self.assertEqual(result.current_metadata.revision, before + 1)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")
        events = self.f.service.read_events(p.current_metadata.plan_id)
        self.assertEqual(sum(e.kind == "accept_step" for e in events), 1)
        self.assertFalse(any(e.kind == "release_resource_use" for e in events))

    def test_unaffected_proposer_accepted_while_sibling_held(self):
        p = self.f.create(steps=(step("a"), step("b")))
        a, b = self.f.start(p, self.f.assign(p)), self.f.start(p, self.f.assign(p, "b"))
        value = checkpoint(affected=("b",))
        self.f.submit(p, a, value)
        self.f.submit(p, b, checkpoint())
        accept_a = self.accept_command(p, a, value)
        accept_b = self.accept_command(p, b, checkpoint())
        def execute(command):
            try:
                return self.f.service.execute(command)
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(execute, (accept_a, accept_b)))
        self.assertFalse(isinstance(results[0], str))
        self.assertIn(results[1], ("held", "conflict"))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.result_versions.keys(), {"a"})
        self.assertTrue(any(h.step_id == "b" for h in view.coordination.holds))

    def test_old_review_cannot_accept_changed_artifact(self):
        p, _, active, _, v1, _ = self.submitted(required=True)
        self.review(p, v1)
        self.f.service.execute(self.f.command("reopen_attempt", {"reason": "fix current artifact"}, receipt=p, attempt=active))
        v2 = checkpoint(artifact_version="v2")
        self.f.submit(p, active, v2)
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.accept(p, active, v2)
        self.assertEqual(self.authority(), snapshot)
        self.review(p, v2)
        self.f.accept(p, active, v2)

    def test_failed_review_same_attempt_fix_handoff(self):
        p, assigned, active, _, v1, rid = self.submitted(required=True, writable=True)
        original = self.f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
        verification = self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": "reviewer", "context": context(rid),
            "handoff_from": original.use_id, "stopped_reason": "implementation writer stopped",
        }, receipt=p))
        vid = verification.committed["use_id"]
        self.review(p, v1, verdict="failed", use=vid)
        reopen = self.f.command("reopen_attempt", {"reason": "authorized fix"}, receipt=p, attempt=active)
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(reopen)
        self.assertEqual(error.exception.code, "resource_conflict")
        other = self.f.create(steps=(step("a"),), creation_id="third-fix-writer")
        def third_writer():
            return self.f.service.execute(self.f.command("assign_step", {
                "step_id": "a", "assignee": "worker-a", "context": context(rid),
            }, receipt=other))
        with self.assertRaises(DomainError):
            third_writer()
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": vid, "resource_ids": (rid,), "reason": "verification stopped", "stopped": True,
        }, receipt=p))
        with self.assertRaises(DomainError):
            third_writer()
        self.f.service.execute(self.f.command("reopen_attempt", {"reason": "authorized fix"}, receipt=p, attempt=active))
        with self.assertRaises(DomainError):
            third_writer()
        v2 = checkpoint(artifact_version="v2")
        resubmit = self.f.command("record_checkpoint", {"checkpoint": v2}, receipt=p, attempt=active)
        self.assertTrue(resubmit.attempt_token == assigned.private.attempt_token)
        self.f.service.execute(replace(resubmit, expected_revision=None, ownership_version=None))
        with self.assertRaises(DomainError):
            self.f.accept(p, active, v2)
        self.review(p, v2)
        self.f.accept(p, active, v2)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["attempts"], 1)
        self.assertEqual(view.coordination.attempts[0].attempt_id, assigned.committed["attempt_id"])
        self.assertEqual(view.legacy_snapshot["steps"][0]["status"], "complete")

    def test_single_accepted_node_invalidation_prevents_complete(self):
        p, _, active, _, value, _ = self.submitted(required=True)
        self.review(p, value)
        self.f.accept(p, active, value)
        pid = p.current_metadata.plan_id
        result = self.f.service.read_plan(pid).coordination.result_versions["a"]
        invalid = self.f.service.execute(self.f.command("invalidate_result", {
            "step_id": "a", "result_version": result, "reason": "later failed assumption", "evidence": checkpoint("failed").evidence,
        }, receipt=p))
        self.assertNotEqual(self.f.service.read_plan(pid).status, "complete")
        resolution = {"invalidation_id": invalid.committed["invalidation_id"], "affected_step_id": "a",
                      "disposition": "revalidated", "corrective_step_id": None, "corrected_result_version": result,
                      "evidence": value.evidence, "reason": "passing current artifact recheck"}
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.service.execute(self.f.command("resolve_invalidation", resolution, receipt=p))
        self.assertEqual(self.authority(), snapshot)
        self.review(p, value)
        self.f.service.execute(self.f.command("resolve_invalidation", resolution, receipt=p))
        self.assertEqual(self.f.service.read_plan(pid).status, "complete")

    def test_many_historical_rows_do_not_block_safety_reducers(self):
        from tests.service_fixtures import seed_terminal_history
        p = self.f.create(steps=(step("a"),), creation_id="bounded-history")
        pid = p.current_metadata.plan_id
        self.revise(p, facts=tuple("x" * 9216 for _ in range(1000)))
        rid = self.f.register(workspace("/synthetic/history", 720), p)
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "a", "assignee": "worker-a", "context": context(rid),
        }, receipt=p))
        active = self.f.start(p, assigned)
        value = checkpoint(affected=("a",))
        cp = self.f.submit(p, active, value)
        classification = {"checkpoint_id": cp.committed["checkpoint_id"], "decision": "reject",
                          "reason": "owner classifies effects resolved", "draft": None, "affected_steps": (), "unbounded": False}
        self.f.service.execute(self.f.command("resolve_reassessment", classification, receipt=p))
        self.f.accept(p, active, value)
        producer_before = self.row(p, active)
        seed_terminal_history(self.f, pid, "a", 2000)
        self.assertEqual(self.row(p, active), producer_before)
        self.f.service.execute(self.f.command("resolve_reassessment", {
            **classification, "decision": "hold", "affected_steps": ("a",),
        }, receipt=p))
        self.assertTrue(self.f.service.read_plan(pid).coordination.has_plan_holds)
        self.f.service.execute(self.f.command("resolve_reassessment", classification, receipt=p))
        view = self.f.service.read_plan(pid)
        self.assertEqual(view.coordination.total_counts["attempts"], 2001)
        self.assertEqual(view.coordination.total_counts["reviews"], 2000)
        use = view.coordination.reservations[0]
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": use.use_id, "resource_ids": use.resource_ids, "reason": "writer stopped", "stopped": True,
        }, receipt=p))
        result = view.coordination.result_versions["a"]
        invalid = self.f.service.execute(self.f.command("invalidate_result", {
            "step_id": "a", "result_version": result, "reason": "recheck output", "evidence": value.evidence,
        }, receipt=p))
        self.f.service.execute(self.f.command("resolve_invalidation", {
            "invalidation_id": invalid.committed["invalidation_id"], "affected_step_id": "a", "disposition": "revalidated",
            "corrective_step_id": None, "corrected_result_version": result, "evidence": value.evidence,
            "reason": "passing current artifact recheck",
        }, receipt=p))
        counts, cursor = {}, None
        while True:
            page = self.f.service.read_coordination(pid, after=cursor)
            for item in page.items:
                counts[item.kind] = counts.get(item.kind, 0) + 1
                if item.kind == "review":
                    self.assertEqual(tuple(len(f) for f in item.value.review.findings), (8192, 8192))
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(counts["attempt"], 2001)
        self.assertEqual(counts["review"], 2000)
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT COUNT(*) FROM checkpoints WHERE plan_id=?", (pid,)).fetchone()[0]), 2001)
        event_counts, last = {}, 0
        while True:
            events = self.f.service.read_events(pid, after=last, limit=1000)
            if not events:
                break
            self.assertTrue(all(event.sequence > last for event in events))
            for event in events:
                event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
            last = events[-1].sequence
        self.assertEqual(event_counts["record_checkpoint"], 2001)
        self.assertEqual(event_counts["record_review"], 2000)
        self.assertEqual(event_counts["synthetic_terminal_rejection"], 2000)
        self.assertEqual(self.f.service.read_plan(pid).status, "complete")

    def test_accepted_consumer_superseded_preserves_history(self):
        p = self.f.create(steps=(step("a", review_required=True), step("b", ("a",), review_required=True), step("fix", review_required=True)))
        a, av = self.live_accept(p, "a", version="producer-v1")
        b, bv = self.live_accept(p, "b", bindings=(self.binding(a, av, "a"),), version="consumer-v1")
        fix, fv = self.live_accept(p, "fix", version="corrective-v1")
        before = self.f.service.read_plan(p.current_metadata.plan_id)
        invalid = self.invalidate(p, a)
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.resolve(p, invalid, "b", bv, disposition="superseded", corrective="fix", result=fix.committed["result_version"])
        self.assertEqual(self.authority(), snapshot)
        self.resolve(p, invalid, "b", fv, disposition="superseded", corrective="fix", result=fix.committed["result_version"])
        after = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(after.legacy_snapshot["steps"], before.legacy_snapshot["steps"])
        self.assertEqual(after.coordination.result_versions, before.coordination.result_versions)
        self.assertEqual({h.step_id for h in after.coordination.holds}, {"a"})
        self.assertEqual(after.coordination.result_versions["b"], b.committed["result_version"])
        with self.assertRaises(DomainError):
            self.f.store.read(lambda db: attempts._accepted_output(db, plans._load_plan(db, after.plan_id), "a", a.committed["result_version"]))

    def test_pending_consumer_replanned_dependency(self):
        p = self.f.create(steps=(step("a", review_required=True), step("consumer", ("a",)), step("fix", review_required=True)))
        a, av = self.live_accept(p, "a", version="original")
        fix, fv = self.live_accept(p, "fix", version="correction")
        invalid = self.invalidate(p, a)
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.resolve(p, invalid, "consumer", fv, disposition="replanned", corrective="fix", result=fix.committed["result_version"])
        self.assertEqual(self.authority(), snapshot)
        old = self.f.service.read_plan(p.current_metadata.plan_id).draft
        self.revise(p, steps=tuple(replace(s, dependencies=("fix",), definition_version="v2") if s.step_id == "consumer" else s for s in old.steps))
        self.resolve(p, invalid, "consumer", fv, disposition="replanned", corrective="fix", result=fix.committed["result_version"])
        assigned = self.f.assign(p, "consumer")
        with self.assertRaises(DomainError):
            self.f.start(p, assigned)
        self.f.service.execute(self.f.command("bind_availability", {
            "bindings": (self.binding(fix, fv, "fix"),), "checked_base": "synthetic-consumer-base",
        }, receipt=p, attempt=assigned))
        active = self.f.start(p, assigned)
        value = checkpoint(artifact_version="consumer-current")
        self.f.submit(p, active, value)
        self.f.accept(p, active, value)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual({h.step_id for h in view.coordination.holds}, {"a"})
        self.assertEqual(view.coordination.result_versions["a"], a.committed["result_version"])
        self.assertEqual(view.coordination.attempts[0].definition_version, "v1")

    def test_live_accept_bind_start_and_invalidation(self):
        p = self.f.create(steps=(step("a", review_required=True), step("consumer", ("a",), review_required=True), step("tail", ("consumer",))))
        a, av = self.live_accept(p, "a")
        consumer = self.f.assign(p, "consumer")
        with self.assertRaises(DomainError):
            self.f.start(p, consumer)
        self.f.service.execute(self.f.command("bind_availability", {
            "bindings": (self.binding(a, av, "a"),), "checked_base": "synthetic-consumer-base",
        }, receipt=p, attempt=consumer))
        active = self.f.start(p, consumer)
        value = checkpoint(affected=("consumer",), artifact_version="consumer-v1")
        cp = self.f.submit(p, active, value)
        self.review(p, value, target="consumer")
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.accept(p, active, value)
        self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(self.f.command("resolve_reassessment", {
            "checkpoint_id": cp.committed["checkpoint_id"], "decision": "reject", "reason": "owner resolves provisional effects",
            "draft": None, "affected_steps": (), "unbounded": False,
        }, receipt=p))
        accepted = self.f.accept(p, active, value)
        tail = self.f.assign(p, "tail")
        self.f.service.execute(self.f.command("bind_availability", {
            "bindings": (self.binding(accepted, value, "consumer"),), "checked_base": "synthetic-consumer-base",
        }, receipt=p, attempt=tail))
        self.f.start(p, tail)
        before = self.f.store.read(lambda db: tuple(db.execute("SELECT attempt_id,state,version,payload FROM attempts")))
        self.invalidate(p, a)
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute("SELECT attempt_id,state,version,payload FROM attempts"))), before)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual({h.step_id for h in view.coordination.holds}, {"a", "consumer", "tail"})
        self.assertEqual(view.coordination.result_versions["consumer"], accepted.committed["result_version"])

    def test_acceptance_partial_original_cohort_release_keeps_other_obligation(self):
        p = self.f.create(steps=(step("a"),))
        rid = self.f.register(workspace("/synthetic/partial-cohort", 916), p)
        eid = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "partial-cohort-external"), p)
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "a", "assignee": "worker-a", "context": context(rid, external_ids=(eid,)),
        }, receipt=p))
        active = self.f.start(p, assigned)
        value = checkpoint()
        self.f.submit(p, active, value)
        self.f.service.execute(self.accept_command(p, active, value, ids=(rid,), reason="workspace implementation use stopped"))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["owned_nonreleased_uses"], 1)
        self.assertEqual(view.status, "blocked")
        remaining = next(u for u in view.coordination.reservations if u.resource_ids == (eid,))
        self.assertTrue(remaining.writer_active)
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": remaining.use_id, "resource_ids": (eid,), "reason": "external implementation use stopped", "stopped": True,
        }, receipt=p))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_released_coherent_submission_accepts_without_reacquisition(self):
        p, _, active, _, value, rid = self.submitted(writable=True)
        use = self.f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": use.use_id, "resource_ids": (rid,), "reason": "implementation stopped before acceptance", "stopped": True,
        }, receipt=p))
        other = self.f.create(steps=(step("a"),), creation_id="released-next-writer")
        self.f.service.execute(self.f.command("assign_step", {"step_id": "a", "assignee": "worker-a", "context": context(rid)}, receipt=other))
        self.f.accept(p, active, value)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")
        self.assertEqual(self.f.service.read_plan(other.current_metadata.plan_id).coordination.total_counts["owned_nonreleased_uses"], 1)

    def test_acceptance_stop_scope_preserves_verifier_and_unlisted_rows(self):
        p, _, active, _, value, rid = self.submitted(required=True, writable=True)
        extra = self.f.register(workspace("/synthetic/verifier-extra", 912), p)
        verification = self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": "reviewer", "context": context(extra), "handoff_from": None, "stopped_reason": None,
        }, receipt=p))
        self.review(p, value, use=verification.committed["use_id"])
        for ids, reason in (((), "no asserted resources"), ((rid,), None), ((rid,), " "), ((extra,), "cannot stop another actor")):
            snapshot = self.authority()
            with self.subTest(ids=ids, reason=reason), self.assertRaises(DomainError):
                self.f.service.execute(self.accept_command(p, active, value, ids=ids, reason=reason))
            self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(self.accept_command(p, active, value, ids=(rid,), reason="implementation stopped"))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["owned_nonreleased_uses"], 1)
        self.assertTrue(any(u.actor == "reviewer" and u.writer_active for u in view.coordination.reservations))
        self.assertEqual(view.status, "blocked")

    def test_acceptance_linked_verifier_requires_separate_anchor_release(self):
        p, _, active, _, value, rid = self.submitted(required=True, writable=True)
        original = self.f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
        verifier = self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": "reviewer", "context": context(rid),
            "handoff_from": original.use_id, "stopped_reason": "implementation stopped",
        }, receipt=p))
        self.review(p, value, use=verifier.committed["use_id"])
        snapshot = self.authority()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.accept_command(p, active, value, ids=(rid,), reason="implementation stopped"))
        self.assertEqual(error.exception.code, "resource_conflict")
        self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": verifier.committed["use_id"], "resource_ids": (rid,), "reason": "verifier explicitly stopped", "stopped": True,
        }, receipt=p))
        self.f.service.execute(self.accept_command(p, active, value, ids=(rid,), reason="implementation stopped"))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_physical_obligations_are_distinct_from_logical_verifier_groups(self):
        p = self.f.create(steps=(step("a", review_required=True),))
        rid = self.f.register(workspace("/synthetic/cohort", 913), p)
        eid = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, "synthetic-external"), p)
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "a", "assignee": "worker-a", "context": context(rid, external_ids=(eid,)),
        }, receipt=p))
        active = self.f.start(p, assigned)
        value = checkpoint()
        self.f.submit(p, active, value)
        original = self.f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
        verification = self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": "reviewer", "context": context(rid, external_ids=(eid,)),
            "handoff_from": original.use_id, "stopped_reason": "implementation cohort stopped",
        }, receipt=p))
        self.review(p, value, use=verification.committed["use_id"])
        self.f.accept(p, active, value)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["reservations"], 3)
        self.assertEqual(view.coordination.total_counts["owned_nonreleased_uses"], 4)
        self.assertEqual(plans.derive_plan_status(replace(view, coordination=replace(view.coordination, reservations=()))), "blocked")
        self.f.service.execute(self.f.command("release_resource_use", {
            "use_id": verification.committed["use_id"], "resource_ids": (rid, eid), "reason": "verifier stopped", "stopped": True,
        }, receipt=p))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["owned_nonreleased_uses"], 2)
        self.assertTrue(all(not u.writer_active for u in view.coordination.reservations))
        for use in view.coordination.reservations:
            if use.actor == "worker-a":
                self.f.service.execute(self.f.command("release_resource_use", {
                    "use_id": use.use_id, "resource_ids": use.resource_ids, "reason": "implementation stopped", "stopped": True,
                }, receipt=p))
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_fixture_accept_uses_actual_public_target_from_assignment(self):
        p = self.f.create(steps=(step("a"), step("b")))
        assigned = self.f.assign(p, "b")
        active = self.f.start(p, assigned)
        value = checkpoint()
        self.f.submit(p, active, value)
        self.f.accept(p, assigned, value)
        self.assertEqual(set(self.f.service.read_plan(p.current_metadata.plan_id).coordination.result_versions), {"b"})
        with self.assertRaises(DomainError):
            self.f.accept(p, active, replace(value, artifact=None))

    def test_acceptance_refuses_contradictory_other_live_target_claim(self):
        p, _, active, _, value, _ = self.submitted()
        def corrupt(db):
            row = db.execute("SELECT plan_id,step_id,assignee,version,definition_version,payload FROM attempts WHERE attempt_id=?",
                             (active.committed["attempt_id"],)).fetchone()
            db.execute("INSERT INTO attempts(rowid,plan_id,attempt_id,step_id,assignee,state,version,definition_version,payload) "
                       "VALUES(-1,?,?,?,?,'active',?,?,?)", (row[0], "synthetic-corrupt-older-live-claim", *row[1:]))
        self.f.store.write(corrupt)
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.accept(p, active, value)
        self.assertEqual(self.authority(), snapshot)

    def test_acceptance_missing_or_corrupt_retained_cohort_fails_atomically(self):
        for corruption in ("missing-member", "missing-namespace", "unknown-namespace", "unclassified-member"):
            with self.subTest(corruption=corruption):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                p, _, active, _, value, _ = self.submitted(writable=True)
                def corrupt(db):
                    row = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (active.committed["attempt_id"],)).fetchone()
                    data = json.loads(row[0])
                    if corruption == "missing-member":
                        db.execute("DELETE FROM resource_uses WHERE attempt_id=?", (active.committed["attempt_id"],))
                    elif corruption == "missing-namespace":
                        del data["execution_uses"]
                    elif corruption == "unknown-namespace":
                        data["execution_uses"]["v"] = 2
                    else:
                        data["execution_uses"]["implementation"]["members"] = []
                    db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), active.committed["attempt_id"]))
                self.f.store.write(corrupt)
                snapshot = self.authority()
                with self.assertRaises(DomainError):
                    self.f.accept(p, active, value)
                self.assertEqual(self.authority(), snapshot)

    def test_acceptance_current_lifecycle_freshness_scope_and_controls(self):
        for corruption in ("stale", "cancelled", "recovery", "bad-time", "naive-time", "expired", "frozen-definition", "grant", "scope", "stop", "block", "store"):
            with self.subTest(corruption=corruption):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                p, _, active, _, value, _ = self.submitted()
                def corrupt(db):
                    raw = db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (active.committed["attempt_id"],)).fetchone()[0]
                    data = json.loads(raw)
                    if corruption == "stale":
                        db.execute("UPDATE attempts SET state='stale' WHERE attempt_id=?", (active.committed["attempt_id"],))
                    elif corruption == "cancelled":
                        data["cancelled_from"] = "submitted"
                    elif corruption == "recovery":
                        data["recovery_required"] = True
                    elif corruption == "bad-time":
                        data["last_activity"] = "bad"
                    elif corruption == "naive-time":
                        data["last_activity"] = "2026-10-06T12:00:00"
                    elif corruption == "expired":
                        self.f.clock._current = datetime.fromisoformat(data["last_activity"]) + timedelta(seconds=self.f.service._stale_seconds)
                    elif corruption == "frozen-definition":
                        data["definition"]["title"] = "Contradictory frozen title"
                    elif corruption == "grant":
                        db.execute("UPDATE capabilities SET revoked=1 WHERE role='attempt'")
                    elif corruption == "scope":
                        plan = json.loads(db.execute("SELECT payload FROM plans WHERE plan_id=?", (p.current_metadata.plan_id,)).fetchone()[0])
                        plan["draft"]["authorization"]["mode"] = "plan-only"
                        db.execute("UPDATE plans SET payload=? WHERE plan_id=?", (json.dumps(plan), p.current_metadata.plan_id))
                    elif corruption == "store":
                        db.execute("UPDATE installation SET store_hold=1 WHERE id=1")
                    db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), active.committed["attempt_id"]))
                if corruption in ("stop", "block"):
                    self.revise(p, **{corruption + "_reason": "explicit control"})
                else:
                    self.f.store.write(corrupt)
                snapshot = self.authority()
                with self.assertRaises(DomainError):
                    self.f.accept(p, active, value)
                self.assertEqual(self.authority(), snapshot)

    def test_acceptance_missing_latest_checkpoint_never_uses_older_proof(self):
        for corrupt in ("missing-row", "missing-event", "bad-current-link"):
            with self.subTest(corrupt=corrupt):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                p, _, active, _, v1, _ = self.submitted(required=True)
                self.review(p, v1)
                self.f.service.execute(self.f.command("reopen_attempt", {"reason": "authorized new cycle"}, receipt=p, attempt=active))
                value = checkpoint(artifact_version="v2")
                current = self.f.submit(p, active, value)
                self.review(p, value)
                sequence = int(current.committed["checkpoint_id"])
                def remove(db):
                    if corrupt == "missing-row":
                        db.execute("DELETE FROM checkpoints WHERE sequence=?", (sequence,))
                    elif corrupt == "missing-event":
                        db.execute("DELETE FROM events WHERE sequence=?", (sequence,))
                    else:
                        data = json.loads(db.execute("SELECT payload FROM events WHERE sequence=?", (sequence,)).fetchone()[0])
                        data["definition"]["title"] = "Contradictory current audit link"
                        db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(data), sequence))
                self.f.store.write(remove)
                snapshot = self.authority()
                with self.assertRaises(DomainError):
                    self.f.accept(p, active, value)
                self.assertEqual(self.authority(), snapshot)

    def test_artifactless_checkpoint_and_latest_failed_review_cannot_supply_acceptance(self):
        p = self.f.create(steps=(step("a", review_required=True),))
        assigned = self.f.assign(p)
        active = self.f.start(p, assigned)
        value = replace(checkpoint(), artifact=None)
        self.f.submit(p, active, value)
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.service.execute(self.accept_command(p, active, checkpoint()))
        self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(self.f.command("reopen_attempt", {"reason": "publish final artifact"}, receipt=p, attempt=active))
        value = checkpoint(artifact_version="explicit-final")
        self.f.submit(p, active, value)
        passed = self.review(p, value)
        self.review(p, value, verdict="failed")
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.accept(p, active, value)
        self.assertEqual(self.authority(), snapshot)
        fresh = self.review(p, value)
        self.f.accept(p, active, value)
        payload = json.loads(self.row(p, active)[3])
        self.assertEqual(payload["review_id"], fresh.committed["review_id"])
        self.assertNotEqual(payload["review_id"], passed.committed["review_id"])

    def test_acceptance_replay_remains_owner_authenticated_and_worker_replay_only(self):
        p = self.f.create(steps=(step("a"),))
        assigned = self.f.assign(p)
        active = self.f.start(p, assigned)
        value = checkpoint()
        worker = replace(self.f.command("record_checkpoint", {"checkpoint": value}, receipt=p, attempt=active, key="original-worker-checkpoint"),
                         expected_revision=None, ownership_version=None)
        self.f.service.execute(worker)
        command = self.accept_command(p, active, value, key="owner-accept")
        snapshot = self.authority()
        with self.assertRaises(DomainError):
            self.f.service.execute(replace(command, attempt_token=assigned.private.attempt_token))
        self.assertEqual(self.authority(), snapshot)
        self.f.service.execute(command)
        self.revise(p, stop_reason="explicit later stop")
        snapshot = self.authority()
        self.assertTrue(self.f.service.execute(command).replayed)
        self.assertTrue(self.f.service.execute(worker).replayed)
        self.assertEqual(self.authority(), snapshot)
        takeover = self.f.service.execute(self.f.command("takeover", {"expected_ownership_version": 1, "reason": "new owner"}, receipt=p))
        with self.assertRaises(DomainError):
            self.f.service.execute(command)
        self.assertTrue(self.f.service.execute(worker).replayed)
        current = self.f.command("accept_step", dict(command.payload), receipt=takeover, attempt=active)
        with self.assertRaises(DomainError):
            self.f.service.execute(current)

    def test_independent_live_acceptance_survives_stale_sibling_without_weakening_start(self):
        p = self.f.create(steps=(step("a"), step("b"), step("c")))
        a = self.f.assign(p)
        b = self.f.start(p, self.f.assign(p, "b"))
        c = self.f.assign(p, "c")
        value = checkpoint()
        self.f.submit(p, b, value)
        # Explicit synthetic stale-state setup; it does not assert actual host execution or recovery.
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='stale' WHERE attempt_id=?", (a.committed["attempt_id"],)))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.status, "running")
        self.assertEqual(view.coordination.total_counts["logical_executable_pending_steps"], 0)
        self.assertTrue(all(any("stale attempt" in reason for reason in ready.reasons) for ready in self.f.service.list_ready(view.plan_id)))
        with self.assertRaises(DomainError) as error:
            self.f.start(p, c)
        self.assertEqual(error.exception.code, "held")
        self.f.accept(p, b, value)
        self.assertEqual(set(self.f.service.read_plan(view.plan_id).coordination.result_versions), {"b"})

    def test_status_precedence_and_independent_running_work(self):
        p = self.f.create(steps=(step("a"), step("b")))
        pid = p.current_metadata.plan_id
        self.assertEqual(self.f.service.read_plan(pid).status, "planning")
        self.assertEqual(self.f.service.read_plan(pid).coordination.total_counts["logical_executable_pending_steps"], 2)
        a = self.f.start(p, self.f.assign(p))
        self.f.submit(p, a, checkpoint("failed", affected=("b",)))
        view = self.f.service.read_plan(pid)
        self.assertEqual(view.status, "running")
        self.revise(p, stop_reason="user stopped")
        self.assertEqual(self.f.service.read_plan(pid).status, "stopped")
        self.revise(p, stop_reason=None, block_reason="explicit block")
        self.assertEqual(self.f.service.read_plan(pid).status, "blocked")
        first = self.f.create(mode="first-step", steps=(step("a"), step("b")), creation_id="first-step-status")
        self.live_accept(first, "a")
        view = self.f.service.read_plan(first.current_metadata.plan_id)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.coordination.total_counts["logical_executable_pending_steps"], 0)
        self.assertEqual(view.draft.authorization.claimed, "a")
        with self.assertRaises(DomainError):
            self.f.assign(first, "b")

    def test_status_rejects_executable_pending_fact_with_started_stale_only_claim(self):
        p = self.f.create(steps=(step("a"), step("b")))
        a = self.f.start(p, self.f.assign(p))
        # TEST-ONLY synthetic stale setup; this does not assert a real recovery sweep or host execution.
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='stale' WHERE attempt_id=?", (a.committed["attempt_id"],)))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(plans.derive_plan_status(view), "blocked")
        self.assertTrue(view.coordination.has_live_attempts)
        counts = view.coordination.total_counts
        self.assertEqual(counts["recorded_started_attempts"], 1)
        self.assertEqual(counts["recorded_nonstale_live_attempts"], 0)
        self.assertEqual(counts["logical_executable_pending_steps"], 0)
        self.assertEqual(next(s["status"] for s in view.legacy_snapshot["steps"] if s["id"] == "b"), "pending")
        contradictory = replace(view, coordination=replace(view.coordination, total_counts={
            **counts, "logical_executable_pending_steps": 1,
        }))
        with self.assertRaises(DomainError) as error:
            plans.derive_plan_status(contradictory)
        self.assertEqual(error.exception.code, "invalid")

    def test_stale_only_and_cancelled_unstarted_work_report_retained_obligations(self):
        p = self.f.create(steps=(step("a"), step("b")))
        a = self.f.assign(p)
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='stale' WHERE attempt_id=?", (a.committed["attempt_id"],)))
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.coordination.total_counts["recorded_started_attempts"], 0)
        self.assertEqual(view.coordination.total_counts["recorded_nonstale_live_attempts"], 0)
        for writable in (False, True):
            with self.subTest(writable=writable):
                plan = self.f.create(steps=(step("a"), step("b")), creation_id=f"cancel-unstarted-{writable}")
                rid = self.f.register(workspace("/synthetic/cancel", 914), plan) if writable else None
                self.f.service.execute(self.f.command("assign_step", {
                    "step_id": "a", "assignee": "worker-a", "context": context(rid, writes=writable),
                }, receipt=plan))
                draft = self.f.service.read_plan(plan.current_metadata.plan_id).draft
                self.f.service.execute(self.f.command("revise_plan", {
                    "draft": draft, "reason": "authorized cancellation", "authorization_change": None,
                    "retire_steps": (RetirementRequest("a", "cancel unstarted claim"),),
                }, receipt=plan))
                view = self.f.service.read_plan(plan.current_metadata.plan_id)
                self.assertEqual(view.status, "blocked" if writable else "planning")
                self.assertEqual(view.coordination.total_counts["recorded_started_attempts"], 0)

    def test_conservative_same_plan_hold_scope_is_shared_and_read_only(self):
        p = self.f.create(steps=(step("a"), step("b")))
        pid = p.current_metadata.plan_id
        bad = ("not-json", {}, {"step_id": "b"}, {"kind": " ", "step_id": "b"},
               {"kind": "reassessment", "step_id": []}, {"kind": "reassessment", "step_id": {}},
               {"kind": "reassessment", "step_id": "foreign"}, {"kind": "reassessment", "step_id": None})
        for value in bad:
            raw = value if isinstance(value, str) else json.dumps(value)
            self.f.store.write(lambda db: db.execute("INSERT OR REPLACE INTO holds(hold_id,plan_id,reason,active,created_at) VALUES('synthetic-hold',?,?,1,'synthetic')", (pid, raw)))
            snapshot = self.authority()
            view = self.f.service.read_plan(pid)
            self.assertEqual(view.status, "blocked")
            self.assertEqual(view.coordination.total_counts["global_holds"], 1)
            self.assertIsNone(view.coordination.holds[0].step_id)
            validation = self.f.store.read(lambda db: plans._validation_coordination(db, pid))
            self.assertIsNone(validation.holds[0].step_id)
            self.assertTrue(all(not r.ready for r in self.f.service.list_ready(pid)))
            self.assertEqual(self.authority(), snapshot)
        raw = json.dumps({"kind": "future-meaningful-kind", "step_id": "b", "reason": "scoped retained reason"})
        self.f.store.write(lambda db: db.execute("UPDATE holds SET reason=? WHERE hold_id='synthetic-hold'", (raw,)))
        view = self.f.service.read_plan(pid)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.coordination.total_counts["global_holds"], 0)
        self.assertEqual(view.coordination.holds[0].step_id, "b")
        self.assertTrue(next(r.ready for r in self.f.service.list_ready(pid) if r.step_id == "a"))

    def test_status_collection_does_not_call_projection_readiness_or_validation(self):
        p = self.f.create(steps=(step("a"), step("b")))
        snapshot = self.authority()
        with mock.patch.object(plans, "project_plan", side_effect=AssertionError("projection recursion")), \
             mock.patch.object(plans, "list_ready", side_effect=AssertionError("readiness recursion")), \
             mock.patch.object(plans, "_validation_coordination", side_effect=AssertionError("validation recursion")):
            agg = self.f.store.read(lambda db: plans._aggregates(db, p.current_metadata.plan_id))
        self.assertEqual(agg.counts["logical_executable_pending_steps"], 2)
        self.assertEqual(self.authority(), snapshot)

    def test_orphan_obligations_do_not_replace_owned_completion_facts(self):
        p = self.f.create(steps=(step("a"),))
        rid = self.f.register(workspace("/synthetic/orphan", 915), p)
        seed_orphan_use(self.f, rid)
        self.live_accept(p, "a")
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.coordination.total_counts["orphan_uses"], 1)
        self.assertEqual(view.coordination.total_counts["owned_nonreleased_uses"], 0)
        self.assertEqual(view.status, "complete")

    def test_terminal_history_seed_refuses_nonowned_store_and_invalid_count(self):
        from tests.service_fixtures import seed_terminal_history
        p = self.f.create(steps=(step("a"),))
        for count in (-1, True, "2"):
            with self.assertRaises(DomainError):
                seed_terminal_history(self.f, p.current_metadata.plan_id, "a", count)
        with mock.patch.object(self.f, "_temporary_identity", (-1, -1)):
            with self.assertRaises(DomainError):
                seed_terminal_history(self.f, p.current_metadata.plan_id, "a", 1)
        other = make_service()
        self.addCleanup(other.cleanup)
        with mock.patch.object(self.f.service, "_store", other.store):
            with self.assertRaises(DomainError):
                seed_terminal_history(self.f, p.current_metadata.plan_id, "a", 1)
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).coordination.total_counts["attempts"], 0)

    def test_transport_exposes_typed_owner_acceptance(self):
        from adaptive_planning.transport import build_mcp
        p, _, active, _, value, _ = self.submitted()
        async def check():
            server = build_mcp(self.f.service)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertIn("accept_step", tools)
            self.assertIn("stopped_resource_ids", json.dumps(tools["accept_step"].input_schema))
            command = self.accept_command(p, active, value)
            result = await server.call_tool("accept_step", {"command": plans.to_jsonable(command)})
            self.assertFalse(result.is_error)
        asyncio.run(check())
        self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status, "complete")

    def test_derived_status_facts_ignore_bounded_arrays_and_overall_outputs(self):
        p, _, _, _, value, _ = self.submitted(writable=True)
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertTrue(all(k in view.coordination.total_counts for k in FACT_KEYS))
        reduced = replace(view, status="complete", legacy_snapshot={**view.legacy_snapshot, "status": "complete"},
                          legacy_publication=None, coordination=replace(view.coordination, attempts=(), holds=(), reservations=(), reviews=()))
        self.assertEqual(plans.derive_plan_status(reduced), "running")
        self.assertEqual(self.f.service.read_coordination(view.plan_id).total_counts, view.coordination.total_counts)
        for key in FACT_KEYS:
            for value in (None, True, -1, "1"):
                facts = dict(view.coordination.total_counts)
                if value is None:
                    del facts[key]
                else:
                    facts[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(DomainError) as error:
                    plans.derive_plan_status(replace(view, coordination=replace(view.coordination, total_counts=facts)))
                self.assertEqual(error.exception.code, "invalid")
        contradictions = (
            {"recorded_nonstale_live_attempts": view.coordination.total_counts["attempts"] + 1},
            {"recorded_started_attempts": view.coordination.total_counts["attempts"] + 1},
            {"global_holds": view.coordination.total_counts["holds"] + 1},
            {"logical_executable_pending_steps": len(view.draft.steps) + 1},
            {"explicit_block_controls": 1},
        )
        for changes in contradictions:
            facts = {**view.coordination.total_counts, **changes}
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                plans.derive_plan_status(replace(view, coordination=replace(view.coordination, total_counts=facts)))
        with self.assertRaises(DomainError):
            plans.derive_plan_status(replace(view, coordination=replace(view.coordination, has_live_attempts=False)))

    def test_status_explicit_block_same_step_core_backing(self):
        p, _, active, _, _, _ = self.submitted()
        self.revise(p, block_reason="waiting for authorized input")
        view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.coordination.total_counts["explicit_block_controls"], 1)
        core.validate_service_graph(plans._proposal(view.legacy_snapshot), None, view.coordination)
        for state in ("active", "submitted", "stale"):
            backing = replace(view.coordination.attempts[0], state=state)
            core.validate_service_graph(plans._proposal(view.legacy_snapshot), None, replace(view.coordination, attempts=(backing,)))
        planning_only = {**plans._proposal(view.legacy_snapshot), "executionMode": "plan-only"}
        for state in ("active", "submitted"):
            backing = replace(view.coordination.attempts[0], state=state)
            with self.subTest(mode="plan-only", state=state), self.assertRaises(core.PlanValidationError):
                core.validate_service_graph(planning_only, None, replace(view.coordination, attempts=(backing,)))
        for control in (None, 0, True, 2, "1"):
            facts = dict(view.coordination.total_counts)
            if control is None:
                del facts["explicit_block_controls"]
            else:
                facts["explicit_block_controls"] = control
            with self.subTest(control=control), self.assertRaises(core.PlanValidationError):
                core.validate_service_graph(plans._proposal(view.legacy_snapshot), None,
                                            replace(view.coordination, total_counts=facts))
        for state, target in (("assigned", "a"), ("active", "b")):
            backing = replace(view.coordination.attempts[0], state=state, step_id=target)
            with self.subTest(state=state, target=target), self.assertRaises(core.PlanValidationError):
                core.validate_service_graph(plans._proposal(view.legacy_snapshot), None,
                                            replace(view.coordination, attempts=(backing,)))


if __name__ == "__main__":
    unittest.main()
