"""Instruction contracts, outage behavior, and missing-MCP worker relay."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest
import uuid

from adaptive_planning.types import (
    ArtifactRef,
    Checkpoint,
    DomainError,
    Evidence,
    Reassessment,
    Review,
)
from tests.service_fixtures import (
    make_service,
    step,
)


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = make_service()
        self.addCleanup(self.f.cleanup)

    def test_instruction_contract_and_unavailable_behavior(self):
        for path in ("SKILL.md", "references/live-dashboard.md"):
            text = Path(path).read_text()
            self.assertIn("checkpoint", text)
            self.assertIn("reassessment", text)
            self.assertIn("unpublished", text)
            self.assertIn("explicit request", text)

            # Normative policy relationships:
            # 1. Default dashboard link
            self.assertIn(
                "The live dashboard link (http://127.0.0.1:8790/) is the default for plan visualization and status reporting.",
                text,
            )
            # 2. ASCII requires explicit request, never default
            self.assertIn(
                "ASCII plan visualization requires an explicit request and is never emitted by default.",
                text,
            )
            # 3. Service mode prohibits per-task fallback server launch
            self.assertIn(
                "Shared service mode prohibits launching a separate per-task or per-session fallback server.",
                text,
            )
            # 4. Named step plus verification requires evidence checkpoint and reassessment
            self.assertIn(
                "Completing a named step and its verification requires publishing a structured checkpoint with evidence and reassessment.",
                text,
            )
            # 5. Outage retains unpublished evidence while new assignments and dependents wait
            self.assertIn(
                "During a service outage, an assigned bounded worker finishes verification and retains unpublished evidence locally while new assignments and dependent steps wait.",
                text,
            )
            # 6. Reconnection preserves the complete command and reconciles first.
            self.assertIn(
                "When service resumes, reread service identity, canonical events, ownership, and coordination before publishing retained evidence.",
                text,
            )
            self.assertIn(
                "Retain the original immutable command body and request key; retry the exact saved command without rebuilding its metadata or payload.",
                text,
            )
            # 7. Recovery uses the authorized owner; metadata is not a filesystem sandbox.
            self.assertIn(
                "Only an authorized owner invokes `sweep_stale`; a direct worker requests that owner sweep before recovery.",
                text,
            )
            self.assertIn(
                "The service enforces declared coordination and capability fences; it does not enforce filesystem permissions or stop host processes.",
                text,
            )

        # Synthetic outage walkthrough:
        # Assigned bounded worker finishes verification and retains unpublished evidence,
        # new assignments/dependents wait, same-key retry/reread/reconcile, no new server/canonical plan.
        p = self.f.create(steps=(step("a"), step("b", dependencies=("a",))))
        assigned = self.f.assign(p, "a")
        active = self.f.start(p, assigned)

        # Bounded worker finishes verification locally and retains unpublished evidence during outage
        art = ArtifactRef(
            kind="code",
            identity="synthetic-artifact-a",
            version="v1",
            digest="sha256-step-a-digest",
            summary="Step A completed implementation",
        )
        ev = Evidence(
            summary="Verification check passed locally",
            artifact=art,
            workspace_id=None,
            check="Check for step a",
            result="passed",
            limitations=(),
            blockers=(),
        )
        reassess = Reassessment(
            summary="Unchanged reassessment",
            facts=(),
            assumptions=(),
            disposition="unchanged",
            reason="Step completed as planned",
            affected_steps=(),
            unbounded=False,
            proposed_draft=None,
        )
        unpublished_checkpoint = Checkpoint(
            outcome="passed",
            check="Check for step a",
            evidence=(ev,),
            reassessment=reassess,
            origin_worker="worker-a",
            relay=None,
            artifact=art,
        )

        # Dependents wait: step b is not ready while step a has unpublished checkpoint
        ready_steps = self.f.service.list_ready(p.current_metadata.plan_id)
        self.assertFalse(any(r.step_id == "b" and r.ready for r in ready_steps))

        # Recovery: same-key retry/reread/reconcile
        key = "idempotent-key-step-a-" + uuid.uuid4().hex
        cmd = self.f.command(
            "record_checkpoint",
            {"checkpoint": unpublished_checkpoint},
            receipt=p,
            attempt=active,
            key=key,
        )
        cmd = replace(cmd, expected_revision=None, ownership_version=None)
        receipt1 = self.f.service.execute(cmd)
        self.assertFalse(receipt1.replayed)

        # Same-key retry succeeds idempotently
        receipt2 = self.f.service.execute(cmd)
        self.assertTrue(receipt2.replayed)

        plan_view = self.f.service.read_plan(p.current_metadata.plan_id)
        self.assertEqual(plan_view.plan_id, p.current_metadata.plan_id)
        self.assertFalse(plan_view.coordination.has_plan_holds)
        coord = self.f.service.read_coordination(p.current_metadata.plan_id)
        self.assertEqual([i for i in coord.items if i.kind == "hold"], [])

        # Accept step a, which unblocks dependent step b
        self.f.accept(p, active, unpublished_checkpoint)
        ready_after = self.f.service.list_ready(p.current_metadata.plan_id)
        self.assertTrue(any(r.step_id == "b" and r.ready for r in ready_after))

    def test_missing_mcp_worker_relay_contract(self):
        # Relay preserves worker + relay identity and never invents checks/heartbeat
        p = self.f.create(steps=(step("a", review_required=True),))
        assigned = self.f.assign(p, "a")

        # Refuse invented worker identity
        wrong_cmd = self.f.command(
            "start_step",
            {
                "origin_worker": "wrong-worker",
                "relay": "synthetic-orchestrator",
                "observed_activity": "wrong worker started",
            },
            receipt=p,
            attempt=assigned,
        )
        wrong_cmd = replace(wrong_cmd, actor="synthetic-orchestrator")
        with self.assertRaises(DomainError) as cm:
            self.f.service.execute(wrong_cmd)
        self.assertEqual(cm.exception.code, "forbidden")

        # 1. Start relayed
        start_cmd = self.f.command(
            "start_step",
            {
                "origin_worker": "worker-a",
                "relay": "synthetic-orchestrator",
                "observed_activity": "worker-a started via relay",
            },
            receipt=p,
            attempt=assigned,
        )
        start_cmd = replace(start_cmd, actor="synthetic-orchestrator")
        start_res = self.f.service.execute(start_cmd)
        self.assertEqual(start_res.committed["attempt_id"], assigned.committed["attempt_id"])

        events = self.f.service.read_events(p.current_metadata.plan_id)
        start_event = next(e for e in events if e.kind == "start_step")
        self.assertEqual(start_event.origin_worker, "worker-a")
        self.assertEqual(start_event.relay, "synthetic-orchestrator")

        # 2. Activity relayed
        act_cmd = self.f.command(
            "report_activity",
            {
                "origin_worker": "worker-a",
                "relay": "synthetic-orchestrator",
                "observed_activity": "worker-a verified tests locally",
            },
            receipt=p,
            attempt=assigned,
        )
        act_cmd = replace(act_cmd, actor="synthetic-orchestrator")
        act_res = self.f.service.execute(act_cmd)
        self.assertEqual(act_res.committed["attempt_id"], assigned.committed["attempt_id"])

        events = self.f.service.read_events(p.current_metadata.plan_id)
        act_event = next(e for e in events if e.kind == "report_activity")
        self.assertEqual(act_event.origin_worker, "worker-a")
        self.assertEqual(act_event.relay, "synthetic-orchestrator")

        # 3. Checkpoint relayed
        art = ArtifactRef(
            kind="code",
            identity="synthetic-artifact-relay",
            version="v1",
            digest="sha256-relay-digest",
            summary="Relayed artifact",
        )
        ev = Evidence(
            summary="Relayed checkpoint evidence",
            artifact=art,
            workspace_id=None,
            check="Check for step a",
            result="passed",
            limitations=(),
            blockers=(),
        )
        reassess = Reassessment(
            summary="Relayed reassessment",
            facts=(),
            assumptions=(),
            disposition="unchanged",
            reason="Unchanged",
            affected_steps=(),
            unbounded=False,
            proposed_draft=None,
        )
        cp = Checkpoint(
            outcome="passed",
            check="Check for step a",
            evidence=(ev,),
            reassessment=reassess,
            origin_worker="worker-a",
            relay="synthetic-orchestrator",
            artifact=art,
        )
        cp_cmd = self.f.command(
            "record_checkpoint",
            {"checkpoint": cp},
            receipt=p,
            attempt=assigned,
        )
        cp_cmd = replace(cp_cmd, actor="synthetic-orchestrator")
        cp_res = self.f.service.execute(cp_cmd)
        self.assertIn("checkpoint_id", cp_res.committed)

        events = self.f.service.read_events(p.current_metadata.plan_id)
        cp_event = next(e for e in events if e.kind == "record_checkpoint")
        self.assertEqual(cp_event.origin_worker, "worker-a")
        self.assertEqual(cp_event.relay, "synthetic-orchestrator")

        # 4. Review relay: Reviewer is reviewer-b, submitting actor is orchestrator/relay.
        # Review object has no invented relay field.
        rev_ev = Evidence(
            summary="Independent review passing evidence",
            artifact=art,
            workspace_id=None,
            check="Check for step a",
            result="passed",
            limitations=(),
            blockers=(),
        )
        review = Review(
            reviewer="reviewer-b",
            artifact=art,
            verdict="passed",
            findings=(),
            evidence=(rev_ev,),
            verification_use_id=None,
        )
        self.assertFalse(hasattr(review, "relay"))
        rev_cmd = self.f.command(
            "record_review",
            {"step_id": "a", "review": review},
            receipt=p,
        )
        rev_res = self.f.service.execute(rev_cmd)
        self.assertEqual(rev_res.committed["verdict"], "passed")

        events = self.f.service.read_events(p.current_metadata.plan_id)
        rev_event = next(e for e in events if e.kind == "record_review")
        self.assertEqual(rev_event.payload["reviewer"], "reviewer-b")
        self.assertEqual(rev_event.actor, "synthetic-orchestrator")
