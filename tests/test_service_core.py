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
    CommandEnvelope,
    DomainError,
    ExecutionContext,
    PlanDraft,
    PrivateGrant,
    StepDefinition,
    parse_command,
)
from tests.service_fixtures import (
    checkpoint,
    context,
    coordination,
    draft,
    legacy_proposal,
    step,
)


ROOT = Path(__file__).resolve().parents[1]


class TestServiceCore(unittest.TestCase):
    """Unit tests for extracted pure planning core and boundary consistency."""

    def test_core_import_without_service_dependencies(self):
        """Pure core and types must import cleanly under Python 3.10 without SDK dependencies."""
        # 1. Fresh isolated subprocess import proof with import blocker installed BEFORE import
        isolated_code = (
            "import sys\n"
            "blocked = {'mcp', 'starlette', 'uvicorn', 'httpx2', 'pydantic'}\n"
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

        # 3. > 10 MB limit
        p_oversize = legacy_proposal()
        p_oversize["steps"][0]["details"] = "x" * (10 * 1024 * 1024 + 10)
        with self.assertRaises(PlanValidationError):
            validate_plan(p_oversize)
        self.assertEqual(len(p_oversize["steps"][0]["details"]), 10 * 1024 * 1024 + 10)

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
        fixture = ServiceFixture(service=None, store=None, directory=Path("/tmp"), clock=clock, epoch="epoch-active")

        # Seed cached grant
        plan_id = "plan-seeded"
        fixture._cached_grants[plan_id] = PrivateGrant(orchestrator_token="cached-orch-token", attempt_token=None)

        # Receipt with private=None falls back to cached grant
        meta_committed = Metadata("epoch-1", plan_id, "gen-1", 1, 1, "url")
        meta_current = Metadata("epoch-active", plan_id, "gen-1", 2, 1, "url")
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


if __name__ == "__main__":
    unittest.main()

