"""Tests for Task 22: Explicit legacy authority handoff and recovery import."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

from adaptive_planning.core import prepare_publication
from adaptive_planning.types import (
    Command, DocumentRef, DomainError, ExecutionContext, ExecutionContextClaims,
    JSONValue, ResourceClaim, ResourceId,
)
from tests.service_fixtures import (
    context, draft, legacy_proposal, make_service, step, workspace,
)


def active_legacy_snapshot() -> dict[str, JSONValue]:
    """Test-only helper: copy legacy_proposal(), set first step to in-progress, publish."""
    p = copy.deepcopy(legacy_proposal())
    p["status"] = "running"
    p["steps"][0]["status"] = "in-progress"
    return prepare_publication(
        p, None,
        timestamp="2026-10-06T00:00:00Z",
        generation="00000000-0000-4000-8000-000000000001",
        expected_revision=0,
        expected_generation=None,
        authorization_note="synthetic legacy authorization",
        max_file_bytes=10 * 1024 * 1024,
        max_history=100,
    )


class TestServiceImports(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = make_service(Path(self.directory.name))
        self.p = self.f.create(steps=(step("setup"),))

    def test_first_plan_import_by_claim(self):
        with TemporaryDirectory() as directory:
            fresh = make_service(Path(directory))
            self.assertEqual(fresh.service.list_plans(), ())
            self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM resources").fetchone()[0]), 0)
            self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM capabilities").fetchone()[0]), 0)
            info = fresh.service.read_service_info()
            source = prepare_publication(
                legacy_proposal(), None, timestamp="2026-10-06T00:00:00Z",
                generation="00000000-0000-4000-8000-000000000001", expected_revision=0,
                expected_generation=None, authorization_note="synthetic legacy authorization",
                max_file_bytes=10 * 1024 * 1024, max_history=100,
            )
            payload = {
                "host_id": info.host_id,
                "source_directory_id": None,
                "source_directory_claim": replace(workspace("/synthetic/legacy", 300), host_id=info.host_id),
                "original_task_id": source["taskId"],
                "original_snapshot": source,
                "provenance": DocumentRef("legacy.json", None, None, "v1", None),
                "writers_stopped": "confirmed synthetic handoff",
                "known_contexts": {},
            }
            original = fresh.command("import_plan", payload, key="first-import")
            first = fresh.service.execute(original)
            self.assertIsNotNone(first.private)
            self.assertTrue(first.private.orchestrator_token)
            self.assertEqual(len(fresh.service.list_plans()), 1)
            self.assertEqual(fresh.service.execute(original).private, first.private)
            by_id = {**payload, "source_directory_id": first.committed["source_directory_id"], "source_directory_claim": None}
            retry = fresh.service.execute(fresh.command("import_plan", by_id, key="by-id-retry"))
            self.assertEqual(retry.current_metadata.plan_id, first.current_metadata.plan_id)
            self.assertEqual(retry.private, first.private)
            self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM resources").fetchone()[0]), 1)

    def test_source_identity_not_digest_tuple_identity(self):
        source = None
        generation = "00000000-0000-4000-8000-000000000001"
        for revision in range(1, 18):
            source = prepare_publication(
                legacy_proposal(), source, timestamp="2026-10-06T00:00:00Z",
                generation=generation, expected_revision=revision - 1,
                expected_generation=None if revision == 1 else generation,
                authorization_note="synthetic legacy authorization" if revision in (1, 12) else None,
                max_file_bytes=10 * 1024 * 1024, max_history=100,
            )
        directory_id = self.f.register(workspace("/synthetic/legacy", 300), self.p)
        payload = {
            "host_id": "synthetic-host",
            "source_directory_id": directory_id,
            "source_directory_claim": None,
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v17", None),
            "writers_stopped": "confirmed synthetic handoff",
            "known_contexts": {},
        }
        original = self.f.command("import_plan", payload, key="import-one")
        first = self.f.service.execute(original)
        retry = self.f.service.execute(self.f.command("import_plan", payload, key="import-two"))
        self.assertEqual(first.current_metadata.plan_id, retry.current_metadata.plan_id)
        self.assertEqual(first.current_metadata.revision, 17)
        self.assertEqual(first.private.orchestrator_token, retry.private.orchestrator_token)
        reopened = make_service(self.f.directory, self.f.clock)
        self.assertEqual(reopened.service.execute(original).private.orchestrator_token, first.private.orchestrator_token)
        takeover = reopened.command(
            "takeover",
            {"expected_ownership_version": first.current_metadata.ownership_version, "reason": "synthetic explicit takeover"},
            receipt=first,
        )
        reopened.service.execute(takeover)
        fenced = reopened.service.execute(original)
        self.assertIsNone(fenced.private)
        self.assertEqual(fenced.committed_metadata.revision, 17)
        self.assertGreater(fenced.current_metadata.ownership_version, first.current_metadata.ownership_version)
        self.assertIsNone(reopened.service.execute(reopened.command("import_plan", payload, key="import-three")).private)
        changed = copy.deepcopy(payload)
        changed["original_snapshot"]["objective"] = "changed after handoff"
        before = reopened.service.read_events(first.current_metadata.plan_id)
        with self.assertRaises(DomainError) as error:
            reopened.service.execute(reopened.command("import_plan", changed, key="changed-source"))
        self.assertEqual(error.exception.code, "handoff_conflict")
        self.assertEqual(reopened.service.read_events(first.current_metadata.plan_id), before)

    def test_active_import_context_claim_and_attachment(self):
        for known in (True, False):
            with self.subTest(known=known), TemporaryDirectory() as directory:
                f = make_service(Path(directory))
                claims = ExecutionContextClaims("non-repository", None, workspace("/synthetic/import-live", 710), False, (), True, None)
                source = active_legacy_snapshot()
                payload = {
                    "host_id": f.service.read_service_info().host_id,
                    "source_directory_id": None,
                    "source_directory_claim": workspace("/synthetic/legacy-source", 711),
                    "original_task_id": source["taskId"],
                    "original_snapshot": source,
                    "provenance": DocumentRef("legacy.json", None, None, "v1", None),
                    "writers_stopped": "legacy publisher stopped; host worker remains unconfirmed",
                    "known_contexts": {"step-1": claims} if known else {},
                }
                imported = f.service.execute(f.command("import_plan", payload))
                other = f.create(steps=(step("a"),), creation_id="other-import-plan")
                attempt = f.service.read_plan(imported.current_metadata.plan_id).coordination.attempts[0]
                self.assertEqual(attempt.state, "stale")
                self.assertIsNone(imported.private.attempt_token)
                if not known:
                    with self.assertRaises(DomainError) as error:
                        f.assign(other)
                    self.assertEqual(error.exception.code, "held")
                    resolution = {
                        "resolution": "attach-context",
                        "reason": "identified live workspace",
                        "next_step_state": "blocked",
                        "stopped_resource_ids": (),
                        "replacement_assignee": None,
                        "replacement_context": None,
                        "attached_context": claims,
                    }
                    f.service.execute(replace(f.command("resolve_attempt", resolution, receipt=imported), attempt_id=attempt.attempt_id, attempt_version=attempt.version, definition_version=attempt.definition_version))
                self.assertFalse(f.service.read_service_info().store_hold)
                rid = f.register(claims.workspace_claim, other)
                with self.assertRaises(DomainError) as error:
                    f.service.execute(f.command("assign_step", {"step_id": "a", "assignee": "worker-a", "context": context(rid)}, receipt=other))
                self.assertEqual(error.exception.code, "resource_conflict")

    def test_import_input_validation_and_failures(self):
        info = self.f.service.read_service_info()
        source = active_legacy_snapshot()
        valid_claim = replace(workspace("/synthetic/val", 800), host_id=info.host_id)
        valid_payload = {
            "host_id": info.host_id,
            "source_directory_id": None,
            "source_directory_claim": valid_claim,
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": "confirmed stopped",
            "known_contexts": {},
        }
        # Both claim and ID present -> invalid
        both = copy.deepcopy(valid_payload)
        both["source_directory_id"] = "res-synthetic"
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", both))
        self.assertEqual(error.exception.code, "invalid")

        # Neither present -> invalid
        neither = copy.deepcopy(valid_payload)
        neither["source_directory_claim"] = None
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", neither))
        self.assertEqual(error.exception.code, "invalid")

        # Task ID mismatch
        mismatched_task = copy.deepcopy(valid_payload)
        mismatched_task["original_task_id"] = "different-task-id"
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", mismatched_task))
        self.assertEqual(error.exception.code, "invalid")

        # Non-workspace claim kind -> invalid
        repo_claim = ResourceClaim(host_id=info.host_id, kind="repository", device=1, inode=2, canonical_path="/repo", case_insensitive=False, repository_id=None, external_identity=None)
        bad_kind = copy.deepcopy(valid_payload)
        bad_kind["source_directory_claim"] = repo_claim
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", bad_kind))
        self.assertEqual(error.exception.code, "invalid")

    def test_active_import_preserves_latch_and_holds(self):
        source = None
        generation = "00000000-0000-4000-8000-000000000001"
        for revision in range(1, 18):
            proposal = copy.deepcopy(legacy_proposal())
            proposal["status"] = "running"
            proposal["steps"][0]["status"] = "in-progress"
            source = prepare_publication(
                proposal, source, timestamp="2026-10-06T00:00:00Z",
                generation=generation, expected_revision=revision - 1,
                expected_generation=None if revision == 1 else generation,
                authorization_note="synthetic legacy authorization" if revision in (1, 12) else None,
                max_file_bytes=10 * 1024 * 1024, max_history=100,
            )
        self.assertEqual(source["revision"], 17)
        self.assertEqual(source["scope"]["sinceRevision"], 12)
        directory_id = self.f.register(workspace("/synthetic/legacy-latch", 301), self.p)
        payload = {
            "host_id": "synthetic-host",
            "source_directory_id": directory_id,
            "source_directory_claim": None,
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v17", None),
            "writers_stopped": "confirmed synthetic handoff",
            "known_contexts": {},
        }
        imported = self.f.service.execute(self.f.command("import_plan", payload, key="import-latch"))
        self.assertEqual(imported.committed_metadata.revision, 17)
        plan_view = self.f.service.read_plan(imported.committed_metadata.plan_id)
        self.assertEqual(plan_view.metadata.revision, 17)
        self.assertEqual(plan_view.draft.authorization.since_revision, 12)
        claims = ExecutionContextClaims("non-repository", None, workspace("/synthetic/live-latch", 720), False, (), True, None)
        resolution = {
            "resolution": "attach-context",
            "reason": "attaching live context",
            "next_step_state": "blocked",
            "stopped_resource_ids": (),
            "replacement_assignee": None,
            "replacement_context": None,
            "attached_context": claims,
        }
        attempt = plan_view.coordination.attempts[0]
        cmd = replace(
            self.f.command("resolve_attempt", resolution, receipt=imported),
            attempt_id=attempt.attempt_id,
            attempt_version=attempt.version,
            definition_version=attempt.definition_version,
        )
        attached = self.f.service.execute(cmd)
        self.assertEqual(attached.committed_metadata.revision, 18)

    def test_corrupt_and_unmigrated_v1_rejected(self):
        info = self.f.service.read_service_info()
        source = active_legacy_snapshot()
        valid_claim = replace(workspace("/synthetic/v1-check", 810), host_id=info.host_id)
        v1_source = copy.deepcopy(source)
        v1_source["schemaVersion"] = 1
        payload = {
            "host_id": info.host_id,
            "source_directory_id": None,
            "source_directory_claim": valid_claim,
            "original_task_id": source["taskId"],
            "original_snapshot": v1_source,
            "provenance": DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": "confirmed stopped",
            "known_contexts": {},
        }
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", payload))
        self.assertEqual(error.exception.code, "invalid")

        corrupt_source = copy.deepcopy(source)
        del corrupt_source["steps"]
        corrupt_payload = {**payload, "original_snapshot": corrupt_source}
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("import_plan", corrupt_payload))
        self.assertEqual(error.exception.code, "invalid")

    def test_completed_legacy_evidence_historical_only(self):
        proposal = copy.deepcopy(legacy_proposal())
        proposal["steps"][0]["status"] = "complete"
        proposal["steps"][0]["evidence"] = ["historical completion note"]
        proposal["nextStepId"] = "step-2"
        source = prepare_publication(
            proposal, None, timestamp="2026-10-06T00:00:00Z",
            generation="00000000-0000-4000-8000-000000000001", expected_revision=0,
            expected_generation=None, authorization_note="synthetic legacy authorization",
            max_file_bytes=10 * 1024 * 1024, max_history=100,
        )
        info = self.f.service.read_service_info()
        claim = replace(workspace("/synthetic/historical", 820), host_id=info.host_id)
        payload = {
            "host_id": info.host_id,
            "source_directory_id": None,
            "source_directory_claim": claim,
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": "confirmed stopped",
            "known_contexts": {},
        }
        receipt = self.f.service.execute(self.f.command("import_plan", payload))
        plan = self.f.service.read_plan(receipt.committed_metadata.plan_id)
        self.assertEqual(len(plan.coordination.attempts), 0)
        self.assertEqual(len(plan.coordination.result_versions), 0)
        self.assertEqual(plan.legacy_snapshot["steps"][0]["status"], "complete")
        self.assertEqual(plan.legacy_snapshot["steps"][0]["evidence"], ["historical completion note"])

    def test_imported_attempt_replacement_replay(self):
        claims = ExecutionContextClaims("non-repository", None, workspace("/synthetic/rep-live", 730), False, (), True, None)
        source = active_legacy_snapshot()
        info = self.f.service.read_service_info()
        payload = {
            "host_id": info.host_id,
            "source_directory_id": None,
            "source_directory_claim": workspace("/synthetic/rep-source", 731),
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": "legacy publisher stopped",
            "known_contexts": {"step-1": claims},
        }
        imported = self.f.service.execute(self.f.command("import_plan", payload))
        plan = self.f.service.read_plan(imported.current_metadata.plan_id)
        attempt = plan.coordination.attempts[0]
        rep_context = replace(context(imported.committed["source_directory_id"]), writes=False)
        resolution = {
            "resolution": "replace",
            "reason": "replacing imported worker",
            "next_step_state": "pending",
            "stopped_resource_ids": (),
            "replacement_assignee": "new-worker",
            "replacement_context": rep_context,
            "attached_context": None,
        }
        cmd = replace(
            self.f.command("resolve_attempt", resolution, receipt=imported, key="rep-key"),
            attempt_id=attempt.attempt_id,
            attempt_version=attempt.version,
            definition_version=attempt.definition_version,
        )
        replaced = self.f.service.execute(cmd)
        self.assertIsNotNone(replaced.private)
        self.assertIsNotNone(replaced.private.attempt_token)
        replayed = self.f.service.execute(cmd)
        self.assertEqual(replayed.private.attempt_token, replaced.private.attempt_token)

    def test_imported_attempt_cancel_tokenless(self):
        source = active_legacy_snapshot()
        info = self.f.service.read_service_info()
        payload = {
            "host_id": info.host_id,
            "source_directory_id": None,
            "source_directory_claim": workspace("/synthetic/cancel-source", 740),
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": "legacy publisher stopped",
            "known_contexts": {},
        }
        imported = self.f.service.execute(self.f.command("import_plan", payload))
        plan = self.f.service.read_plan(imported.current_metadata.plan_id)
        attempt = plan.coordination.attempts[0]
        resolution = {
            "resolution": "release",
            "reason": "cancelling unknown context attempt",
            "next_step_state": "blocked",
            "stopped_resource_ids": (),
            "replacement_assignee": None,
            "replacement_context": None,
            "attached_context": None,
        }
        cmd = replace(
            self.f.command("resolve_attempt", resolution, receipt=imported),
            attempt_id=attempt.attempt_id,
            attempt_version=attempt.version,
            definition_version=attempt.definition_version,
        )
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(cmd)
        self.assertEqual(error.exception.code, "held")


if __name__ == "__main__":
    unittest.main()
