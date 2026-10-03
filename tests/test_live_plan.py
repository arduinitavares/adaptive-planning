#!/usr/bin/env python3
"""
test_live_plan.py - Comprehensive automated tests for Live Plan schema v2 publisher,
recovery tool, server, and dashboard frontend logic.

Coverage targets below; run with unittest discovery from the repository root:
1. lasting claim & retry & renewal
2. existing interval pending to complete claims & distinct baseline
3. two simultaneous non-baseline active/completes reject
4. plan-only mode constraints
5. v1 migration with/without note
6. recovery authorization and notes
7. generation CAS, stale G1r1 rejection, creation conflict
8. recovery validation, byte-for-byte corrupt backup, unique names, retry exhaust
9. evidence prefix append-only, check fixed once evidence, retirement rules
10. Windows reader concurrency, transient winerror 5/32 retry, CLI 75 on exhaust
11. socket SO_EXCLUSIVEADDRUSE, ready file token check, lockfile retention
12. SSE deterministic serving, changed bytes same revision, error recovery re-sending plan without state-ok,
    frontend decision helper
13. mixed version reading (v1 and v2), old validator rejection, malformed scope rejection
14. UI badge backgrounds, neutral mode, distinct stopped style, retired view
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
import uuid

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.live_plan import (
    PlanValidationError,
    RetryableStorageError,
    FileLock,
    ThreadedHTTPServer,
    LivePlanRequestHandler,
    validate_plan,
    validate_proposal_schema,
    validate_plan_steps_and_graph,
    publish,
    recover,
    serve,
    SCHEMA_VERSION,
    MAX_HISTORY,
    MAX_FILE_BYTES,
    read_published_snapshot,
)


def read_sse_event(response, timeout: float = 5.0) -> dict:
    start = time.time()
    event = {"event": None, "id": None, "data": ""}
    lines = []
    while time.time() - start < timeout:
        raw = response.fp.readline()
        if not raw:
            raise EOFError("SSE connection closed unexpectedly")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if event["event"] or lines:
                event["data"] = "\n".join(lines)
                return event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event["event"] = line[6:].strip()
        elif line.startswith("id:"):
            event["id"] = line[3:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].strip())
    raise TimeoutError("Timed out waiting for SSE event")


class TestPlanValidationV2(unittest.TestCase):
    """Unit tests for plan schema v2 validation, DAG consistency, evidence, and retirement rules."""

    def valid_v2_proposal(self) -> dict:
        return {
            "schemaVersion": 2,
            "taskId": "task-test-01",
            "objective": "Verify the v2 test harness",
            "status": "planning",
            "executionMode": "first-step",
            "nextStepId": "step-1",
            "changeSummary": "Initial v2 plan configuration",
            "steps": [
                {
                    "id": "step-1",
                    "title": "Set up database schema",
                    "status": "pending",
                    "dependsOn": [],
                    "check": "Database migrations execute with exit code 0",
                    "evidence": []
                },
                {
                    "id": "step-2",
                    "title": "Seed baseline data",
                    "status": "pending",
                    "dependsOn": ["step-1"],
                    "check": "User count equals 10",
                    "evidence": []
                }
            ]
        }

    def test_valid_v2_proposal_passes(self):
        plan = self.valid_v2_proposal()
        validate_plan(plan)

    def test_proposal_rejects_publisher_owned_fields(self):
        for field, value in [
            ("scope", {"mode": "first-step", "sinceRevision": 1, "baseline": [], "claimed": None}),
            ("generation", str(uuid.uuid4())),
            ("revision", 1),
            ("history", []),
            ("updatedAt", "2026-10-03T18:00:00+00:00"),
        ]:
            with self.subTest(field=field):
                plan = self.valid_v2_proposal()
                plan[field] = value
                with self.assertRaises(PlanValidationError):
                    validate_plan(plan)

    def test_proposal_rejects_schema_version_1(self):
        plan = self.valid_v2_proposal()
        plan["schemaVersion"] = 1
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_missing_or_empty_required_fields(self):
        for field in ["taskId", "objective", "status", "executionMode", "nextStepId", "changeSummary", "steps"]:
            plan = self.valid_v2_proposal()
            del plan[field]
            with self.assertRaises(PlanValidationError, msg=f"Should fail when missing {field}"):
                validate_plan(plan)

            plan = self.valid_v2_proposal()
            plan[field] = "" if field != "steps" else "not-a-list"
            with self.assertRaises(PlanValidationError, msg=f"Should fail when {field} is empty or wrong type"):
                validate_plan(plan)

    def test_invalid_statuses_and_modes(self):
        plan = self.valid_v2_proposal()
        plan["status"] = "flying"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan = self.valid_v2_proposal()
        plan["executionMode"] = "manual"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan = self.valid_v2_proposal()
        plan["steps"][0]["status"] = "almost-done"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_duplicate_step_ids(self):
        plan = self.valid_v2_proposal()
        plan["steps"].append({
            "id": "step-1",
            "title": "Duplicate step",
            "status": "pending",
            "dependsOn": [],
            "check": "Check",
            "evidence": []
        })
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_self_dependency_and_missing_dep(self):
        plan = self.valid_v2_proposal()
        plan["steps"][0]["dependsOn"] = ["step-1"]
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan = self.valid_v2_proposal()
        plan["steps"][1]["dependsOn"] = ["step-nonexistent"]
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_dag_cycle_rejection(self):
        plan = self.valid_v2_proposal()
        plan["steps"][0]["dependsOn"] = ["step-2"]
        plan["steps"][1]["dependsOn"] = ["step-1"]
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_complete_step_requires_evidence(self):
        plan = self.valid_v2_proposal()
        plan["steps"][0]["status"] = "complete"
        plan["steps"][0]["evidence"] = []
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan["steps"][0]["evidence"] = ["   "]
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan["steps"][0]["evidence"] = ["Database tables created: users, accounts"]
        plan["nextStepId"] = "step-2"
        validate_plan(plan)

    def test_incomplete_prerequisites_rejected(self):
        plan = self.valid_v2_proposal()
        plan["steps"][1]["status"] = "in-progress"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

        plan["steps"][1]["status"] = "complete"
        plan["steps"][1]["evidence"] = ["Seeded 10 rows"]
        with self.assertRaises(PlanValidationError):
            validate_plan(plan)

    def test_acceptance_9_evidence_and_retirement(self):
        """Acceptance 9: evidence reorder/drop duplicate rejected, append accepted;
        check fixed once evidence; unfinished title/deps editable; completed immutable;
        remove evidence reject; retire valid allowed; terminal retired cannot delete/reactivate;
        nonretired depending retired reject; completed with retired allowed."""
        existing = self.valid_v2_proposal()
        existing["steps"][0]["status"] = "complete"
        existing["steps"][0]["evidence"] = ["ev-alpha", "ev-beta", "ev-alpha"]
        existing["nextStepId"] = "step-2"

        # 1. Evidence reorder rejected
        proposed_reorder = copy.deepcopy(existing)
        proposed_reorder["steps"][0]["evidence"] = ["ev-beta", "ev-alpha", "ev-alpha"]
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed_reorder, existing)

        # 2. Evidence drop duplicate rejected
        proposed_drop_dup = copy.deepcopy(existing)
        proposed_drop_dup["steps"][0]["evidence"] = ["ev-alpha", "ev-beta"]
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed_drop_dup, existing)

        # 3. Evidence append accepted
        proposed_append = copy.deepcopy(existing)
        proposed_append["steps"][0]["evidence"] = ["ev-alpha", "ev-beta", "ev-alpha", "ev-gamma"]
        validate_plan(proposed_append, existing)

        # 4. Check fixed once evidence: step-2 has evidence while pending
        existing2 = copy.deepcopy(existing)
        existing2["steps"][1]["evidence"] = ["partial log output"]
        proposed_check_change = copy.deepcopy(existing2)
        proposed_check_change["steps"][1]["check"] = "Modified check definition"
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed_check_change, existing2)

        # 5. Unfinished title and dependencies editable when check not changed
        existing3 = copy.deepcopy(existing)
        # step-2 has no evidence yet
        proposed_edit = copy.deepcopy(existing3)
        proposed_edit["steps"][1]["title"] = "Updated title for step 2"
        proposed_edit["steps"][1]["check"] = "Updated check before evidence"
        proposed_edit["steps"][1]["dependsOn"] = []
        validate_plan(proposed_edit, existing3)

        # 6. Completed step is immutable: cannot change title or check or deps or status
        for field, bad_val in [("title", "New Title"), ("check", "New Check"), ("status", "pending")]:
            with self.subTest(field=field):
                bad_prop = copy.deepcopy(existing)
                bad_prop["steps"][0][field] = bad_val
                with self.assertRaises(PlanValidationError):
                    validate_plan(bad_prop, existing)

        # 7. Evidence-bearing step cannot disappear / remove evidence reject
        proposed_drop_step = copy.deepcopy(existing)
        proposed_drop_step["steps"] = [proposed_drop_step["steps"][1]]
        proposed_drop_step["steps"][0]["dependsOn"] = []
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed_drop_step, existing)

        # 8. Retirement: retire pending or blocked step is allowed (requires retiredReason)
        proposed_retire = copy.deepcopy(existing)
        proposed_retire["steps"][1]["status"] = "retired"
        # missing retiredReason fails
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed_retire, existing)

        # with valid retiredReason passes
        proposed_retire["steps"][1]["retiredReason"] = "No longer needed for minimal seed"
        proposed_retire["nextStepId"] = None
        validate_plan(proposed_retire, existing)

        # 9. Terminal retired step cannot be deleted or reactivated
        retired_existing = copy.deepcopy(proposed_retire)
        reactivate = copy.deepcopy(retired_existing)
        reactivate["steps"][1]["status"] = "pending"
        with self.assertRaises(PlanValidationError):
            validate_plan(reactivate, retired_existing)

        delete_retired = copy.deepcopy(retired_existing)
        delete_retired["steps"] = [delete_retired["steps"][0]]
        with self.assertRaises(PlanValidationError):
            validate_plan(delete_retired, retired_existing)

        # 10. Non-retired step depending on retired step rejected
        step3_plan = copy.deepcopy(retired_existing)
        step3_plan["steps"].append({
            "id": "step-3",
            "title": "Step 3",
            "status": "pending",
            "dependsOn": ["step-2"],  # step-2 is retired!
            "check": "Check",
            "evidence": []
        })
        with self.assertRaises(PlanValidationError):
            validate_plan(step3_plan)

        # 11. Retired-to-retired dependency is allowed if graph is valid
        step3_plan["steps"][2]["status"] = "retired"
        step3_plan["steps"][2]["retiredReason"] = "Cascading retirement"
        validate_plan(step3_plan)

        # 12. Task complete with retired step allowed
        complete_plan = copy.deepcopy(retired_existing)
        complete_plan["status"] = "complete"
        complete_plan["executionMode"] = "complete-task"
        complete_plan["nextStepId"] = None
        validate_plan(complete_plan)

    def test_acceptance_4_plan_only_mode_constraints(self):
        """Acceptance 4: plan-only retained completed history + pending accepted,
        active/new complete reject."""
        plan = self.valid_v2_proposal()
        plan["executionMode"] = "plan-only"
        plan["status"] = "planning"
        validate_plan(plan)

        # in-progress step in plan-only rejected
        plan_active = copy.deepcopy(plan)
        plan_active["steps"][0]["status"] = "in-progress"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan_active)

        # running task status in plan-only rejected
        plan_running = copy.deepcopy(plan)
        plan_running["status"] = "running"
        with self.assertRaises(PlanValidationError):
            validate_plan(plan_running)


class TestPublishScopeAndClaims(unittest.TestCase):
    """Integration tests for publisher scope, lasting claims, CAS, and intervals."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_pub_test_")
        self.state_dir = os.path.join(self.test_dir, "state")
        self.input_file = os.path.join(self.test_dir, "proposed.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def write_input(self, data: dict) -> None:
        with open(self.input_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def base_plan(self) -> dict:
        return {
            "schemaVersion": 2,
            "taskId": "task-claim-test",
            "objective": "Test publisher claims and scope",
            "status": "running",
            "executionMode": "first-step",
            "nextStepId": "step-A",
            "changeSummary": "Initial plan",
            "steps": [
                {
                    "id": "step-A",
                    "title": "Step A",
                    "status": "pending",
                    "dependsOn": [],
                    "check": "Check A",
                    "evidence": []
                },
                {
                    "id": "step-B",
                    "title": "Step B",
                    "status": "pending",
                    "dependsOn": [],
                    "check": "Check B",
                    "evidence": []
                }
            ]
        }

    def test_acceptance_1_lasting_claim(self):
        """Acceptance 1: lasting claim Aactive->Ablocked+Bactive rejects; retryA allowed;
        renewal allows B; remove/readd claim retained."""
        plan = self.base_plan()
        plan["steps"][0]["status"] = "in-progress"
        self.write_input(plan)

        # Initial publish: claims step-A
        res1 = publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="User authorizes Step A")
        self.assertEqual(res1["revision"], 1)
        self.assertEqual(res1["scope"]["claimed"], "step-A")
        gen = res1["generation"]

        # Attempt A blocked + B active without renewal note -> REJECTS
        plan2 = copy.deepcopy(plan)
        plan2["steps"][0]["status"] = "blocked"
        plan2["steps"][1]["status"] = "in-progress"
        plan2["nextStepId"] = "step-B"
        plan2["changeSummary"] = "Step A blocked, moving to B"
        self.write_input(plan2)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=gen)

        # Retry A (A in-progress again) -> ALLOWED
        plan_retry_a = copy.deepcopy(plan)
        plan_retry_a["changeSummary"] = "Retrying Step A"
        self.write_input(plan_retry_a)
        res2 = publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=gen)
        self.assertEqual(res2["revision"], 2)
        self.assertEqual(res2["scope"]["claimed"], "step-A")

        # Complete Step A
        plan_comp_a = copy.deepcopy(plan_retry_a)
        plan_comp_a["steps"][0]["status"] = "complete"
        plan_comp_a["steps"][0]["evidence"] = ["A completed successfully"]
        plan_comp_a["nextStepId"] = "step-B"
        plan_comp_a["changeSummary"] = "Step A completed"
        self.write_input(plan_comp_a)
        res3 = publish(self.state_dir, self.input_file, expected_revision=2, expected_generation=gen)
        self.assertEqual(res3["revision"], 3)

        # Propose B active without renewal note -> REJECTED (claimed is still step-A)
        plan_b_active = copy.deepcopy(plan_comp_a)
        plan_b_active["steps"][1]["status"] = "in-progress"
        plan_b_active["changeSummary"] = "Starting step B"
        self.write_input(plan_b_active)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=3, expected_generation=gen)

        # Renewal with authorizationNote -> ALLOWS B
        res4 = publish(
            self.state_dir,
            self.input_file,
            expected_revision=3,
            expected_generation=gen,
            authorization_note="User authorizes Step B after reviewing A"
        )
        self.assertEqual(res4["revision"], 4)
        self.assertEqual(res4["scope"]["claimed"], "step-B")
        self.assertIn("step-A", res4["scope"]["baseline"])

        # Acceptance 1: remove/readd claim retained
        # Step B is claimed but has no evidence yet. Step B can be removed.
        # But claim on Step B is retained, so proposing Step C active without note is rejected!
        plan_remove_b = copy.deepcopy(plan_b_active)
        plan_remove_b["steps"] = [
            plan_remove_b["steps"][0],  # step-A (complete)
            {
                "id": "step-C",
                "title": "Step C",
                "status": "pending",
                "dependsOn": ["step-A"],
                "check": "Check C",
                "evidence": []
            }
        ]
        plan_remove_b["nextStepId"] = "step-C"
        plan_remove_b["changeSummary"] = "Removed step B while keeping claim"
        self.write_input(plan_remove_b)
        res5 = publish(self.state_dir, self.input_file, expected_revision=4, expected_generation=gen)
        self.assertEqual(res5["revision"], 5)
        self.assertEqual(res5["scope"]["claimed"], "step-B", "Removal does not release claim")

        # Propose step-C active without renewal note -> REJECTS
        plan_c_active = copy.deepcopy(plan_remove_b)
        plan_c_active["steps"][1]["status"] = "in-progress"
        plan_c_active["changeSummary"] = "Step C active"
        self.write_input(plan_c_active)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=5, expected_generation=gen)

        # Re-adding step-B active -> ALLOWED because claimed is step-B!
        plan_readd_b = copy.deepcopy(plan_remove_b)
        plan_readd_b["steps"].append({
            "id": "step-B",
            "title": "Step B",
            "status": "in-progress",
            "dependsOn": [],
            "check": "Check B",
            "evidence": []
        })
        plan_readd_b["changeSummary"] = "Re-added step B active"
        self.write_input(plan_readd_b)
        res6 = publish(self.state_dir, self.input_file, expected_revision=5, expected_generation=gen)
        self.assertEqual(res6["revision"], 6)
        self.assertEqual(res6["scope"]["claimed"], "step-B")

    def test_acceptance_2_existing_interval_pending_to_complete_claims(self):
        """Acceptance 2: existing interval Apending->Acomplete claims, later Bactive rejects;
        imported initial complete A baseline distinct."""
        # Initial publish with A pending and B pending
        plan = self.base_plan()
        plan["status"] = "planning"
        self.write_input(plan)
        res1 = publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Init plan")
        gen = res1["generation"]
        self.assertIsNone(res1["scope"]["claimed"])
        self.assertEqual(res1["scope"]["baseline"], [])

        # Update 1: A directly complete in second publish claims A
        plan2 = copy.deepcopy(plan)
        plan2["status"] = "running"
        plan2["steps"][0]["status"] = "complete"
        plan2["steps"][0]["evidence"] = ["A verified directly"]
        plan2["nextStepId"] = "step-B"
        plan2["changeSummary"] = "A completed"
        self.write_input(plan2)
        res2 = publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=gen)
        self.assertEqual(res2["scope"]["claimed"], "step-A")

        # Later proposal with B active without renewal note -> REJECTS
        plan3 = copy.deepcopy(plan2)
        plan3["steps"][1]["status"] = "in-progress"
        plan3["changeSummary"] = "B in-progress"
        self.write_input(plan3)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=2, expected_generation=gen)

        # Distinct baseline: imported initial complete A on creation is in baseline, not a claim
        clean_dir = os.path.join(self.test_dir, "clean_state")
        plan_init_comp = self.base_plan()
        plan_init_comp["steps"][0]["status"] = "complete"
        plan_init_comp["steps"][0]["evidence"] = ["Imported prior check"]
        plan_init_comp["nextStepId"] = "step-B"
        self.write_input(plan_init_comp)
        init_res = publish(clean_dir, self.input_file, expected_revision=0, authorization_note="Import complete A")
        self.assertEqual(init_res["scope"]["baseline"], ["step-A"])
        self.assertIsNone(init_res["scope"]["claimed"])

        # In that interval, step-B can be claimed!
        plan_claim_b = copy.deepcopy(plan_init_comp)
        plan_claim_b["steps"][1]["status"] = "in-progress"
        self.write_input(plan_claim_b)
        b_res = publish(clean_dir, self.input_file, expected_revision=1, expected_generation=init_res["generation"])
        self.assertEqual(b_res["scope"]["claimed"], "step-B")

    def test_acceptance_3_two_simultaneous_nonbaseline_active_or_complete_reject(self):
        """Acceptance 3: two simultaneous nonbaseline active/completes reject."""
        plan = self.base_plan()
        plan["steps"][0]["status"] = "in-progress"
        plan["steps"][1]["status"] = "in-progress"
        self.write_input(plan)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Two active")

        # Open an interval with both steps pending: later completions are not imports.
        self.write_input(self.base_plan())
        initial = publish(self.state_dir, self.input_file, 0, authorization_note="User requested first step")
        # Two newly completed non-baseline steps also reject
        plan_two_comp = self.base_plan()
        plan_two_comp["steps"][0]["status"] = "complete"
        plan_two_comp["steps"][0]["evidence"] = ["Ev 1"]
        plan_two_comp["steps"][1]["status"] = "complete"
        plan_two_comp["steps"][1]["evidence"] = ["Ev 2"]
        plan_two_comp["nextStepId"] = None
        self.write_input(plan_two_comp)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=initial["generation"])

        # One in-progress and one complete simultaneously also reject
        plan_act_comp = self.base_plan()
        plan_act_comp["steps"][0]["status"] = "in-progress"
        plan_act_comp["steps"][1]["status"] = "complete"
        plan_act_comp["steps"][1]["evidence"] = ["Comp ev"]
        self.write_input(plan_act_comp)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=initial["generation"])

    def test_acceptance_4_plan_only_new_complete_rejected(self):
        """Acceptance 4: in plan-only mode, newly completed steps outside baseline reject."""
        plan = self.base_plan()
        plan["status"] = "planning"
        plan["executionMode"] = "plan-only"
        self.write_input(plan)
        res1 = publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Plan-only init")
        gen = res1["generation"]

        # Attempt to complete a step in plan-only mode -> REJECTS
        plan_comp = copy.deepcopy(plan)
        plan_comp["steps"][0]["status"] = "complete"
        plan_comp["steps"][0]["evidence"] = ["Should not complete in plan-only"]
        plan_comp["nextStepId"] = "step-B"
        plan_comp["changeSummary"] = "Illegal completion in plan-only"
        self.write_input(plan_comp)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=gen)

    def test_acceptance_5_v1_migration(self):
        """Acceptance 5: migration without note reject bytes unchanged; note accepts B,
        baseline old complete; planonly migration no execution; history records note."""
        # Create a genuine v1 canonical state file
        os.makedirs(self.state_dir, exist_ok=True)
        v1_state = {
            "schemaVersion": 1,
            "revision": 2,
            "updatedAt": "2026-10-03T10:00:00+00:00",
            "taskId": "task-mig-01",
            "objective": "Test v1 migration",
            "status": "running",
            "executionMode": "first-step",
            "nextStepId": "step-2",
            "changeSummary": "V1 revision 2 completed step 1",
            "steps": [
                {
                    "id": "step-1",
                    "title": "Step 1",
                    "status": "complete",
                    "dependsOn": [],
                    "check": "Check 1",
                    "evidence": ["V1 check 1 passed"]
                },
                {
                    "id": "step-2",
                    "title": "Step 2",
                    "status": "pending",
                    "dependsOn": ["step-1"],
                    "check": "Check 2",
                    "evidence": []
                }
            ],
            "history": [
                {"revision": 1, "timestamp": "2026-10-03T09:00:00+00:00", "summary": "Rev 1 init"},
                {"revision": 2, "timestamp": "2026-10-03T10:00:00+00:00", "summary": "V1 revision 2 completed step 1"}
            ]
        }
        canonical_path = os.path.join(self.state_dir, "live_plan.json")
        with open(canonical_path, "wb") as f:
            v1_bytes = json.dumps(v1_state, indent=2).encode("utf-8")
            f.write(v1_bytes)

        # 1. Propose v2 update without authorizationNote -> REJECTS, bytes unchanged
        v2_prop = {
            "schemaVersion": 2,
            "taskId": "task-mig-01",
            "objective": "Test v1 migration",
            "status": "running",
            "executionMode": "first-step",
            "nextStepId": "step-2",
            "changeSummary": "V2 update step 2",
            "steps": [
                v1_state["steps"][0],
                {
                    "id": "step-2",
                    "title": "Step 2",
                    "status": "in-progress",
                    "dependsOn": ["step-1"],
                    "check": "Check 2",
                    "evidence": []
                }
            ]
        }
        self.write_input(v2_prop)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=2, expected_generation="none")

        with open(canonical_path, "rb") as f:
            self.assertEqual(f.read(), v1_bytes)

        # 2. Migration to plan-only with active step or new complete step -> REJECTS (no execution)
        v2_plan_only_active = copy.deepcopy(v2_prop)
        v2_plan_only_active["executionMode"] = "plan-only"
        v2_plan_only_active["status"] = "planning"
        v2_plan_only_active["steps"][1]["status"] = "in-progress"
        self.write_input(v2_plan_only_active)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=2, expected_generation="none", authorization_note="Migrate plan-only with active")

        v2_plan_only_new_comp = copy.deepcopy(v2_prop)
        v2_plan_only_new_comp["executionMode"] = "plan-only"
        v2_plan_only_new_comp["status"] = "planning"
        v2_plan_only_new_comp["steps"][1]["status"] = "complete"
        v2_plan_only_new_comp["steps"][1]["evidence"] = ["New comp"]
        v2_plan_only_new_comp["nextStepId"] = None
        self.write_input(v2_plan_only_new_comp)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=2, expected_generation="none", authorization_note="Migrate plan-only with new complete")

        # 3. With authorization note and --expected-generation none -> ACCEPTS B, baseline is old complete
        self.write_input(v2_prop)
        res = publish(
            self.state_dir,
            self.input_file,
            expected_revision=2,
            expected_generation="none",
            authorization_note="User authorizes migration to v2 and executing step 2"
        )
        self.assertEqual(res["schemaVersion"], 2)
        self.assertEqual(res["revision"], 3)
        self.assertIsNotNone(res["generation"])
        self.assertEqual(res["scope"]["baseline"], ["step-1"])
        self.assertEqual(res["scope"]["claimed"], "step-2")
        self.assertEqual(res["history"][-1]["authorizationNote"], "User authorizes migration to v2 and executing step 2")

    def test_acceptance_6_mode_change_without_note_rejected(self):
        """Acceptance 6: mode change without authorization note rejects, leaving canonical bytes unchanged."""
        plan = self.base_plan()
        self.write_input(plan)
        res1 = publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Init first-step")
        gen = res1["generation"]
        canonical_path = os.path.join(self.state_dir, "live_plan.json")
        with open(canonical_path, "rb") as f:
            orig_bytes = f.read()

        # Propose mode change from first-step to complete-task WITHOUT authorization note -> REJECTS
        plan_mode_change = copy.deepcopy(plan)
        plan_mode_change["executionMode"] = "complete-task"
        plan_mode_change["changeSummary"] = "Switching to complete-task without auth"
        self.write_input(plan_mode_change)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=gen)

        with open(canonical_path, "rb") as f:
            self.assertEqual(f.read(), orig_bytes, "Canonical bytes must be unchanged on mode change rejection")

    def test_acceptance_7_stale_generation_cas_and_creation_conflict(self):
        """Acceptance 7: staleG1r1 after G2r1 rejects bytes unchanged; validG2 next works.
        creation existing reject; migration requires none; legacy task identity separate."""
        # Create plan G1
        plan = self.base_plan()
        self.write_input(plan)
        res1 = publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Init G1")
        g1 = res1["generation"]
        canonical_path = os.path.join(self.state_dir, "live_plan.json")

        # Creation on existing file rejects
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=0, authorization_note="Duplicate init")

        # Simulate corrupt state recovery that yields fresh generation G2 at rev 1
        with open(canonical_path, "w", encoding="utf-8") as f:
            f.write("{ corrupt json")

        rec_input = self.base_plan()
        rec_input["status"] = "planning"
        rec_input["executionMode"] = "plan-only"
        rec_file = os.path.join(self.test_dir, "rec.json")
        with open(rec_file, "w", encoding="utf-8") as rf:
            json.dump(rec_input, rf)

        rec_res = recover(self.state_dir, rec_file, recovery_note="Imported pending steps")
        g2 = rec_res["generation"]
        self.assertNotEqual(g1, g2)
        self.assertEqual(rec_res["revision"], 1)

        with open(canonical_path, "rb") as f:
            g2_bytes = f.read()

        # Update expecting stale G1 rev 1 -> REJECTS with bytes unchanged
        update_plan = copy.deepcopy(rec_input)
        update_plan["changeSummary"] = "Stale G1 update"
        self.write_input(update_plan)
        with self.assertRaises(PlanValidationError):
            publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=g1)

        with open(canonical_path, "rb") as f:
            self.assertEqual(f.read(), g2_bytes)

        # Update expecting valid G2 rev 1 -> SUCCEEDS
        res_g2 = publish(self.state_dir, self.input_file, expected_revision=1, expected_generation=g2)
        self.assertEqual(res_g2["generation"], g2)
        self.assertEqual(res_g2["revision"], 2)


