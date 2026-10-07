"""Tests for pure core planning rules, contracts, and fixtures extraction.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
"""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adaptive_planning.core import (
    PlanValidationError,
    prepare_publication,
    renew_authorization,
    validate_execution_scope,
    validate_plan,
    validate_published_snapshot,
    validate_service_graph,
)
from adaptive_planning.types import (
    BOOTSTRAP_KINDS,
    ORCHESTRATOR_KINDS,
    WORKER_KINDS,
    ArtifactRef,
    AttemptView,
    Authorization,
    AuthorizationChange,
    Availability,
    Checkpoint,
    CommandEnvelope,
    CoordinationItem,
    CoordinationPage,
    CoordinationView,
    DocumentRef,
    DomainError,
    Evidence,
    ExecutionContext,
    ExecutionContextClaims,
    HoldView,
    HostWork,
    InstallationView,
    Metadata,
    MutationReceipt,
    PlanDraft,
    PrivateGrant,
    Reassessment,
    ReservationView,
    ResourceClaim,
    ResourceResolution,
    RetirementRequest,
    Review,
    ReviewView,
    StepDefinition,
    parse_command,
)
from tests.service_fixtures import (
    FakeClock,
    ServiceFixture,
    checkpoint,
    context,
    coordination,
    draft,
    legacy_proposal,
    step,
    workspace,
)


ROOT = Path(__file__).resolve().parents[1]