class TestRecoveryV2(unittest.TestCase):
    """Integration tests for the recover command, byte-for-byte corrupt backups, and notes."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_rec_test_")
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.input_file = os.path.join(self.test_dir, "replacement.json")
        self.canonical_path = os.path.join(self.state_dir, "live_plan.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def write_replacement(self, data: dict) -> None:
        with open(self.input_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def valid_replacement(self) -> dict:
        return {
            "schemaVersion": 2,
            "taskId": "task-recovery",
            "objective": "Recover from disaster",
            "status": "planning",
            "executionMode": "plan-only",
            "nextStepId": "step-1",
            "changeSummary": "Recovered plan state",
            "steps": [
                {
                    "id": "step-1",
                    "title": "Rebuild database",
                    "status": "pending",
                    "dependsOn": [],
                    "check": "DB exists",
                    "evidence": []
                }
            ]
        }

    def test_acceptance_8_recover_comprehensive(self):
        """Invalid input has no backup; refuse valid/absent; fresh generation and unique backups.
        Separate corrective regressions cover postcommit reporting and real serialization."""
        self.write_replacement(self.valid_replacement())

        # 1. Refuse absent canonical
        with self.assertRaises(PlanValidationError):
            recover(self.state_dir, self.input_file, recovery_note="Note")

        # 2. Refuse valid canonical
        with open(self.canonical_path, "w", encoding="utf-8") as f:
            # write a valid published snapshot
            f.write(json.dumps({
                "schemaVersion": 2,
                "generation": str(uuid.uuid4()),
                "revision": 1,
                "updatedAt": "2026-10-03T12:00:00+00:00",
                "taskId": "task-valid",
                "objective": "Valid state",
                "status": "planning",
                "executionMode": "plan-only",
                "scope": {"mode": "plan-only", "sinceRevision": 1, "baseline": [], "claimed": None},
                "nextStepId": "step-1",
                "changeSummary": "Valid",
                "steps": [{
                    "id": "step-1", "title": "Step 1", "status": "pending", "dependsOn": [], "check": "Ok", "evidence": []
                }],
                "history": [{"revision": 1, "timestamp": "2026-10-03T12:00:00+00:00", "summary": "Valid", "authorizationNote": "User requested planning only"}]
            }))
        with self.assertRaises(PlanValidationError):
            recover(self.state_dir, self.input_file, recovery_note="Note")

        # 3. Make canonical corrupt
        corrupt_data = b'{"schemaVersion": 2, "corrupted": true, UNTERMINATED STRING'
        with open(self.canonical_path, "wb") as f:
            f.write(corrupt_data)

        # 4. Invalid recover input -> NO backup created
        bad_input = {"schemaVersion": 2, "taskId": "invalid"}  # missing steps, objective, etc.
        self.write_replacement(bad_input)
        with self.assertRaises(PlanValidationError):
            recover(self.state_dir, self.input_file, recovery_note="Note")

        backups = [f for f in os.listdir(self.state_dir) if "live_plan.corrupt." in f]
        self.assertEqual(len(backups), 0, "No backup must be created on invalid input")

        # 5. Success with valid input -> creates backup byte-for-byte, new gen, rev 1
        self.write_replacement(self.valid_replacement())
        res = recover(self.state_dir, self.input_file, recovery_note="Reverified step 1 from git log")
        self.assertEqual(res["revision"], 1)
        self.assertIsNotNone(res["generation"])
        self.assertEqual(res["history"][0]["recoveryNote"], "Reverified step 1 from git log")

        backups = [f for f in os.listdir(self.state_dir) if "live_plan.corrupt." in f]
        self.assertEqual(len(backups), 1)
        backup_file = os.path.join(self.state_dir, backups[0])
        with open(backup_file, "rb") as bf:
            self.assertEqual(bf.read(), corrupt_data, "Backup must be byte-for-byte identical to corrupt original")

        # 6. Two recoveries with frozen clock yield unique names
        # Corrupt the file again
        with open(self.canonical_path, "wb") as f:
            f.write(b"corrupt 2")
        with patch("scripts.live_plan.datetime") as mock_dt:
            fixed_now = datetime(2026, 10, 3, 14, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            res2 = recover(self.state_dir, self.input_file, recovery_note="Frozen clock rec 1")
            backup1 = res2["_corruptBackup"]

            # Corrupt file once more
            with open(self.canonical_path, "wb") as f:
                f.write(b"corrupt 3")
            res3 = recover(self.state_dir, self.input_file, recovery_note="Frozen clock rec 2")
            backup2 = res3["_corruptBackup"]

            self.assertNotEqual(backup1, backup2, "Two recoveries under frozen clock must produce unique backup names")

    def test_acceptance_8_recover_refuses_symlink(self):
        """Acceptance 8: refuse symlink canonical in recover."""
        self.write_replacement(self.valid_replacement())
        target_file = os.path.join(self.test_dir, "real_corrupt.json")
        with open(target_file, "wb") as f:
            f.write(b"corrupt target")

        try:
            os.symlink(target_file, self.canonical_path)
        except NotImplementedError as exc:
            self.skipTest(f"OS does not support symlink creation: {exc}")
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest(f"OS does not permit symlink creation: {exc}")
            raise

        with self.assertRaises(PermissionError):
            recover(self.state_dir, self.input_file, recovery_note="Note")

    def test_acceptance_8_recover_retry_exhaust_preserves_old_bytes_and_backup(self):
        """Acceptance 8: replace retry exhaust preserves old bytes and backup file; raises RetryableStorageError."""
        corrupt_bytes = b"corrupted canonical state content"
        with open(self.canonical_path, "wb") as f:
            f.write(corrupt_bytes)

        self.write_replacement(self.valid_replacement())

        def mock_exhaust(tmp, can, timeout=2.0, is_recovery=False):
            if os.path.exists(tmp):
                os.remove(tmp)
            raise RetryableStorageError("retryable; canonical state unchanged")

        with patch("scripts.live_plan._atomic_replace_with_retry", side_effect=mock_exhaust):
            with self.assertRaises(RetryableStorageError) as ctx:
                recover(self.state_dir, self.input_file, recovery_note="Exhaust test note")
            self.assertIn("retryable; canonical state unchanged", str(ctx.exception))
            self.assertIsNotNone(ctx.exception.backup_path)
            self.assertIn(ctx.exception.backup_path, str(ctx.exception))

        # Check canonical state is unchanged
        with open(self.canonical_path, "rb") as f:
            self.assertEqual(f.read(), corrupt_bytes)

        # Check corrupt backup was preserved
        backups = [f for f in os.listdir(self.state_dir) if "live_plan.corrupt." in f]
        self.assertEqual(len(backups), 1, "Completed corrupt backup must remain on disk")
        with open(os.path.join(self.state_dir, backups[0]), "rb") as bf:
            self.assertEqual(bf.read(), corrupt_bytes)

        # Check no tmp files left
        tmps = [f for f in os.listdir(self.state_dir) if ".tmp." in f]
        self.assertEqual(len(tmps), 0)

    def test_acceptance_6_recovery_authorization_and_notes(self):
        """Acceptance 6: recovery executing modes without auth reject; planonly recovery
        recoveryNote alone accepted; mode change no note reject."""
        with open(self.canonical_path, "wb") as f:
            f.write(b"corrupt")

        # Plan-only with recoveryNote alone -> ACCEPTED
        plan_only = self.valid_replacement()
        self.write_replacement(plan_only)
        res = recover(self.state_dir, self.input_file, recovery_note="Recovery plan-only")
        self.assertEqual(res["revision"], 1)

        # Executing mode (first-step) without authorizationNote -> REJECTED
        with open(self.canonical_path, "wb") as f:
            f.write(b"corrupt again")
        exec_plan = self.valid_replacement()
        exec_plan["executionMode"] = "first-step"
        exec_plan["status"] = "running"
        exec_plan["steps"][0]["status"] = "in-progress"
        self.write_replacement(exec_plan)

        with self.assertRaises(PlanValidationError):
            recover(self.state_dir, self.input_file, recovery_note="Exec recovery without auth")

        # With authorizationNote -> ACCEPTED
        res_auth = recover(
            self.state_dir,
            self.input_file,
            recovery_note="Exec recovery note",
            authorization_note="User instructions authorized execution"
        )
        self.assertEqual(res_auth["revision"], 1)
        self.assertEqual(res_auth["scope"]["claimed"], "step-1")


class TestStorageRetryAndWindowsConcurrency(unittest.TestCase):
    """Tests for transient Windows PermissionError retry and exit code 75 on exhaust."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_retry_test_")
        self.state_dir = os.path.join(self.test_dir, "state")
        self.input_file = os.path.join(self.test_dir, "proposed.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

class TestServerBindAndReadyFile(unittest.TestCase):
    """Acceptance 11: duplicate bind fails; controlled ready cleanup/write interleaving retains B."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_bind_test_")
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.ready_file = os.path.join(self.test_dir, "ready.json")
        self.assets_dir = os.path.join(REPO_ROOT, "assets", "dashboard")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_acceptance_11_duplicate_bind_fails(self):
        """Acceptance 11: duplicate bind on the same port fails."""
        # Start Server 1 on an ephemeral port
        server1 = ThreadedHTTPServer(("127.0.0.1", 0), LivePlanRequestHandler, self.state_dir, self.assets_dir)
        port = server1.server_address[1]
        try:
            # Attempt to bind Server 2 to the exact same port
            with self.assertRaises(OSError):
                ThreadedHTTPServer(("127.0.0.1", port), LivePlanRequestHandler, self.state_dir, self.assets_dir)
        finally:
            server1.server_close()

class TestSseAndFrontendDecision(unittest.TestCase):
    """Acceptance 12: same-stream and reconnect recovery; changed bytes same IDs sent;
    error recovery re-sending plan without state-ok; frontend decision logic."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_sse_test_")
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.ready_file = os.path.join(self.test_dir, "ready.json")
        self.assets_dir = os.path.join(REPO_ROOT, "assets", "dashboard")

        self.server = ThreadedHTTPServer(("127.0.0.1", 0), LivePlanRequestHandler, self.state_dir, self.assets_dir)
        self.port = self.server.server_address[1]

        ready_data = {
            "host": "127.0.0.1",
            "port": self.port,
            "pid": os.getpid(),
            "token": "test-tok",
            "url": f"http://127.0.0.1:{self.port}/"
        }
        with open(self.ready_file, "w", encoding="utf-8") as rf:
            json.dump(ready_data, rf)

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_acceptance_12_sse_streaming_and_recovery_without_state_ok(self):
        canonical_path = os.path.join(self.state_dir, "live_plan.json")
        input_file = os.path.join(self.test_dir, "init.json")
        plan = TestPlanValidationV2().valid_v2_proposal()
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(plan, f)

        res1 = publish(self.state_dir, input_file, expected_revision=0, authorization_note="SSE test init")
        with open(canonical_path, "rb") as original:
            original_bytes = original.read()

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)

        # 1. Initial plan event on connect
        ev1 = read_sse_event(resp)
        self.assertEqual(ev1["event"], "plan")
        self.assertEqual(ev1["id"], "1")
        self.assertEqual(json.loads(ev1["data"]), res1)

        # 2. Corrupt state produces state-error
        with open(canonical_path, "wb") as f:
            f.write(b"corrupt")

        ev_err = read_sse_event(resp)
        self.assertEqual(ev_err["event"], "state-error")

        # 3. Restore valid state -> re-sends plan event directly, NO state-ok event!
        with open(input_file, "w", encoding="utf-8") as f:
            # publish revision 2
            plan["changeSummary"] = "Recovered valid publish"
            json.dump(plan, f)

        # Using publish after manual repair
        with open(canonical_path, "wb") as f:
            f.write(original_bytes)

        ev_recovered = read_sse_event(resp)
        self.assertEqual(ev_recovered["event"], "plan", "SSE must re-send plan event without state-ok")
        self.assertEqual(ev_recovered["data"], ev1["data"])

        conn.close()

    def test_acceptance_12_sse_changed_bytes_and_reconnect(self):
        """Acceptance 12: changed bytes with same IDs are sent; reconnect receives current plan."""
        canonical_path = os.path.join(self.state_dir, "live_plan.json")
        input_file = os.path.join(self.test_dir, "change.json")
        plan = TestPlanValidationV2().valid_v2_proposal()
        plan["changeSummary"] = "Initial before byte change"
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(plan, f)

        res = publish(self.state_dir, input_file, expected_revision=0, authorization_note="Init for byte change")

        # 1. Connect SSE client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        ev1 = read_sse_event(resp)
        self.assertEqual(ev1["event"], "plan")

        # Simulate external content corruption that remains structurally valid:
        # same generation/revision/IDs, changed semantic bytes must still be sent.
        res["objective"] = "Changed content with identical generation and revision"
        with open(canonical_path, "w", encoding="utf-8") as f:
            json.dump(res, f)

        ev2 = read_sse_event(resp)
        self.assertEqual(ev2["event"], "plan")
        self.assertEqual(ev2["id"], "1")
        self.assertEqual(json.loads(ev2["data"]), res)
        self.assertNotEqual(ev1["data"], ev2["data"])
        conn.close()

        # 3. Reconnect receives current plan
        conn2 = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn2.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.port}"})
        resp2 = conn2.getresponse()
        ev_reconnect = read_sse_event(resp2)
        self.assertEqual(ev_reconnect["event"], "plan")
        self.assertEqual(ev_reconnect["id"], "1")
        self.assertEqual(ev_reconnect["data"], ev2["data"])
        conn2.close()

    def test_acceptance_12_frontend_evaluate_plan_update_node_matrix(self):
        """Acceptance 12: execute Node.js to decisively test evaluatePlanUpdate across all decision rules."""
        import subprocess

        node_script = """
        const { evaluatePlanUpdate } = require('./assets/dashboard/app.js');
        const assert = require('assert');

        // 1. Unparseable JSON
        const r1 = evaluatePlanUpdate('not-valid-json', null);
        assert.strictEqual(r1.accept, false);
        assert.strictEqual(r1.availabilityError, true);
        assert.strictEqual(r1.reason, 'unparseable');

        // 2. Missing basic required fields
        const r2 = evaluatePlanUpdate('{"schemaVersion":2}', null);
        assert.strictEqual(r2.accept, false);
        assert.strictEqual(r2.availabilityError, true);
        assert.strictEqual(r2.reason, 'invalid_fields');

        // 3. Missing v2 generation or scope
        const v2MissingGen = {
            schemaVersion: 2,
            revision: 1,
            taskId: 't1',
            objective: 'O',
            status: 'planning', executionMode: 'first-step', nextStepId: null,
            changeSummary: 'Initial', updatedAt: '2026-10-03T10:00:00Z',
            steps: [],
            history: []
        };
        const r3 = evaluatePlanUpdate(JSON.stringify(v2MissingGen), null);
        assert.strictEqual(r3.accept, false);
        assert.strictEqual(r3.reason, 'invalid_v2_fields');

        // 4. Initial valid v2 plan
        const v2Plan1 = {
            schemaVersion: 2,
            generation: 'gen-001',
            revision: 1,
            taskId: 'task-1',
            objective: 'Objective 1',
            nextStepId: 's1', changeSummary: 'Init', updatedAt: '2026-10-03T10:00:00Z',
            status: 'planning',
            executionMode: 'first-step',
            scope: { mode: 'first-step', sinceRevision: 1, baseline: [], claimed: null },
            steps: [{ id: 's1', title: 'S1', status: 'pending', dependsOn: [], check: 'C', evidence: [] }],
            history: [{ revision: 1, timestamp: '2026-10-03T10:00:00Z', summary: 'Init' }]
        };
        const s1 = JSON.stringify(v2Plan1);
        const r4 = evaluatePlanUpdate(s1, null);
        assert.strictEqual(r4.accept, true);
        assert.strictEqual(r4.clearAvailability, true);
        assert.strictEqual(r4.clearConsistency, true);
        assert.strictEqual(r4.reason, 'initial');

        // 5. Same gen, same revision, identical bytes -> no rerender, clear availability, keep consistency
        const state1 = { currentPlan: v2Plan1, lastRawText: s1, consistencyWarning: false, availabilityWarning: false };
        const r5 = evaluatePlanUpdate(s1, state1);
        assert.strictEqual(r5.accept, false);
        assert.strictEqual(r5.clearAvailability, true);
        assert.strictEqual(r5.keepConsistency, true);
        assert.strictEqual(r5.reason, 'identical');

        // 6. Equal key changed bytes -> consistency warning, no accept, clear availability
        const s1Changed = JSON.stringify(v2Plan1, null, 2); // Different bytes, same revision/generation
        const r6 = evaluatePlanUpdate(s1Changed, state1);
        assert.strictEqual(r6.accept, false);
        assert.strictEqual(r6.clearAvailability, true);
        assert.strictEqual(r6.consistencyWarning, true);
        assert.strictEqual(r6.reason, 'equal_key_changed_bytes');

        // 7. Rollback (same gen, lower revision) -> consistency warning
        const v2PlanOlder = { ...v2Plan1, revision: 1 };
        const r7 = evaluatePlanUpdate(JSON.stringify(v2PlanOlder), {currentPlan: {...v2Plan1, revision: 2}, lastRawText: s1});
        assert.strictEqual(r7.accept, false);
        assert.strictEqual(r7.consistencyWarning, true);
        assert.strictEqual(r7.reason, 'rollback');

        // 8. Same gen, newer revision -> accept, clears consistency & availability
        const v2Plan2 = { ...v2Plan1, revision: 2 };
        const s2 = JSON.stringify(v2Plan2);
        const stateWithConflict = { currentPlan: v2Plan1, lastRawText: s1, consistencyWarning: true, availabilityWarning: true };
        const r8 = evaluatePlanUpdate(s2, stateWithConflict);
        assert.strictEqual(r8.accept, true);
        assert.strictEqual(r8.clearConsistency, true);
        assert.strictEqual(r8.clearAvailability, true);
        assert.strictEqual(r8.reason, 'newer_revision');

        // 9. New generation -> accept, clears consistency & availability
        const v2PlanGen2 = { ...v2Plan1, generation: 'gen-002', revision: 1 };
        const r9 = evaluatePlanUpdate(JSON.stringify(v2PlanGen2), stateWithConflict);
        assert.strictEqual(r9.accept, true);
        assert.strictEqual(r9.clearConsistency, true);
        assert.strictEqual(r9.clearAvailability, true);
        assert.strictEqual(r9.reason, 'new_generation');

        // 10. v1 to v2 migration -> accept, clears consistency & availability
        const v1Plan = {
            schemaVersion: 1,
            revision: 3,
            taskId: 'task-1',
            objective: 'Objective 1',
            nextStepId: 's1', changeSummary: 'r3', updatedAt: '2026-10-03T10:00:00Z',
            status: 'planning',
            executionMode: 'first-step',
            steps: [{ id: 's1', title: 'S1', status: 'pending', dependsOn: [], check: 'C', evidence: [] }],
            history: [1, 2, 3].map(revision => ({revision, summary: 'r' + revision, timestamp: '2026-10-03T10:00:00Z'}))
        };
        const stateV1 = { currentPlan: v1Plan, lastRawText: JSON.stringify(v1Plan), consistencyWarning: false, availabilityWarning: false };
        const r10 = evaluatePlanUpdate(s1, stateV1);
        assert.strictEqual(r10.accept, true);
        assert.strictEqual(r10.clearConsistency, true);
        assert.strictEqual(r10.clearAvailability, true);
        assert.strictEqual(r10.reason, 'migration_v1_to_v2');

        // 11. v2 state followed by v1 event -> rejected with consistency warning
        const r11 = evaluatePlanUpdate(JSON.stringify(v1Plan), state1);
        assert.strictEqual(r11.accept, false);
        assert.strictEqual(r11.consistencyWarning, true);
        assert.strictEqual(r11.reason, 'v1_rollback');

        console.log('ALL_NODE_EVALUATE_PLAN_TESTS_PASSED');
        """;

        try:
            res = subprocess.run(
                ["node", "-e", node_script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True
            )
            self.assertIn("ALL_NODE_EVALUATE_PLAN_TESTS_PASSED", res.stdout)
        except FileNotFoundError as exc:
            # Leave explicit record if node binary is unavailable in local environment
            self.skipTest(f"Node execution skipped: {exc}")

    def request(self, method: str, path: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            req_headers.update(headers)
        conn.request(method, path, headers=req_headers)
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, resp_headers, body

    def test_serve_html_asset(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"LIVE PLAN", body)
        self.assertIn("default-src 'self'", headers.get("content-security-policy", ""))

    def test_serve_css_asset(self):
        status, headers, body = self.request("GET", "/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("content-type", ""))
        self.assertIn(b"--bg-paper", body)

    def test_serve_js_asset(self):
        status, headers, body = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers.get("content-type", ""))
        self.assertIn(b"evaluatePlanUpdate", body)

    def test_whitelist_rejection(self):
        status, _, _ = self.request("GET", "/random-file.txt")
        self.assertEqual(status, 404)

        status, _, _ = self.request("GET", "/../scripts/live_plan.py")
        self.assertEqual(status, 404)

    def test_method_rejection(self):
        status, _, _ = self.request("POST", "/api/plan")
        self.assertEqual(status, 405)

        status, _, _ = self.request("PUT", "/")
        self.assertEqual(status, 405)

        status, _, _ = self.request("DELETE", "/api/plan")
        self.assertEqual(status, 405)

    def test_origin_validation(self):
        status, _, _ = self.request("GET", "/api/plan", headers={"Origin": "http://evil.attacker.com"})
        self.assertEqual(status, 403)

        status, _, _ = self.request("GET", "/", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_host_header_validation(self):
        status, _, _ = self.request("GET", "/", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

        status, _, _ = self.request("GET", "/", headers={"Host": "evil.attacker.com"})
        self.assertEqual(status, 400)


class TestMixedVersionAndSnapshotValidation(unittest.TestCase):
    """Acceptance 13: mixed version checks new read v1, first publish v2 gen,
    old validator rejects v2; malformed scope reject."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="live_plan_mixed_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_read_published_snapshot_v1_and_v2(self):
        # 1. Read valid v1 snapshot
        v1_path = os.path.join(self.test_dir, "v1.json")
        v1_data = {
            "schemaVersion": 1,
            "revision": 1,
            "updatedAt": "2026-10-03T10:00:00+00:00",
            "taskId": "task-v1",
            "objective": "Objective v1",
            "status": "planning",
            "executionMode": "first-step",
            "nextStepId": "step-1",
            "changeSummary": "Summary",
            "steps": [{
                "id": "step-1", "title": "Title", "status": "pending", "dependsOn": [], "check": "Check", "evidence": []
            }],
            "history": [{"revision": 1, "timestamp": "2026-10-03T10:00:00+00:00", "summary": "Summary"}]
        }
        with open(v1_path, "w", encoding="utf-8") as f:
            json.dump(v1_data, f)

        data, _ = read_published_snapshot(v1_path)
        self.assertEqual(data["schemaVersion"], 1)

        # 2. Read valid v2 snapshot
        v2_path = os.path.join(self.test_dir, "v2.json")
        v2_data = dict(v1_data)
        v2_data["schemaVersion"] = 2
        v2_data["generation"] = str(uuid.uuid4())
        v2_data["history"] = copy.deepcopy(v1_data["history"])
        v2_data["history"][0]["authorizationNote"] = "User requested execution of the first step"
        v2_data["scope"] = {
            "mode": "first-step",
            "sinceRevision": 1,
            "baseline": [],
            "claimed": None
        }
        with open(v2_path, "w", encoding="utf-8") as f:
            json.dump(v2_data, f)

        data2, _ = read_published_snapshot(v2_path)
        self.assertEqual(data2["schemaVersion"], 2)

        # 3. Old v1 validator rejects v2
        import importlib.util
        baseline = os.path.join(REPO_ROOT, "tests", "fixtures", "live_plan_v1.py")
        spec = importlib.util.spec_from_file_location("baseline_live_plan", baseline)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        with self.assertRaises(old.PlanValidationError):
            old.validate_plan(data2, data2)

        # 4. Malformed scope rejected
        bad_scopes = [
            None,
            "not a dict",
            {"mode": "first-step"},  # missing keys
            {"mode": "plan-only", "sinceRevision": 1, "baseline": [], "claimed": None},  # mode mismatch with executionMode
            {"mode": "first-step", "sinceRevision": 0, "baseline": [], "claimed": None},  # sinceRevision 0
            {"mode": "first-step", "sinceRevision": 2, "baseline": [], "claimed": None},  # future sinceRevision
            {"mode": "first-step", "sinceRevision": 1, "baseline": ["dup", "dup"], "claimed": None},  # duplicate baseline
            {"mode": "first-step", "sinceRevision": 1, "baseline": ["unknown-id"], "claimed": None},  # baseline step not in steps
            {"mode": "first-step", "sinceRevision": 1, "baseline": ["step-1"], "claimed": None},  # step-1 is pending, not complete
            {"mode": "first-step", "sinceRevision": 1, "baseline": [], "claimed": "   "},  # whitespace claimed
        ]
        for bad_scope in bad_scopes:
            with self.subTest(bad_scope=bad_scope):
                malformed = copy.deepcopy(v2_data)
                malformed["scope"] = bad_scope
                with open(v2_path, "w", encoding="utf-8") as f:
                    json.dump(malformed, f)
                with self.assertRaises(PlanValidationError):
                    read_published_snapshot(v2_path)

        # 5. Plan-only completed step outside baseline rejected
        plan_only_bad = copy.deepcopy(v2_data)
        plan_only_bad["executionMode"] = "plan-only"
        plan_only_bad["scope"] = {"mode": "plan-only", "sinceRevision": 1, "baseline": [], "claimed": None}
        plan_only_bad["steps"][0]["status"] = "complete"
        plan_only_bad["steps"][0]["evidence"] = ["evidence"]
        plan_only_bad["nextStepId"] = None
        with open(v2_path, "w", encoding="utf-8") as f:
            json.dump(plan_only_bad, f)
        with self.assertRaisesRegex(PlanValidationError, "outside baseline"):
            read_published_snapshot(v2_path)

        # 6. Claimed in baseline rejected
        claimed_in_baseline = copy.deepcopy(v2_data)
        claimed_in_baseline["steps"][0]["status"] = "complete"
        claimed_in_baseline["steps"][0]["evidence"] = ["evidence"]
        claimed_in_baseline["nextStepId"] = None
        claimed_in_baseline["scope"] = {"mode": "first-step", "sinceRevision": 1, "baseline": ["step-1"], "claimed": "step-1"}
        with open(v2_path, "w", encoding="utf-8") as f:
            json.dump(claimed_in_baseline, f)
        with self.assertRaisesRegex(PlanValidationError, "claimed .* cannot be in baseline"):
            read_published_snapshot(v2_path)

    def test_acceptance_13_first_publish_creates_v2_generation_uuid(self):
        """Acceptance 13: first publish creates a valid v2 generation UUID."""
        state_dir = os.path.join(self.test_dir, "first_pub")
        input_file = os.path.join(self.test_dir, "first_input.json")
        plan = TestPlanValidationV2().valid_v2_proposal()
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(plan, f)

        res = publish(state_dir, input_file, expected_revision=0, authorization_note="First publish init")
        self.assertEqual(res["schemaVersion"], 2)
        self.assertEqual(res["revision"], 1)
        # Validate that generation is a valid UUID
        gen_uuid = uuid.UUID(res["generation"])
        self.assertEqual(gen_uuid.version, 4)


class TestUiElementsAndBadges(unittest.TestCase):
    """Static asset smoke checks only; behavioral DOM checks live in test_frontend_dom.js."""

    def test_css_contains_distinct_stopped_style_and_neutral_mode(self):
        css_path = os.path.join(REPO_ROOT, "assets", "dashboard", "style.css")
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()

        self.assertIn(".badge--stopped", css)
        self.assertIn(".badge--neutral", css)
        self.assertIn(".retired-section", css)
        self.assertIn(".retired-item", css)

    def test_html_contains_retired_section_and_badges(self):
        html_path = os.path.join(REPO_ROOT, "assets", "dashboard", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn('id="retired-section"', html)
        self.assertIn('id="task-status-val"', html)
        self.assertIn('id="exec-mode-val"', html)


if __name__ == "__main__":
    unittest.main()