class TestServiceCore(unittest.TestCase):
    """Unit tests for extracted pure planning core and boundary consistency."""

    def test_core_import_without_service_dependencies(self):
        """Pure core and types must import cleanly under Python 3.10 without SDK dependencies."""
        # 1. Fresh isolated subprocess import proof with import blocker installed BEFORE import
        isolated_code = (
            "import sys\n"
            "blocked = {'mcp', 'starlette', 'uvicorn', 'httpx', 'pydantic'}\n"
            "class BlockHook:\n"
            "    def find_spec(self, fullname, path, target=None):\n"
            "        top = fullname.split('.')[0]\n"
            "        if top in blocked:\n"
            "            raise ImportError(f'Blocked import of {fullname}')\n"
            "        return None\n"
            "sys.meta_path.insert(0, BlockHook())\n"
            "for b in list(blocked):\n"
            "    sys.modules[b] = None\n"
            "\n"
            "import adaptive_planning.core as core\n"
            "import adaptive_planning.types as types\n"
            "import tests.service_fixtures as fixtures\n"
            "\n"
            "assert hasattr(core, 'prepare_publication')\n"
            "assert hasattr(core, 'validate_plan')\n"
            "assert hasattr(types, 'CommandEnvelope')\n"
            "assert hasattr(fixtures, 'make_service')\n"
            "print('SUCCESS')\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", isolated_code],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(res.returncode, 0, f"Isolated import failed:\n{res.stderr}")
        self.assertIn("SUCCESS", res.stdout)

        # 2. In-process patch verification
        blocked_modules = {
            "mcp": None,
            "mcp.server": None,
            "mcp.types": None,
            "starlette": None,
            "uvicorn": None,
        }
        with patch.dict(sys.modules, blocked_modules):
            import adaptive_planning.core as core
            import adaptive_planning.types as types
            import tests.service_fixtures as fixtures

            self.assertTrue(hasattr(core, "prepare_publication"))
            self.assertTrue(hasattr(core, "validate_plan"))
            self.assertTrue(hasattr(types, "CommandEnvelope"))
            self.assertTrue(hasattr(fixtures, "make_service"))

    def test_absolute_legacy_cli_outside_checkout(self):
        """Absolute invocation of legacy script from outside checkout succeeds without SDK."""
        script_path = (ROOT / "scripts" / "live_plan.py").resolve()
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
            self.assertIn("live_plan.py", res.stdout)

    def test_scope_publication_is_pure(self):
        """prepare_publication does not mutate inputs and injects pure timestamp/generation."""
        initial_proposal = legacy_proposal()
        initial_proposal["status"] = "running"
        initial_proposal["executionMode"] = "first-step"
        initial_proposal["changeSummary"] = "Initial publish"
        initial_proposal["steps"][0]["status"] = "in-progress"

        copied_proposal = copy.deepcopy(initial_proposal)
        injected_ts = "2026-10-06T12:00:00+00:00"
        injected_gen = "11111111-1111-1111-1111-111111111111"

        # Create publish
        pub1 = prepare_publication(
            initial_proposal,
            None,
            timestamp=injected_ts,
            generation=injected_gen,
            expected_revision=0,
            expected_generation=None,
            authorization_note="Authorized first step",
            max_file_bytes=10 * 1024 * 1024,
            max_history=100,
        )

        # Check proposal was not mutated
        self.assertEqual(initial_proposal, copied_proposal)
        self.assertEqual(pub1["revision"], 1)
        self.assertEqual(pub1["generation"], injected_gen)
        self.assertEqual(pub1["updatedAt"], injected_ts)
        self.assertEqual(pub1["scope"]["claimed"], "step-1")

        # Second publish (update)
        update_proposal = copy.deepcopy(initial_proposal)
        update_proposal["changeSummary"] = "Second publish"
        copied_update = copy.deepcopy(update_proposal)
        copied_prev = copy.deepcopy(pub1)

        pub2 = prepare_publication(
            update_proposal,
            pub1,
            timestamp="2026-10-06T12:05:00+00:00",
            generation="22222222-2222-2222-2222-222222222222",  # Should retain pub1 generation!
            expected_revision=1,
            expected_generation=injected_gen,
            authorization_note=None,
            max_file_bytes=10 * 1024 * 1024,
            max_history=100,
        )

        self.assertEqual(update_proposal, copied_update)
        self.assertEqual(pub1, copied_prev)
        self.assertEqual(pub2["revision"], 2)
        self.assertEqual(pub2["generation"], injected_gen)  # Retained from previous v2
        self.assertEqual(pub2["updatedAt"], "2026-10-06T12:05:00+00:00")
        self.assertEqual(len(pub2["history"]), 2)

    def test_service_status_boundary_preserves_strict_legacy(self):
        """Preserves legacy rejection of in-progress steps with blocked/stopped status, while service accepts it."""
        proposed = legacy_proposal()
        proposed["steps"][0]["status"] = "in-progress"
        attempt = AttemptView(
            "x",
            "step-1",
            "worker-a",
            "stale",
            "active",
            1,
            "v1",
            "2026-10-06T00:00:00Z",
            context(None, writes=False),
            True,
            None,
        )
        for status in ("blocked", "stopped"):
            proposed["status"] = status
            validate_service_graph(proposed, None, coordination((attempt,)))
            with self.assertRaises(PlanValidationError):
                validate_plan(proposed)

    def test_core_snapshot_limits(self):
        """201 nodes, 51 evidence items, and >10MB snapshots reject without truncation."""
        # 1. 201 nodes (MAX_STEPS = 200)
        p_nodes = legacy_proposal()
        p_nodes["steps"] = [
            {
                "id": f"s{i}",
                "title": f"Step {i}",
                "status": "pending",
                "dependsOn": [],
                "check": "check",
                "evidence": [],
            }
            for i in range(201)
        ]
        with self.assertRaises(PlanValidationError):
            validate_plan(p_nodes)
        self.assertEqual(len(p_nodes["steps"]), 201)

        # 2. 51 evidence items (MAX_EVIDENCE_ITEMS = 50)
        p_ev = legacy_proposal()
        p_ev["steps"][0]["evidence"] = [f"evidence-{i}" for i in range(51)]
        with self.assertRaises(PlanValidationError):
            validate_plan(p_ev)
        self.assertEqual(len(p_ev["steps"][0]["evidence"]), 51)

        # 3. > 10 MB limit (many fields <= 10000 chars whose total exceeds max_file_bytes)
        p_oversize = legacy_proposal()
        p_oversize["nextStepId"] = "s0"
        p_oversize["steps"] = [
            {
                "id": f"s{i}",
                "title": f"Step {i}",
                "status": "pending",
                "dependsOn": [],
                "check": "Valid check string",
                "evidence": ["e" * 1500 for _ in range(50)],
            }
            for i in range(200)
        ]
        # Verify individual fields are within limits
        self.assertEqual(len(p_oversize["steps"]), 200)
        self.assertEqual(len(p_oversize["steps"][0]["evidence"]), 50)
        self.assertLessEqual(len(p_oversize["steps"][0]["evidence"][0]), 10000)

        snapshot_oversize = {
            "schemaVersion": 2,
            "generation": "11111111-1111-1111-1111-111111111111",
            "revision": 1,
            "updatedAt": "2026-10-06T12:00:00Z",
            "taskId": "task-test-01",
            "objective": "Verify size limit",
            "status": "planning",
            "executionMode": "complete-task",
            "scope": {"mode": "complete-task", "sinceRevision": 1, "baseline": [], "claimed": None},
            "nextStepId": "s0",
            "changeSummary": "Size limit test",
            "steps": p_oversize["steps"],
            "history": [{"revision": 1, "timestamp": "2026-10-06T12:00:00Z", "summary": "Initial"}],
        }
        with self.assertRaises(PlanValidationError) as ctx:
            validate_published_snapshot(snapshot_oversize, max_file_bytes=10 * 1024 * 1024)
        self.assertIn("exceeds maximum allowed size", str(ctx.exception).lower())

        with self.assertRaises(PlanValidationError) as ctx:
            prepare_publication(
                p_oversize, None, timestamp="2026-10-06T12:00:00Z", generation="11111111-1111-1111-1111-111111111111",
                expected_revision=0, expected_generation=None, authorization_note="initial note",
                max_file_bytes=10 * 1024 * 1024, max_history=10,
            )
        self.assertIn("exceeds maximum allowed size", str(ctx.exception).lower())

    def test_command_parser_kind_payload_matrix(self):
        """Command parser validates exact kinds, rejects unknown fields and kind/payload mismatches."""
        # Verify kind partitioning
        self.assertTrue(BOOTSTRAP_KINDS.isdisjoint(WORKER_KINDS))
        self.assertTrue(BOOTSTRAP_KINDS.isdisjoint(ORCHESTRATOR_KINDS))
        self.assertTrue(WORKER_KINDS.isdisjoint(ORCHESTRATOR_KINDS))
        self.assertEqual(len(BOOTSTRAP_KINDS) + len(WORKER_KINDS) + len(ORCHESTRATOR_KINDS), 23)

        p_draft = draft()
        valid_create = {
            "kind": "create_plan",
            "epoch": "epoch-1",
            "key": "req-1",
            "actor": "synthetic-orchestrator",
            "plan_id": None,
            "generation": None,
            "expected_revision": None,
            "ownership_version": None,
            "orchestrator_token": None,
            "attempt_id": None,
            "attempt_token": None,
            "attempt_version": None,
            "definition_version": None,
            "payload": {
                "creation_id": "create-1",
                "draft": p_draft,
                "new_plan_intent": "intent",
            },
        }
        cmd = parse_command(valid_create)
        self.assertEqual(cmd.kind, "create_plan")
        self.assertEqual(cmd.payload["creation_id"], "create-1")

        # Unknown field in envelope rejects
        bad_envelope = copy.deepcopy(valid_create)
        bad_envelope["unknown_extra_field"] = "bad"
        with self.assertRaises(DomainError) as ctx:
            parse_command(bad_envelope)
        self.assertEqual(ctx.exception.code, "invalid")

        # Unknown field in payload rejects
        bad_payload = copy.deepcopy(valid_create)
        bad_payload["payload"]["unknown_payload_field"] = "bad"
        with self.assertRaises(DomainError) as ctx:
            parse_command(bad_payload)
        self.assertEqual(ctx.exception.code, "invalid")

        # Mismatched kind and payload rejects
        mismatched = copy.deepcopy(valid_create)
        mismatched["kind"] = "takeover"  # takeover requires expected_ownership_version, reason
        with self.assertRaises(DomainError) as ctx:
            parse_command(mismatched)
        self.assertEqual(ctx.exception.code, "invalid")

        # Default for revise_plan.retire_steps -> ()
        revise_raw = {
            "kind": "revise_plan",
            "epoch": "epoch-1",
            "key": "req-2",
            "actor": "synthetic-orchestrator",
            "plan_id": "plan-1",
            "generation": "gen-1",
            "expected_revision": 1,
            "ownership_version": 1,
            "orchestrator_token": "token-orch",
            "attempt_id": None,
            "attempt_token": None,
            "attempt_version": None,
            "definition_version": None,
            "payload": {
                "draft": p_draft,
                "reason": "revise reason",
                "authorization_change": None,
            },
        }
        cmd_revise = parse_command(revise_raw)
        self.assertEqual(cmd_revise.payload["retire_steps"], ())

        # Default for resolve_attempt.attached_context -> None
        resolve_raw = {
            "kind": "resolve_attempt",
            "epoch": "epoch-1",
            "key": "req-3",
            "actor": "synthetic-orchestrator",
            "plan_id": "plan-1",
            "generation": "gen-1",
            "expected_revision": 1,
            "ownership_version": 1,
            "orchestrator_token": "token-orch",
            "attempt_id": "att-1",
            "attempt_token": None,
            "attempt_version": 1,
            "definition_version": "v1",
            "payload": {
                "resolution": "reject",
                "reason": "reject reason",
                "next_step_state": "pending",
                "stopped_resource_ids": (),
                "replacement_assignee": None,
                "replacement_context": None,
            },
        }
        cmd_resolve = parse_command(resolve_raw)
        self.assertIsNone(cmd_resolve.payload["attached_context"])

    def test_command_parser_token_and_relay_matrix(self):
        """Tokens are validated according to the normative capability matrix."""
        p_draft = draft()
        base_cmd = {
            "epoch": "epoch-1",
            "key": "req-t",
            "plan_id": "plan-1",
            "generation": "gen-1",
            "expected_revision": 1,
            "ownership_version": 1,
            "attempt_id": None,
            "attempt_version": None,
            "definition_version": None,
        }

        # 1. Bootstrap kinds forbid both orchestrator and attempt tokens
        for token_key in ("orchestrator_token", "attempt_token"):
            raw = {
                **base_cmd,
                "kind": "create_plan",
                "actor": "synthetic-orchestrator",
                "orchestrator_token": "tok" if token_key == "orchestrator_token" else None,
                "attempt_token": "tok" if token_key == "attempt_token" else None,
                "payload": {"creation_id": "c1", "draft": p_draft, "new_plan_intent": None},
            }
            with self.assertRaises(DomainError) as ctx:
                parse_command(raw)
            self.assertEqual(ctx.exception.code, "invalid")

        # 2. Worker kinds without relay forbid orchestrator token
        worker_raw = {
            **base_cmd,
            "kind": "start_step",
            "actor": "worker-a",
            "orchestrator_token": "forbidden-orch-tok",
            "attempt_token": "att-tok",
            "attempt_id": "att-1",
            "payload": {"origin_worker": "worker-a", "relay": None, "observed_activity": "act"},
        }
        with self.assertRaises(DomainError) as ctx:
            parse_command(worker_raw)
        self.assertEqual(ctx.exception.code, "invalid")

        # Worker kinds with relay permit orchestrator token
        worker_relayed = copy.deepcopy(worker_raw)
        worker_relayed["payload"]["relay"] = "relayed-worker"
        cmd = parse_command(worker_relayed)
        self.assertEqual(cmd.orchestrator_token, "forbidden-orch-tok")

        # 3. Orchestrator kinds forbid attempt token
        orch_raw = {
            **base_cmd,
            "kind": "sweep_stale",
            "actor": "synthetic-orchestrator",
            "orchestrator_token": "orch-tok",
            "attempt_token": "forbidden-att-tok",
            "payload": {},
        }
        with self.assertRaises(DomainError) as ctx:
            parse_command(orch_raw)
        self.assertEqual(ctx.exception.code, "invalid")

    def test_command_deep_copied_and_immutable(self):
        """Commands and nested values are immutable and deep-copied; secrets are redacted in repr."""
        raw_payload = {
            "creation_id": "c-orig",
            "draft": draft(),
            "new_plan_intent": "intent-orig",
        }
        raw = {
            "kind": "create_plan",
            "epoch": "epoch-1",
            "key": "k-1",
            "actor": "orch",
            "plan_id": None,
            "generation": None,
            "expected_revision": None,
            "ownership_version": None,
            "orchestrator_token": None,
            "attempt_id": None,
            "attempt_token": None,
            "attempt_version": None,
            "definition_version": None,
            "payload": raw_payload,
        }
        cmd = parse_command(raw)

        # Mutating input payload after parse does not affect command
        raw_payload["creation_id"] = "MUTATED"
        self.assertEqual(cmd.payload["creation_id"], "c-orig")

        # CommandEnvelope is frozen
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cmd.actor = "MUTATED_ACTOR"  # type: ignore[misc]

        # Secret repr redaction
        grant = PrivateGrant(orchestrator_token="secret-orch", attempt_token="secret-att")
        self.assertNotIn("secret-orch", repr(grant))
        self.assertNotIn("secret-att", repr(grant))

        worker_cmd = parse_command({
            "kind": "start_step",
            "epoch": "epoch-1",
            "key": "k-2",
            "actor": "worker-a",
            "plan_id": "p-1",
            "generation": "g-1",
            "expected_revision": 1,
            "ownership_version": 1,
            "orchestrator_token": "orch-secret-token",
            "attempt_id": "att-1",
            "attempt_token": "att-secret-token",
            "attempt_version": 1,
            "definition_version": "v1",
            "payload": {"origin_worker": "worker-a", "relay": "relayer", "observed_activity": "act"},
        })
        self.assertNotIn("orch-secret-token", repr(worker_cmd))
        self.assertNotIn("att-secret-token", repr(worker_cmd))

    def test_authorization_latch_and_renewal(self):
        """Execution scope enforces plan-only, first-step latching, and renewal rules."""
        # 1. plan-only forbids execution
        plan_only_auth = Authorization(
            mode="plan-only",
            interval="int-1",
            since_revision=1,
            note="Plan only note",
            baseline=(),
            claimed=None,
        )
        self.assertEqual(validate_execution_scope(plan_only_auth, None), plan_only_auth)
        with self.assertRaises(PlanValidationError):
            validate_execution_scope(plan_only_auth, "step-1")

        # 2. first-step latches first claim and rejects competing claims
        first_step_auth = Authorization(
            mode="first-step",
            interval="int-2",
            since_revision=1,
            note="First step note",
            baseline=("base-1",),
            claimed=None,
        )
        # Cannot claim baseline step
        with self.assertRaises(PlanValidationError):
            validate_execution_scope(first_step_auth, "base-1")

        # Claiming step-1 latches
        latched = validate_execution_scope(first_step_auth, "step-1")
        self.assertEqual(latched.claimed, "step-1")

        # Re-claiming step-1 is allowed
        self.assertEqual(validate_execution_scope(latched, "step-1").claimed, "step-1")

        # Competing step-2 rejected
        with self.assertRaises(PlanValidationError):
            validate_execution_scope(latched, "step-2")

        # 3. complete-task allows multiple steps without latch
        complete_auth = Authorization(
            mode="complete-task",
            interval="int-3",
            since_revision=1,
            note="Complete task note",
            baseline=(),
            claimed=None,
        )
        s1 = validate_execution_scope(complete_auth, "step-1")
        self.assertIsNone(s1.claimed)
        s2 = validate_execution_scope(complete_auth, "step-2")
        self.assertIsNone(s2.claimed)

        # 4. renew_authorization
        change = AuthorizationChange(mode="first-step", note="Renewed note")
        renewed = renew_authorization(
            complete_auth,
            change,
            completed=("step-1",),
            claimed_steps=("step-2",),
            revision=5,
            interval="int-renewed",
        )
        self.assertEqual(renewed.mode, "first-step")
        self.assertEqual(renewed.since_revision, 5)
        self.assertEqual(renewed.interval, "int-renewed")
        self.assertEqual(renewed.baseline, ("step-1",))
        self.assertEqual(renewed.claimed, "step-2")

        # Renewing first-step with >1 active step outside baseline fails
        with self.assertRaises(PlanValidationError):
            renew_authorization(
                complete_auth,
                change,
                completed=("step-1",),
                claimed_steps=("step-2", "step-3"),
                revision=6,
                interval="int-bad",
            )

        # Empty note fails
        bad_change = AuthorizationChange(mode="first-step", note="   ")
        with self.assertRaises(PlanValidationError):
            renew_authorization(
                complete_auth,
                bad_change,
                completed=(),
                claimed_steps=(),
                revision=7,
                interval="int-bad",
            )

    def test_prepare_publication_cas_and_generation(self):
        """CAS, generation retention, and publication constraints are strictly enforced."""
        prop = legacy_proposal()
        prop["executionMode"] = "first-step"
        prop["changeSummary"] = "Initial"

        # Creation revision mismatch
        with self.assertRaises(PlanValidationError):
            prepare_publication(
                prop, None, timestamp="2026-10-06T12:00:00Z", generation="gen-1",
                expected_revision=1, expected_generation=None, authorization_note="note",
                max_file_bytes=10000, max_history=10,
            )

        # Creation generation not None
        with self.assertRaises(PlanValidationError):
            prepare_publication(
                prop, None, timestamp="2026-10-06T12:00:00Z", generation="gen-1",
                expected_revision=0, expected_generation="gen-0", authorization_note="note",
                max_file_bytes=10000, max_history=10,
            )

        # Creation missing note
        with self.assertRaises(PlanValidationError):
            prepare_publication(
                prop, None, timestamp="2026-10-06T12:00:00Z", generation="gen-1",
                expected_revision=0, expected_generation=None, authorization_note="",
                max_file_bytes=10000, max_history=10,
            )

        # Valid create
        v2_pub = prepare_publication(
            prop, None, timestamp="2026-10-06T12:00:00Z", generation="11111111-1111-1111-1111-111111111111",
            expected_revision=0, expected_generation=None, authorization_note="initial note",
            max_file_bytes=10000, max_history=10,
        )

        # Update with wrong revision
        with self.assertRaises(PlanValidationError):
            prepare_publication(
                prop, v2_pub, timestamp="2026-10-06T12:01:00Z", generation="22222222-2222-2222-2222-222222222222",
                expected_revision=2, expected_generation="11111111-1111-1111-1111-111111111111",
                authorization_note=None, max_file_bytes=10000, max_history=10,
            )

        # Update with wrong generation
        with self.assertRaises(PlanValidationError):
            prepare_publication(
                prop, v2_pub, timestamp="2026-10-06T12:01:00Z", generation="22222222-2222-2222-2222-222222222222",
                expected_revision=1, expected_generation="wrong-generation",
                authorization_note=None, max_file_bytes=10000, max_history=10,
            )

    def test_prepare_publication_bounded_history_with_custom_max_history(self):
        """prepare_publication correctly bounds history across revisions with custom max_history."""
        prop = legacy_proposal()
        prop["executionMode"] = "complete-task"
        prev = None
        gen = "11111111-1111-1111-1111-111111111111"
        for i in range(1, 16):
            prop["changeSummary"] = f"Revision {i}"
            prev = prepare_publication(
                prop,
                prev,
                timestamp="2026-10-06T12:00:00Z",
                generation=gen,
                expected_revision=i - 1,
                expected_generation=None if i == 1 else gen,
                authorization_note="init" if i == 1 else None,
                max_file_bytes=100000,
                max_history=10,
            )
        self.assertEqual(prev["revision"], 15)
        self.assertEqual(len(prev["history"]), 10)
        self.assertEqual([e["revision"] for e in prev["history"]], list(range(6, 16)))
        # Verify snapshot validation with custom max_history succeeds
        data, wire = validate_published_snapshot(prev, max_history=10)
        self.assertEqual(data["revision"], 15)

    def test_command_parser_type_and_literal_validations(self):
        """parse_command rigorously rejects wrong types, invalid literals, and invariant violations."""
        base_env = {
            "epoch": "epoch-1",
            "key": "req-1",
            "actor": "orch",
        }

        # 1. Non-integer or negative expected_revision
        for bad_rev in ("1", -1, 3.14):
            with self.assertRaises(DomainError) as ctx:
                parse_command({
                    **base_env,
                    "kind": "revise_plan",
                    "expected_revision": bad_rev,
                    "payload": {"draft": draft(), "reason": "reason", "authorization_change": None},
                })
            self.assertEqual(ctx.exception.code, "invalid")

        # 2. Non-integer or negative ownership_version
        for bad_ov in ("1", -1):
            with self.assertRaises(DomainError) as ctx:
                parse_command({
                    **base_env,
                    "kind": "takeover",
                    "ownership_version": bad_ov,
                    "payload": {"expected_ownership_version": 1, "reason": "reason"},
                })
            self.assertEqual(ctx.exception.code, "invalid")

        # 3. resolve_attempt: invalid resolution literal
        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "resolve_attempt",
                "orchestrator_token": "tok",
                "payload": {
                    "resolution": "invalid-resolution",
                    "reason": "reason",
                    "next_step_state": "pending",
                    "stopped_resource_ids": (),
                    "replacement_assignee": None,
                    "replacement_context": None,
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

        # 4. resolve_attempt: invalid next_step_state literal
        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "resolve_attempt",
                "orchestrator_token": "tok",
                "payload": {
                    "resolution": "replace",
                    "reason": "reason",
                    "next_step_state": "in-progress",  # Only pending, blocked, retired allowed
                    "stopped_resource_ids": (),
                    "replacement_assignee": None,
                    "replacement_context": None,
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

        # 5. resolve_attempt: normal variants require attached_context=None
        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "resolve_attempt",
                "orchestrator_token": "tok",
                "payload": {
                    "resolution": "replace",
                    "reason": "reason",
                    "next_step_state": "pending",
                    "stopped_resource_ids": (),
                    "replacement_assignee": None,
                    "replacement_context": None,
                    "attached_context": context(None, writes=False),
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

        # 6. resolve_reassessment: invalid decision or non-bool unbounded
        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "resolve_reassessment",
                "orchestrator_token": "tok",
                "payload": {
                    "checkpoint_id": "cp-1",
                    "decision": "invalid-decision",
                    "reason": "reason",
                    "draft": None,
                    "affected_steps": (),
                    "unbounded": False,
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "resolve_reassessment",
                "orchestrator_token": "tok",
                "payload": {
                    "checkpoint_id": "cp-1",
                    "decision": "apply",
                    "reason": "reason",
                    "draft": None,
                    "affected_steps": (),
                    "unbounded": "not-a-bool",
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

        # 7. release_resource_use: non-bool stopped
        with self.assertRaises(DomainError) as ctx:
            parse_command({
                **base_env,
                "kind": "release_resource_use",
                "orchestrator_token": "tok",
                "payload": {
                    "use_id": "use-1",
                    "resource_ids": (),
                    "reason": "reason",
                    "stopped": "true",  # string instead of bool
                },
            })
        self.assertEqual(ctx.exception.code, "invalid")

    def test_service_fixture_cached_grants_and_frozen_epoch(self):
        """ServiceFixture context kind literals, cached grant fallback, and frozen epoch are verified."""
        # 1. context() kind literals
        ro_ctx = context(None, writes=False)
        self.assertEqual(ro_ctx.kind, "read-only")

        repo_ctx = context(None, repository_id="repo-1")
        self.assertEqual(repo_ctx.kind, "repository")

        non_repo_ctx = context("ws-1")
        self.assertEqual(non_repo_ctx.kind, "non-repository")

        ext_ctx = context(None, external_ids=("ext-1",))
        self.assertEqual(ext_ctx.kind, "external-only")

        # 2. ServiceFixture command token caching and freeze behavior
        from tests.service_fixtures import FakeClock, ServiceFixture
        from adaptive_planning.types import Metadata, MutationReceipt, PrivateGrant

        clock = FakeClock()
        plan_id = "plan-seeded"
        meta_committed = Metadata("epoch-1", plan_id, "gen-1", 1, 1, "url")
        meta_current = Metadata("epoch-active", plan_id, "gen-1", 2, 1, "url")

        class _ProtocolStubService:
            def read_service_info(self):
                from unittest.mock import MagicMock
                return MagicMock(epoch="epoch-active")

            def read_plan(self, pid):
                from unittest.mock import MagicMock
                return MagicMock(metadata=meta_current)

        fixture = ServiceFixture(service=_ProtocolStubService(), store=None, directory=Path("/tmp"), clock=clock, epoch="epoch-active")

        # Seed cached grant
        fixture._cached_grants[plan_id] = PrivateGrant(orchestrator_token="cached-orch-token", attempt_token=None)

        # Receipt with private=None falls back to cached grant
        receipt_no_private = MutationReceipt(
            committed={}, committed_metadata=meta_committed, current_metadata=meta_current, replayed=False, private=None
        )

        p_draft = draft()
        cmd = fixture.command(
            "revise_plan",
            {"draft": p_draft, "reason": "rev", "authorization_change": None},
            receipt=receipt_no_private,
        )
        self.assertEqual(cmd.orchestrator_token, "cached-orch-token")
        self.assertEqual(cmd.epoch, "epoch-active")

        # refresh_metadata=False freezes committed epoch
        cmd_frozen = fixture.command(
            "revise_plan",
            {"draft": p_draft, "reason": "rev", "authorization_change": None},
            receipt=receipt_no_private,
            refresh_metadata=False,
        )
        self.assertEqual(cmd_frozen.epoch, "epoch-1")
        self.assertEqual(cmd_frozen.expected_revision, 1)

    def test_core_reexports_parse_command(self):
        """adaptive_planning.core re-exports parse_command conforming to the interface contract."""
        from adaptive_planning.core import parse_command as core_parse_command
        self.assertTrue(callable(core_parse_command))

    def test_relayed_checkpoint_parser_and_fixture(self):
        """Relayed record_checkpoint permits orchestrator token; fixture selects both tokens for relayed checkpoint."""
        cp = checkpoint(relay="relay-worker")
        cmd_raw = {
            "kind": "record_checkpoint",
            "epoch": "epoch-1",
            "key": "req-cp",
            "actor": "worker-a",
            "plan_id": "p-1",
            "generation": "g-1",
            "expected_revision": 1,
            "ownership_version": 1,
            "orchestrator_token": "orch-tok",
            "attempt_id": "att-1",
            "attempt_token": "att-tok",
            "attempt_version": 1,
            "definition_version": "v1",
            "payload": {"checkpoint": cp},
        }
        cmd = parse_command(cmd_raw)
        self.assertEqual(cmd.orchestrator_token, "orch-tok")
        self.assertEqual(cmd.attempt_token, "att-tok")

        # Negative: worker command with relay=None forbids orchestrator token
        non_relayed_raw = copy.deepcopy(cmd_raw)
        non_relayed_raw["payload"] = {"checkpoint": checkpoint(relay=None)}
        with self.assertRaises(DomainError) as ctx:
            parse_command(non_relayed_raw)
        self.assertEqual(ctx.exception.code, "invalid")

        # Fixture token selection for relayed checkpoint
        clock = FakeClock()
        fixture = ServiceFixture(service=None, store=None, directory=Path("/tmp"), clock=clock, epoch="epoch-1")
        meta = Metadata("epoch-1", "p-1", "g-1", 1, 1, "url")
        receipt = MutationReceipt(
            committed={}, committed_metadata=meta, current_metadata=meta, replayed=False,
            private=PrivateGrant(orchestrator_token="receipt-orch", attempt_token=None),
        )
        attempt = MutationReceipt(
            committed={"attempt_id": "att-1", "version": 1, "definition_version": "v1"},
            committed_metadata=meta, current_metadata=meta, replayed=False,
            private=PrivateGrant(orchestrator_token=None, attempt_token="receipt-att"),
        )
        fix_cmd = fixture.command(
            "record_checkpoint",
            {"checkpoint": cp},
            receipt=receipt,
            attempt=attempt,
            refresh_metadata=False,
        )
        self.assertEqual(fix_cmd.orchestrator_token, "receipt-orch")
        self.assertEqual(fix_cmd.attempt_token, "receipt-att")

    def test_command_and_nested_payload_immutability(self):
        """Top-level command payload, nested mappings, receipts, drafts and views reject mutation."""
        p_draft = draft()
        cmd = parse_command({
            "kind": "create_plan",
            "epoch": "epoch-1",
            "key": "k-imm",
            "actor": "orch",
            "payload": {
                "creation_id": "c-1",
                "draft": p_draft,
                "new_plan_intent": "intent",
            },
        })
        # Top-level payload mutation raises TypeError
        with self.assertRaises(TypeError):
            cmd.payload["creation_id"] = "MUTATED"
        with self.assertRaises(TypeError):
            cmd.payload["new_field"] = "BAD"

        # Focused regression for in-place union (__ior__ / |=)
        takeover_cmd = parse_command({
            "kind": "takeover",
            "epoch": "epoch-1",
            "key": "k-to",
            "actor": "orch",
            "payload": {
                "expected_ownership_version": 1,
                "reason": "takeover",
            },
        })
        p = takeover_cmd.payload
        raw_json_before = json.dumps(takeover_cmd.payload)
        with self.assertRaises(TypeError):
            p |= {"changed": 1}
        self.assertEqual(json.dumps(takeover_cmd.payload), raw_json_before)
        self.assertNotIn("changed", takeover_cmd.payload)
        self.assertNotIn("changed", p)

        # Nested mapping mutation and in-place union raise TypeError
        import_cmd = parse_command({
            "kind": "import_plan",
            "epoch": "epoch-1",
            "key": "k-imp",
            "actor": "orch",
            "payload": {
                "host_id": "host-1",
                "source_directory_id": "dir-1",
                "source_directory_claim": None,
                "original_task_id": "task-1",
                "original_snapshot": {"schemaVersion": 2, "objective": "test"},
                "provenance": DocumentRef("path/doc", None, None, "v1", None),
                "writers_stopped": "yes",
                "known_contexts": {"s1": context("ws-1")},
            },
        })
        with self.assertRaises(TypeError):
            import_cmd.payload["original_snapshot"]["objective"] = "MUTATED"

        snap = import_cmd.payload["original_snapshot"]
        raw_snap_before = json.dumps(snap)
        with self.assertRaises(TypeError):
            snap |= {"changed": 1}
        self.assertEqual(json.dumps(snap), raw_snap_before)
        self.assertNotIn("changed", snap)

        # MutationReceipt.committed is frozen
        meta = Metadata("epoch-1", "p-1", "g-1", 1, 1, "url")
        receipt = MutationReceipt(
            committed={"resource_id": "res-1"},
            committed_metadata=meta,
            current_metadata=meta,
            replayed=False,
            private=None,
        )
        with self.assertRaises(TypeError):
            receipt.committed["resource_id"] = "MUTATED"

        # CoordinationView.result_versions and total_counts are frozen
        coord_view = coordination()
        with self.assertRaises(TypeError):
            coord_view.result_versions["a"] = "v2"
        with self.assertRaises(TypeError):
            coord_view.total_counts["attempts"] = 5

    def test_all_23_command_kinds_positive_and_negative_typed_payloads(self):
        """All 23 command kinds validate typed payload shapes, literals, and reject invalid/non-finite inputs."""
        base_orch = {
            "epoch": "epoch-1",
            "key": "req-pos",
            "actor": "orch",
            "orchestrator_token": "orch-tok",
        }
        base_worker = {
            "epoch": "epoch-1",
            "key": "req-w",
            "actor": "worker-a",
            "attempt_token": "att-tok",
        }
        base_boot = {
            "epoch": "epoch-1",
            "key": "req-b",
            "actor": "orch",
        }

        # Common raw JSON draft dictionary for wire-format testing
        raw_draft = {
            "objective": "test obj",
            "acceptance_criteria": ["ac1"],
            "constraints": ["c1"],
            "facts": ["f1"],
            "assumptions": ["a1"],
            "next_check": "chk",
            "authorization": {
                "mode": "plan-only",
                "interval": "1h",
                "since_revision": 0,
                "note": "note",
                "baseline": [],
                "claimed": None,
            },
            "steps": [
                {
                    "step_id": "s1",
                    "title": "t1",
                    "dependencies": [],
                    "completion_check": "chk",
                    "document": None,
                    "definition_version": "v1",
                    "review_required": False,
                    "final_gate": False,
                }
            ],
            "documents": [{"path": "p", "branch": None, "commit": None, "version": "v1", "task_id": None}],
            "workspace_refs": ["w1"],
            "workflow": None,
            "horizon": 10,
            "stop_reason": None,
            "block_reason": None,
        }

        # 1. create_plan (wire-format raw JSON draft conversion to PlanDraft)
        c_pos = parse_command({**base_boot, "kind": "create_plan", "payload": {"creation_id": "c1", "draft": raw_draft, "new_plan_intent": None}})
        self.assertEqual(c_pos.kind, "create_plan")
        self.assertIsInstance(c_pos.payload["draft"], PlanDraft)
        self.assertIsInstance(c_pos.payload["draft"].authorization, Authorization)
        self.assertIsInstance(c_pos.payload["draft"].steps[0], StepDefinition)
        self.assertIsInstance(c_pos.payload["draft"].documents[0], DocumentRef)
        with self.assertRaises(DomainError):
            parse_command({**base_boot, "kind": "create_plan", "payload": {"creation_id": "c1", "draft": "not-a-draft", "new_plan_intent": None}})
        bad_draft_mode = copy.deepcopy(raw_draft)
        bad_draft_mode["authorization"]["mode"] = "invalid-mode"
        with self.assertRaises(DomainError):
            parse_command({**base_boot, "kind": "create_plan", "payload": {"creation_id": "c1", "draft": bad_draft_mode, "new_plan_intent": None}})

        # 2. revise_plan (wire-format raw JSON nested authorization_change and retire_steps)
        raw_auth_change = {"mode": "plan-only", "note": "revised"}
        raw_retire = {"step_id": "s1", "reason": "retired"}
        r_pos = parse_command({**base_orch, "kind": "revise_plan", "payload": {"draft": raw_draft, "reason": "rev", "authorization_change": raw_auth_change, "retire_steps": [raw_retire]}})
        self.assertEqual(r_pos.kind, "revise_plan")
        self.assertIsInstance(r_pos.payload["draft"], PlanDraft)
        self.assertIsInstance(r_pos.payload["authorization_change"], AuthorizationChange)
        self.assertIsInstance(r_pos.payload["retire_steps"][0], RetirementRequest)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "revise_plan", "payload": {"draft": raw_draft, "reason": 123, "authorization_change": None}})
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "revise_plan", "payload": {"draft": raw_draft, "reason": "rev", "authorization_change": {"mode": "bad", "note": "n"}}})

        # 3. takeover
        t_pos = parse_command({**base_boot, "kind": "takeover", "payload": {"expected_ownership_version": 2, "reason": "takeover"}})
        self.assertEqual(t_pos.kind, "takeover")
        with self.assertRaises(DomainError):
            parse_command({**base_boot, "kind": "takeover", "payload": {"expected_ownership_version": -1, "reason": "takeover"}})

        # 4. register_resource (wire-format raw JSON claim conversion to ResourceClaim)
        raw_claim = {
            "host_id": "h",
            "kind": "workspace",
            "device": None,
            "inode": None,
            "canonical_path": "/p",
            "case_insensitive": False,
            "repository_id": None,
            "external_identity": None,
        }
        rr_pos = parse_command({**base_orch, "kind": "register_resource", "payload": {"claim": raw_claim}})
        self.assertEqual(rr_pos.kind, "register_resource")
        self.assertIsInstance(rr_pos.payload["claim"], ResourceClaim)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "register_resource", "payload": {"claim": {"host_id": "h", "kind": "invalid-kind", "case_insensitive": False}}})

        # 5. reconcile_resources
        rec_pos = parse_command({**base_orch, "kind": "reconcile_resources", "payload": {"resource_ids": ("r1",), "canonical_claim": raw_claim, "reason": "rec"}})
        self.assertEqual(rec_pos.kind, "reconcile_resources")
        self.assertIsInstance(rec_pos.payload["canonical_claim"], ResourceClaim)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "reconcile_resources", "payload": {"resource_ids": "not-a-tuple", "canonical_claim": raw_claim, "reason": "rec"}})

        # 6. invalidate_result (wire-format raw JSON evidence conversion to Evidence)
        raw_ev = {
            "summary": "ev",
            "artifact": None,
            "workspace_id": None,
            "check": "chk",
            "result": "failed",
            "limitations": [],
            "blockers": [],
        }
        ir_pos = parse_command({**base_orch, "kind": "invalidate_result", "payload": {"step_id": "s1", "result_version": "v1", "reason": "inv", "evidence": [raw_ev]}})
        self.assertEqual(ir_pos.kind, "invalidate_result")
        self.assertIsInstance(ir_pos.payload["evidence"][0], Evidence)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "invalidate_result", "payload": {"step_id": "s1", "result_version": "v1", "reason": "inv", "evidence": ({"summary": "s", "check": "c", "result": "bad-result"},)}})

        # 7. resolve_invalidation
        rinv_pos = parse_command({**base_orch, "kind": "resolve_invalidation", "payload": {"invalidation_id": "i1", "affected_step_id": "s1", "disposition": "revalidated", "corrective_step_id": None, "corrected_result_version": None, "evidence": (), "reason": "ok"}})
        self.assertEqual(rinv_pos.kind, "resolve_invalidation")
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "resolve_invalidation", "payload": {"invalidation_id": "i1", "affected_step_id": "s1", "disposition": "invalid-disp", "corrective_step_id": None, "corrected_result_version": None, "evidence": (), "reason": "ok"}})

        # 8. assign_step (wire-format raw JSON context conversion to ExecutionContext)
        raw_ctx = {
            "kind": "repository",
            "repository_id": None,
            "workspace_id": None,
            "repository_exclusive": True,
            "external_ids": [],
            "writes": True,
            "checked_base": None,
        }
        as_pos = parse_command({**base_orch, "kind": "assign_step", "payload": {"step_id": "s1", "assignee": "w1", "context": raw_ctx}})
        self.assertEqual(as_pos.kind, "assign_step")
        self.assertIsInstance(as_pos.payload["context"], ExecutionContext)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "assign_step", "payload": {"step_id": "s1", "assignee": "w1", "context": {"kind": "bad-kind", "repository_exclusive": True, "writes": True}}})

        # 9. bind_availability (wire-format raw JSON availability conversion)
        raw_avail = {
            "prerequisite": "s0",
            "result_version": "v1",
            "method": "ancestor",
            "checked_base": "base",
            "artifact_digest": None,
            "evidence": raw_ev,
        }
        ba_pos = parse_command({**base_orch, "kind": "bind_availability", "payload": {"bindings": [raw_avail], "checked_base": "base"}})
        self.assertEqual(ba_pos.kind, "bind_availability")
        self.assertIsInstance(ba_pos.payload["bindings"][0], Availability)
        self.assertIsInstance(ba_pos.payload["bindings"][0].evidence, Evidence)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "bind_availability", "payload": {"bindings": [{"prerequisite": "s0", "result_version": "v1", "method": "bad-method", "checked_base": "base", "artifact_digest": None, "evidence": raw_ev}], "checked_base": "base"}})

        # 10. start_step
        ss_pos = parse_command({**base_worker, "kind": "start_step", "payload": {"origin_worker": "worker-a", "relay": None, "observed_activity": "act"}})
        self.assertEqual(ss_pos.kind, "start_step")
        with self.assertRaises(DomainError):
            parse_command({**base_worker, "kind": "start_step", "payload": {"origin_worker": 123, "relay": None, "observed_activity": "act"}})

        # 11. report_activity
        ra_pos = parse_command({**base_worker, "kind": "report_activity", "payload": {"origin_worker": "worker-a", "relay": None, "observed_activity": "act"}})
        self.assertEqual(ra_pos.kind, "report_activity")
        with self.assertRaises(DomainError):
            parse_command({**base_worker, "kind": "report_activity", "payload": {"origin_worker": "worker-a", "relay": None, "observed_activity": None}})

        # 12. record_checkpoint (wire-format raw JSON checkpoint conversion to Checkpoint)
        raw_checkpoint = {
            "outcome": "passed",
            "check": "chk",
            "evidence": [raw_ev],
            "reassessment": {
                "summary": "s",
                "facts": [],
                "assumptions": [],
                "disposition": "unchanged",
                "reason": "r",
                "affected_steps": [],
                "unbounded": False,
                "proposed_draft": None,
            },
            "origin_worker": "worker-a",
            "relay": None,
            "artifact": None,
        }
        rc_pos = parse_command({**base_worker, "kind": "record_checkpoint", "payload": {"checkpoint": raw_checkpoint}})
        self.assertEqual(rc_pos.kind, "record_checkpoint")
        self.assertIsInstance(rc_pos.payload["checkpoint"], Checkpoint)
        self.assertIsInstance(rc_pos.payload["checkpoint"].reassessment, Reassessment)
        self.assertIsInstance(rc_pos.payload["checkpoint"].evidence[0], Evidence)
        with self.assertRaises(DomainError):
            parse_command({**base_worker, "kind": "record_checkpoint", "payload": {"checkpoint": "not-a-checkpoint"}})
        bad_cp_outcome = copy.deepcopy(raw_checkpoint)
        bad_cp_outcome["outcome"] = "bad-outcome"
        with self.assertRaises(DomainError):
            parse_command({**base_worker, "kind": "record_checkpoint", "payload": {"checkpoint": bad_cp_outcome}})

        # 13. resolve_reassessment
        rr_pos2 = parse_command({**base_orch, "kind": "resolve_reassessment", "payload": {"checkpoint_id": "cp1", "decision": "apply", "reason": "r", "draft": None, "affected_steps": (), "unbounded": False}})
        self.assertEqual(rr_pos2.kind, "resolve_reassessment")
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "resolve_reassessment", "payload": {"checkpoint_id": "cp1", "decision": "bad-decision", "reason": "r", "draft": None, "affected_steps": (), "unbounded": False}})

        # 14. reopen_attempt
        ro_pos = parse_command({**base_orch, "kind": "reopen_attempt", "payload": {"reason": "reopen"}})
        self.assertEqual(ro_pos.kind, "reopen_attempt")
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "reopen_attempt", "payload": {"reason": 999}})

        # 15. sweep_stale
        sw_pos = parse_command({**base_orch, "kind": "sweep_stale", "payload": {}})
        self.assertEqual(sw_pos.kind, "sweep_stale")
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "sweep_stale", "payload": {"extra": "forbidden"}})

        # 16. record_review (wire-format raw JSON review conversion to Review)
        raw_review = {
            "reviewer": "reviewer-1",
            "artifact": {
                "kind": "code",
                "identity": "art-1",
                "version": "v1",
                "digest": None,
                "summary": "summary",
            },
            "verdict": "passed",
            "findings": ["finding 1"],
            "evidence": [raw_ev],
            "verification_use_id": None,
        }
        rv_pos = parse_command({**base_orch, "kind": "record_review", "payload": {"step_id": "s1", "review": raw_review}})
        self.assertEqual(rv_pos.kind, "record_review")
        self.assertIsInstance(rv_pos.payload["review"], Review)
        self.assertIsInstance(rv_pos.payload["review"].artifact, ArtifactRef)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "record_review", "payload": {"step_id": "s1", "review": "not-a-review"}})
        bad_rev_verdict = copy.deepcopy(raw_review)
        bad_rev_verdict["verdict"] = "bad-verdict"
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "record_review", "payload": {"step_id": "s1", "review": bad_rev_verdict}})

        # 17. accept_step
        raw_art = {
            "kind": "code",
            "identity": "art-1",
            "version": "v1",
            "digest": None,
            "summary": "summary",
        }
        ac_pos = parse_command({**base_orch, "kind": "accept_step", "payload": {"step_id": "s1", "artifact": raw_art, "stopped_resource_ids": (), "stopped_reason": None}})
        self.assertEqual(ac_pos.kind, "accept_step")
        self.assertIsInstance(ac_pos.payload["artifact"], ArtifactRef)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "accept_step", "payload": {"step_id": "s1", "artifact": "not-an-artifact", "stopped_resource_ids": (), "stopped_reason": None}})

        # 18. resolve_attempt
        ra_pos2 = parse_command({**base_orch, "kind": "resolve_attempt", "payload": {"resolution": "confirm-owner", "reason": "ok", "next_step_state": "pending", "stopped_resource_ids": (), "replacement_assignee": None, "replacement_context": raw_ctx, "attached_context": None}})
        self.assertEqual(ra_pos2.kind, "resolve_attempt")
        self.assertIsInstance(ra_pos2.payload["replacement_context"], ExecutionContext)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "resolve_attempt", "payload": {"resolution": "unknown-res", "reason": "ok", "next_step_state": "pending", "stopped_resource_ids": (), "replacement_assignee": None, "replacement_context": None, "attached_context": None}})

        # 19. reserve_verification
        rv_pos2 = parse_command({**base_orch, "kind": "reserve_verification", "payload": {"step_id": "s1", "verifier": "v1", "context": raw_ctx, "handoff_from": None, "stopped_reason": None}})
        self.assertEqual(rv_pos2.kind, "reserve_verification")
        self.assertIsInstance(rv_pos2.payload["context"], ExecutionContext)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "reserve_verification", "payload": {"step_id": "s1", "verifier": 123, "context": raw_ctx, "handoff_from": None, "stopped_reason": None}})

        # 20. release_resource_use
        ru_pos = parse_command({**base_orch, "kind": "release_resource_use", "payload": {"use_id": "u1", "resource_ids": (), "reason": "rel", "stopped": True}})
        self.assertEqual(ru_pos.kind, "release_resource_use")
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "release_resource_use", "payload": {"use_id": "u1", "resource_ids": (), "reason": "rel", "stopped": "not-bool"}})

        # 21. append_late_evidence
        ale_pos = parse_command({**base_orch, "kind": "append_late_evidence", "payload": {"origin_attempt_id": "att-1", "evidence": [raw_ev], "stopped_resource_ids": (), "reason": "ok"}})
        self.assertEqual(ale_pos.kind, "append_late_evidence")
        self.assertIsInstance(ale_pos.payload["evidence"][0], Evidence)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "append_late_evidence", "payload": {"origin_attempt_id": 123, "evidence": (), "stopped_resource_ids": (), "reason": "ok"}})

        # 22. import_plan (wire-format raw JSON provenance and known_contexts, omitting source_directory_claim)
        raw_prov = {
            "path": "path",
            "branch": None,
            "commit": None,
            "version": "v1",
            "task_id": None,
        }
        ip_pos = parse_command({
            **base_boot,
            "kind": "import_plan",
            "payload": {
                "host_id": "h",
                "source_directory_id": "d1",
                "source_directory_claim": None,
                "original_task_id": "t1",
                "original_snapshot": {"schemaVersion": 2},
                "provenance": raw_prov,
                "writers_stopped": "yes",
                "known_contexts": {"s1": raw_ctx},
            },
        })
        self.assertEqual(ip_pos.kind, "import_plan")
        self.assertIsInstance(ip_pos.payload["provenance"], DocumentRef)
        self.assertIsInstance(ip_pos.payload["known_contexts"]["s1"], ExecutionContext)
        with self.assertRaises(DomainError):
            parse_command({**base_boot, "kind": "import_plan", "payload": {"host_id": "h", "source_directory_id": None, "source_directory_claim": None, "original_task_id": "t1", "original_snapshot": {"schemaVersion": 2}, "provenance": raw_prov, "writers_stopped": "yes", "known_contexts": {}}})

        # 23. resolve_store_hold (wire-format raw JSON absent_work_inventory and resource_resolutions)
        raw_work = {
            "work_id": "w1",
            "actor": "act",
            "state": "stopped",
            "resource_ids": ["r1"],
            "resource_claims": [raw_claim],
            "evidence": [raw_ev],
        }
        raw_res = {
            "use_id": None,
            "absent_work_id": None,
            "resource_ids": ["r1"],
            "resolution": "stopped",
            "reason": "done",
        }
        rsh_pos = parse_command({
            **base_orch,
            "kind": "resolve_store_hold",
            "payload": {
                "backup_cutoff": "2026-10-06T00:00:00Z",
                "inventory_complete": True,
                "absent_work_inventory": [raw_work],
                "resource_resolutions": [raw_res],
                "reason": "ok",
            },
        })
        self.assertEqual(rsh_pos.kind, "resolve_store_hold")
        self.assertIsInstance(rsh_pos.payload["absent_work_inventory"][0], HostWork)
        self.assertIsInstance(rsh_pos.payload["resource_resolutions"][0], ResourceResolution)
        with self.assertRaises(DomainError):
            parse_command({**base_orch, "kind": "resolve_store_hold", "payload": {"backup_cutoff": "2026-10-06T00:00:00Z", "inventory_complete": "not-bool", "absent_work_inventory": (), "resource_resolutions": (), "reason": "ok"}})

        # Non-finite float tests inside nested JSON (original_snapshot in import_plan)
        with self.assertRaises(DomainError) as ctx_nan:
            parse_command({
                **base_boot,
                "kind": "import_plan",
                "payload": {
                    "host_id": "h",
                    "source_directory_id": "d1",
                    "source_directory_claim": None,
                    "original_task_id": "t1",
                    "original_snapshot": {"schemaVersion": 2, "metric": float("nan")},
                    "provenance": raw_prov,
                    "writers_stopped": "yes",
                    "known_contexts": {},
                },
            })
        self.assertIn("Non-finite float", str(ctx_nan.exception))

        with self.assertRaises(DomainError) as ctx_inf:
            parse_command({
                **base_boot,
                "kind": "import_plan",
                "payload": {
                    "host_id": "h",
                    "source_directory_id": "d1",
                    "source_directory_claim": None,
                    "original_task_id": "t1",
                    "original_snapshot": {"schemaVersion": 2, "metric": float("inf")},
                    "provenance": raw_prov,
                    "writers_stopped": "yes",
                    "known_contexts": {},
                },
            })
        self.assertIn("Non-finite float", str(ctx_inf.exception))

        with self.assertRaises(DomainError) as ctx_ninf:
            parse_command({
                **base_boot,
                "kind": "import_plan",
                "payload": {
                    "host_id": "h",
                    "source_directory_id": "d1",
                    "source_directory_claim": None,
                    "original_task_id": "t1",
                    "original_snapshot": {"schemaVersion": 2, "metric": float("-inf")},
                    "provenance": raw_prov,
                    "writers_stopped": "yes",
                    "known_contexts": {},
                },
            })
        self.assertIn("Non-finite float", str(ctx_ninf.exception))

    def test_prepare_publication_propagates_internal_type_error(self):
        """Validator-internal TypeError propagates and max_history is never silently dropped."""
        prop = legacy_proposal()
        call_records = []
        def faulty_validator(published, **kwargs):
            call_records.append(kwargs)
            if "max_history" in kwargs:
                raise TypeError("Internal bug in validator implementation with max_history")

        with self.assertRaises(TypeError) as ctx:
            prepare_publication(
                prop, None, timestamp="2026-10-06T12:00:00Z", generation="gen-1",
                expected_revision=0, expected_generation=None, authorization_note="note",
                max_file_bytes=10000, max_history=10,
                validate_snapshot_fn=faulty_validator,
            )
        self.assertIn("Internal bug in validator implementation with max_history", str(ctx.exception))
        self.assertEqual(len(call_records), 1)
        self.assertEqual(call_records[0].get("max_file_bytes"), 10000)
        self.assertEqual(call_records[0].get("max_history"), 10)

    def test_service_status_coordination_backing_and_planning_rejection(self):
        """planning with in-progress step is rejected; blocked requires stale attempt or hold; stopped allows active/submitted/stale attempt or hold."""
        prop = legacy_proposal()
        prop["steps"][0]["status"] = "in-progress"
        stale_attempt = AttemptView(
            "x", "step-1", "worker-a", "stale", "active", 1, "v1", "2026-10-06T00:00:00Z",
            context(None, writes=False), True, None
        )
        active_attempt = dataclasses.replace(stale_attempt, state="active")
        submitted_attempt = dataclasses.replace(stale_attempt, state="submitted")

        hold_step1 = HoldView("h1", "reassessment", "step-1", "src1", "hold on step-1")
        hold_step2 = HoldView("h2", "reassessment", "step-2", "src2", "hold on step-2")
        hold_global = HoldView("hg", "reassessment", None, "src3", "plan-wide hold")

        # 1. planning status with in-progress step ALWAYS rejected under all coordination states
        prop["status"] = "planning"
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination((stale_attempt,)))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(holds=(hold_global,)))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(store_hold=True))

        # 2. blocked status with in-progress step:
        # Requires stale attempt for step-1, step-1 hold, or global hold.
        # Live attempts (active/submitted) or unrelated step holds do NOT back in-progress under blocked.
        prop["status"] = "blocked"
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(()))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination((active_attempt,)))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination((submitted_attempt,)))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(holds=(hold_step2,)))

        # Passing conditions under blocked:
        validate_service_graph(prop, None, coordination((stale_attempt,)))
        validate_service_graph(prop, None, coordination(holds=(hold_step1,)))
        validate_service_graph(prop, None, coordination(holds=(hold_global,)))
        validate_service_graph(prop, None, coordination(store_hold=True))

        # 3. stopped status with in-progress step (Design line 97 explicit stop precedence):
        # In-progress step is valid when backed by active, submitted, or stale attempt for step-1,
        # or step-1 hold, or global hold.
        # Unbacked or unrelated step holds are rejected.
        prop["status"] = "stopped"
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(()))
        with self.assertRaises(PlanValidationError):
            validate_service_graph(prop, None, coordination(holds=(hold_step2,)))

        # Passing conditions under stopped:
        validate_service_graph(prop, None, coordination((active_attempt,)))
        validate_service_graph(prop, None, coordination((submitted_attempt,)))
        validate_service_graph(prop, None, coordination((stale_attempt,)))
        validate_service_graph(prop, None, coordination(holds=(hold_step1,)))
        validate_service_graph(prop, None, coordination(holds=(hold_global,)))
        validate_service_graph(prop, None, coordination(store_hold=True))

    def test_service_fixture_metadata_and_attempt_version(self):
        """ServiceFixture reads current metadata and attempt version when refresh_metadata=True, freezes on False."""
        from unittest.mock import MagicMock
        clock = FakeClock()
        mock_service = MagicMock()
        mock_service.read_service_info.return_value = InstallationView(
            epoch="epoch-current", schema_version=2, host_id="synthetic-host", dashboard_url="http://dash",
            store_hold=False, restore_cutoff=None
        )
        plan_meta = Metadata("epoch-current", "p-1", "gen-curr", 5, 2, "http://dash")
        mock_plan = MagicMock()
        mock_plan.metadata = plan_meta
        mock_service.read_plan.return_value = mock_plan

        coord_attempt = AttemptView(
            "att-1", "step-1", "worker-a", "active", "assigned", 4, "v1", "2026-10-06T00:00:00Z",
            context(None, writes=False), False, None
        )
        coord_page = CoordinationPage(
            plan_id="p-1", revision=5, store_sequence=1,
            items=(CoordinationItem(kind="attempt", value=coord_attempt),),
            next_cursor=None, total_counts={"attempts": 1}
        )
        mock_service.read_coordination.return_value = coord_page

        fixture = ServiceFixture(service=mock_service, store=None, directory=Path("/tmp"), clock=clock, epoch="epoch-init")

        receipt_meta = Metadata("epoch-orig", "p-1", "gen-orig", 1, 1, "url")
        receipt = MutationReceipt(
            committed={}, committed_metadata=receipt_meta, current_metadata=receipt_meta,
            replayed=False, private=PrivateGrant("orch-tok", None)
        )
        attempt_receipt = MutationReceipt(
            committed={"attempt_id": "att-1", "version": 1, "definition_version": "v1"},
            committed_metadata=receipt_meta, current_metadata=receipt_meta,
            replayed=False, private=PrivateGrant(None, "att-tok")
        )

        # refresh_metadata=True reads CURRENT epoch, generation, revision (5), and attempt version (4)
        cmd_refreshed = fixture.command(
            "report_activity",
            {"origin_worker": "worker-a", "relay": None, "observed_activity": "working"},
            receipt=receipt,
            attempt=attempt_receipt,
            refresh_metadata=True,
        )
        self.assertEqual(cmd_refreshed.epoch, "epoch-current")
        self.assertEqual(cmd_refreshed.generation, "gen-curr")
        self.assertEqual(cmd_refreshed.expected_revision, 5)
        self.assertEqual(cmd_refreshed.attempt_version, 4)
        self.assertEqual(cmd_refreshed.ownership_version, 1)

        # refresh_metadata=False freezes original metadata and attempt version (1)
        cmd_frozen = fixture.command(
            "report_activity",
            {"origin_worker": "worker-a", "relay": None, "observed_activity": "working"},
            receipt=receipt,
            attempt=attempt_receipt,
            refresh_metadata=False,
        )
        self.assertEqual(cmd_frozen.epoch, "epoch-orig")
        self.assertEqual(cmd_frozen.generation, "gen-orig")
        self.assertEqual(cmd_frozen.expected_revision, 1)
        self.assertEqual(cmd_frozen.attempt_version, 1)
        self.assertEqual(cmd_frozen.ownership_version, 1)

    def test_make_service_lazy_import_constructor(self):
        """make_service uses exact Store and PlanningService interfaces and port 8790."""
        from tests.service_fixtures import make_service, FakeClock
        import types

        calls = {}

        class DummyStore:
            def __init__(self, db_path, clock, host_id=None):
                calls["store_init"] = {"path": db_path, "clock": clock, "host_id": host_id}

            def installation_epoch(self):
                calls["installation_epoch"] = True
                return "epoch-synthetic-123"

        class DummyService:
            def __init__(self, store, base_url=None, clock=None):
                calls["service_init"] = {"store": store, "base_url": base_url, "clock": clock}

        app_mod = types.ModuleType("adaptive_planning.app")
        app_mod.PlanningService = DummyService
        store_mod = types.ModuleType("adaptive_planning.store")
        store_mod.Store = DummyStore

        orig_app = sys.modules.get("adaptive_planning.app")
        orig_store = sys.modules.get("adaptive_planning.store")
        sys.modules["adaptive_planning.app"] = app_mod
        sys.modules["adaptive_planning.store"] = store_mod
        try:
            clock = FakeClock()
            test_dir = Path("/tmp/synthetic_test_dir")
            fixture = make_service(test_dir, clock=clock)
            self.assertEqual(calls["store_init"]["path"], test_dir / "planning.sqlite")
            self.assertEqual(calls["store_init"]["clock"], clock)
            self.assertEqual(calls["store_init"]["host_id"], "synthetic-host")
            self.assertEqual(calls["service_init"]["base_url"], "http://127.0.0.1:8790")
            self.assertEqual(calls["service_init"]["clock"], clock)
            self.assertTrue(calls.get("installation_epoch"))
            self.assertEqual(fixture.epoch, "epoch-synthetic-123")
        finally:
            if orig_app is not None:
                sys.modules["adaptive_planning.app"] = orig_app
            else:
                sys.modules.pop("adaptive_planning.app", None)
            if orig_store is not None:
                sys.modules["adaptive_planning.store"] = orig_store
            else:
                sys.modules.pop("adaptive_planning.store", None)

    def test_service_fixture_refresh_metadata_error_propagation_and_pagination(self):
        """refresh_metadata=True propagates read errors directly and paginates coordination."""
        from unittest.mock import MagicMock
        from tests.service_fixtures import ServiceFixture, FakeClock

        clock = FakeClock()
        mock_service = MagicMock()
        fixture = ServiceFixture(service=mock_service, store=None, directory=Path("/tmp"), clock=clock, epoch="epoch-active")

        plan_meta = Metadata("epoch-curr", "plan-1", "gen-curr", 2, 1, "url")
        receipt = MutationReceipt(
            committed={}, committed_metadata=plan_meta, current_metadata=plan_meta,
            replayed=False, private=PrivateGrant("orch-tok", None)
        )
        attempt_receipt = MutationReceipt(
            committed={"attempt_id": "att-1", "version": 1, "definition_version": "v1"},
            committed_metadata=plan_meta, current_metadata=plan_meta,
            replayed=False, private=PrivateGrant(None, "att-tok")
        )

        # 1. read_service_info failure propagates directly
        mock_service.read_service_info.side_effect = DomainError("stale_epoch", "stale epoch in service")
        with self.assertRaises(DomainError) as ctx_err:
            fixture.command("create_plan", {"creation_id": "c1", "draft": draft(), "new_plan_intent": None}, refresh_metadata=True)
        self.assertEqual(ctx_err.exception.code, "stale_epoch")

        # 2. read_plan failure propagates directly
        mock_service.read_service_info.side_effect = None
        mock_service.read_service_info.return_value = MagicMock(epoch="epoch-curr")
        mock_service.read_plan.side_effect = DomainError("not_found", "plan not found")
        with self.assertRaises(DomainError) as ctx_err2:
            fixture.command("revise_plan", {"draft": draft(), "reason": "r", "authorization_change": None}, receipt=receipt, refresh_metadata=True)
        self.assertEqual(ctx_err2.exception.code, "not_found")

        # 3. read_coordination failure propagates directly
        mock_service.read_plan.side_effect = None
        mock_service.read_plan.return_value = MagicMock(metadata=plan_meta)
        mock_service.read_coordination.side_effect = DomainError("conflict", "coordination conflict")
        with self.assertRaises(DomainError) as ctx_err3:
            fixture.command("report_activity", {"origin_worker": "worker-a", "relay": None, "observed_activity": "w"}, receipt=receipt, attempt=attempt_receipt, refresh_metadata=True)
        self.assertEqual(ctx_err3.exception.code, "conflict")

        # 4. Multi-page pagination finds attempt on later page
        mock_service.read_coordination.side_effect = None
        page1_item = CoordinationItem(kind="attempt", value=AttemptView("att-other", "s0", "w0", "active", "assigned", 1, "v1", "ts", context(None, writes=False), False, None))
        page2_item = CoordinationItem(kind="attempt", value=AttemptView("att-1", "s1", "worker-a", "active", "assigned", 9, "v1", "ts", context(None, writes=False), False, None))
        page1 = CoordinationPage("plan-1", 2, 1, (page1_item,), "cursor-p2", {"attempts": 2})
        page2 = CoordinationPage("plan-1", 2, 2, (page2_item,), None, {"attempts": 2})

        def fake_read_coordination(plan_id, after=None):
            if after is None:
                return page1
            elif after == "cursor-p2":
                return page2
            raise ValueError(f"Unknown cursor {after}")

        mock_service.read_coordination.side_effect = fake_read_coordination
        cmd_paged = fixture.command(
            "report_activity",
            {"origin_worker": "worker-a", "relay": None, "observed_activity": "w"},
            receipt=receipt,
            attempt=attempt_receipt,
            refresh_metadata=True,
        )
        self.assertEqual(cmd_paged.attempt_version, 9)

        # 5. Missing attempt across pages raises ValueError
        page2_without_target = CoordinationPage("plan-1", 2, 2, (), None, {"attempts": 1})
        mock_service.read_coordination.side_effect = lambda plan_id, after=None: page1 if after is None else page2_without_target
        with self.assertRaises(ValueError):
            fixture.command(
                "report_activity",
                {"origin_worker": "worker-a", "relay": None, "observed_activity": "w"},
                receipt=receipt,
                attempt=attempt_receipt,
                refresh_metadata=True,
            )

        # 6. Malformed DTO missing required attribute raises AttributeError (no silent fallback)
        malformed_plan_view = MagicMock(spec=[])  # no metadata attribute
        mock_service.read_plan.return_value = malformed_plan_view
        with self.assertRaises(AttributeError):
            fixture.command(
                "revise_plan",
                {"draft": draft(), "reason": "r", "authorization_change": None},
                receipt=receipt,
                refresh_metadata=True,
            )

    def test_import_plan_source_directory_fields_and_exclusivity(self):
        """import_plan requires both source_directory_id and source_directory_claim with exactly one non-null."""
        base_cmd = {
            "kind": "import_plan",
            "epoch": "epoch-1",
            "key": "k-ip",
            "actor": "orch",
            "payload": {
                "host_id": "host-1",
                "original_task_id": "task-1",
                "original_snapshot": {"schemaVersion": 2},
                "provenance": {"path": "p", "branch": None, "commit": None, "version": "v1", "task_id": None},
                "writers_stopped": "stopped",
                "known_contexts": {},
            },
        }

        # Omitted source_directory_id
        cmd_omitted_id = copy.deepcopy(base_cmd)
        cmd_omitted_id["payload"]["source_directory_claim"] = None
        with self.assertRaises(DomainError) as ctx1:
            parse_command(cmd_omitted_id)
        self.assertEqual(ctx1.exception.code, "invalid")

        # Omitted source_directory_claim
        cmd_omitted_claim = copy.deepcopy(base_cmd)
        cmd_omitted_claim["payload"]["source_directory_id"] = "res-1"
        with self.assertRaises(DomainError) as ctx2:
            parse_command(cmd_omitted_claim)
        self.assertEqual(ctx2.exception.code, "invalid")

        # Both non-null
        cmd_both = copy.deepcopy(base_cmd)
        cmd_both["payload"]["source_directory_id"] = "res-1"
        cmd_both["payload"]["source_directory_claim"] = {
            "host_id": "h", "kind": "workspace", "device": None, "inode": None,
            "canonical_path": "/p", "case_insensitive": False, "repository_id": None, "external_identity": None
        }
        with self.assertRaises(DomainError) as ctx3:
            parse_command(cmd_both)
        self.assertEqual(ctx3.exception.code, "invalid")

        # Both null
        cmd_neither = copy.deepcopy(base_cmd)
        cmd_neither["payload"]["source_directory_id"] = None
        cmd_neither["payload"]["source_directory_claim"] = None
        with self.assertRaises(DomainError) as ctx4:
            parse_command(cmd_neither)
        self.assertEqual(ctx4.exception.code, "invalid")

        # Exactly one non-null (id)
        cmd_ok_id = copy.deepcopy(base_cmd)
        cmd_ok_id["payload"]["source_directory_id"] = "res-1"
        cmd_ok_id["payload"]["source_directory_claim"] = None
        parsed_id = parse_command(cmd_ok_id)
        self.assertEqual(parsed_id.payload["source_directory_id"], "res-1")
        self.assertIsNone(parsed_id.payload["source_directory_claim"])

        # Exactly one non-null (claim)
        cmd_ok_claim = copy.deepcopy(base_cmd)
        cmd_ok_claim["payload"]["source_directory_id"] = None
        cmd_ok_claim["payload"]["source_directory_claim"] = {
            "host_id": "h", "kind": "workspace", "device": None, "inode": None,
            "canonical_path": "/p", "case_insensitive": False, "repository_id": None, "external_identity": None
        }
        parsed_claim = parse_command(cmd_ok_claim)
        self.assertIsNone(parsed_claim.payload["source_directory_id"])
        self.assertIsInstance(parsed_claim.payload["source_directory_claim"], ResourceClaim)

    def test_raw_wire_dto_normative_fields_and_claims_discrimination(self):
        """Raw wire DTO mappings require normative fields and ExecutionContextClaims is discriminated robustly."""
        from adaptive_planning.types import _to_authorization, _to_evidence

        # ExecutionContextClaims discrimination without repository_claim
        claims_mapping = {
            "kind": "non-repository",
            "repository_claim": None,
            "workspace_claim": {
                "host_id": "h", "kind": "workspace", "device": None, "inode": None,
                "canonical_path": "/p", "case_insensitive": False, "repository_id": None, "external_identity": None
            },
            "repository_exclusive": False,
            "external_claims": (),
            "writes": True,
            "checked_base": None,
        }
        cmd_claims = parse_command({
            "kind": "resolve_attempt",
            "epoch": "epoch-1",
            "key": "k-claim",
            "actor": "orch",
            "orchestrator_token": "orch-tok",
            "payload": {
                "resolution": "attach-context",
                "reason": "attach claims",
                "next_step_state": "pending",
                "stopped_resource_ids": (),
                "replacement_assignee": None,
                "replacement_context": None,
                "attached_context": claims_mapping,
            },
        })
        self.assertIsInstance(cmd_claims.payload["attached_context"], ExecutionContextClaims)

        # Omitted required field in raw wire mapping: Authorization without claimed
        bad_auth_mapping = {
            "mode": "plan-only",
            "interval": "1h",
            "since_revision": 0,
            "note": "note",
            "baseline": (),
            # "claimed" omitted!
        }
        with self.assertRaises(DomainError) as ctx_a:
            _to_authorization(bad_auth_mapping)
        self.assertEqual(ctx_a.exception.code, "invalid")

        # Omitted required field in raw wire mapping: Evidence without limitations
        bad_ev_mapping = {
            "summary": "s", "check": "c", "result": "passed",
            "artifact": None, "workspace_id": None,
            # "limitations" and "blockers" omitted!
        }
        with self.assertRaises(DomainError) as ctx_e:
            _to_evidence(bad_ev_mapping)
        self.assertEqual(ctx_e.exception.code, "invalid")

    def test_strict_json_trees_leaves_workflow_and_recursion(self):
        """JSON subtrees reject non-JSON leaves and cycles; workflow enforces mapping[str, str]; RecursionError converts to DomainError."""
        from adaptive_planning.types import _to_plan_draft

        # 1. Non-JSON leaves in original_snapshot
        bad_snap_set = {"schemaVersion": 2, "tags": {"a", "b"}}
        with self.assertRaises(DomainError) as ctx_set:
            parse_command({
                "kind": "import_plan", "epoch": "epoch-1", "key": "k-set", "actor": "orch",
                "payload": {
                    "host_id": "h", "source_directory_id": "d1", "source_directory_claim": None,
                    "original_task_id": "t1", "original_snapshot": bad_snap_set,
                    "provenance": {"path": "p", "branch": None, "commit": None, "version": "v1", "task_id": None},
                    "writers_stopped": "yes", "known_contexts": {},
                }
            })
        self.assertEqual(ctx_set.exception.code, "invalid")

        bad_snap_bytes = {"schemaVersion": 2, "blob": b"binary"}
        with self.assertRaises(DomainError) as ctx_bytes:
            parse_command({
                "kind": "import_plan", "epoch": "epoch-1", "key": "k-b", "actor": "orch",
                "payload": {
                    "host_id": "h", "source_directory_id": "d1", "source_directory_claim": None,
                    "original_task_id": "t1", "original_snapshot": bad_snap_bytes,
                    "provenance": {"path": "p", "branch": None, "commit": None, "version": "v1", "task_id": None},
                    "writers_stopped": "yes", "known_contexts": {},
                }
            })
        self.assertEqual(ctx_bytes.exception.code, "invalid")

        # 2. Cyclic reference in original_snapshot
        cyclic_snap = {"schemaVersion": 2}
        cyclic_snap["cycle"] = cyclic_snap
        with self.assertRaises(DomainError) as ctx_cycle:
            parse_command({
                "kind": "import_plan", "epoch": "epoch-1", "key": "k-cyc", "actor": "orch",
                "payload": {
                    "host_id": "h", "source_directory_id": "d1", "source_directory_claim": None,
                    "original_task_id": "t1", "original_snapshot": cyclic_snap,
                    "provenance": {"path": "p", "branch": None, "commit": None, "version": "v1", "task_id": None},
                    "writers_stopped": "yes", "known_contexts": {},
                }
            })
        self.assertEqual(ctx_cycle.exception.code, "invalid")

        # 3. Non-JSON leaves in MutationReceipt committed
        with self.assertRaises(DomainError) as ctx_rcpt:
            meta = Metadata("epoch-1", "p-1", "g-1", 1, 1, "url")
            MutationReceipt(committed={"bad": {1, 2}}, committed_metadata=meta, current_metadata=meta, replayed=False, private=None)
        self.assertEqual(ctx_rcpt.exception.code, "invalid")

        # 4. PlanDraft.workflow rejects non-string values
        with self.assertRaises(DomainError) as ctx_wf:
            d = draft()
            object.__setattr__(d, "workflow", {"step": 123})
            _to_plan_draft(d)
        self.assertEqual(ctx_wf.exception.code, "invalid")


if __name__ == "__main__":
    unittest.main()

