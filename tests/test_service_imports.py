"""Tests for Task 22: Explicit legacy authority handoff and recovery import."""
from __future__ import annotations
from contextlib import ExitStack

import asyncio
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest import mock
import uuid

from adaptive_planning import plans, recovery, resources
from adaptive_planning.core import prepare_publication
from adaptive_planning.types import (
    Availability, Command, DocumentRef, DomainError, ExecutionContext, ExecutionContextClaims,
    JSONValue, ResourceClaim, ResourceId, RetirementRequest,
)
from tests.service_fixtures import (
    checkpoint, context, draft, legacy_proposal, make_service, step, workspace,
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
        self.f = self.enterContext(make_service(Path(self.directory.name)))
        self.p = self.f.create(steps=(step("setup"),))

    def test_first_plan_import_by_claim(self):
        with TemporaryDirectory() as directory, ExitStack() as _directory_store_lifetimes_0:
            fresh = _directory_store_lifetimes_0.enter_context(make_service(Path(directory)))
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
        with ExitStack() as _owned_store_lifetimes:
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
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
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
            with self.subTest(known=known), TemporaryDirectory() as directory, ExitStack() as _directory_store_lifetimes_0:
                f = _directory_store_lifetimes_0.enter_context(make_service(Path(directory)))
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


def known_claim(path: str, inode: int, writes: bool = True) -> ExecutionContextClaims:
    kind = "non-repository" if writes else "read-only"
    return ExecutionContextClaims(kind, None, workspace(path, inode), False, (), writes, None)


def independent_legacy_snapshot() -> dict[str, JSONValue]:
    """Active legacy snapshot whose second step no longer depends on the in-progress first step."""
    p = copy.deepcopy(legacy_proposal())
    p["status"] = "running"
    p["steps"][0]["status"] = "in-progress"
    p["steps"][1]["dependsOn"] = []
    return prepare_publication(
        p, None, timestamp="2026-10-06T00:00:00Z", generation="00000000-0000-4000-8000-000000000001",
        expected_revision=0, expected_generation=None, authorization_note="synthetic legacy authorization",
        max_file_bytes=10 * 1024 * 1024, max_history=100)


class TestImportFixRound1(unittest.TestCase):
    """Fix round 1: composite import risks that the first nine cases did not establish."""

    TABLES = ("plans", "resources", "capabilities", "imports", "requests", "events", "holds",
              "resource_uses", "attempts", "store_hold_reasons", "steps")

    def setUp(self):
        self.reset()

    def reset(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory
        self.f = self.enterContext(make_service(Path(directory.name)))
        self.p = self.f.create(steps=(step("setup"),))

    # ----------------------------------------------------------------- helpers
    def payload(self, source=None, *, claim=None, directory_id=None, known=None, provenance=None,
                path="/synthetic/r1-source", inode=1000, writers="legacy publisher stopped"):
        source = source if source is not None else active_legacy_snapshot()
        info = self.f.service.read_service_info()
        return {
            "host_id": info.host_id,
            "source_directory_id": directory_id,
            "source_directory_claim": None if directory_id is not None else (claim or workspace(path, inode)),
            "original_task_id": source["taskId"],
            "original_snapshot": source,
            "provenance": provenance or DocumentRef("legacy.json", None, None, "v1", None),
            "writers_stopped": writers,
            "known_contexts": known or {},
        }

    def do_import(self, payload, key=None):
        return self.f.service.execute(self.f.command("import_plan", payload, key=key))

    def rows(self, sql, *params):
        return self.f.store.read(lambda db: tuple(db.execute(sql, params)))

    def counts(self):
        return self.f.store.read(lambda db: {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in self.TABLES})

    def attempt(self, receipt):
        return self.f.service.read_plan(receipt.current_metadata.plan_id).coordination.attempts[0]

    def holds(self, plan_id):
        return [item.value for item in self.f.service.read_coordination(plan_id).items if item.kind == "hold"]

    def import_known(self, path, inode, writes=True, **kwargs):
        receipt = self.do_import(self.payload(
            path=path, inode=inode, known={"step-1": known_claim(path + "-live", inode + 1, writes)}, **kwargs))
        return receipt, self.attempt(receipt)

    def resolve(self, receipt, attempt, resolution, next_state, *, stopped=(), attached=None,
                replacement=None, assignee=None, key=None):
        body = {"resolution": resolution, "reason": "r1 regression", "next_step_state": next_state,
                "stopped_resource_ids": tuple(stopped), "replacement_assignee": assignee,
                "replacement_context": replacement, "attached_context": attached}
        return replace(self.f.command("resolve_attempt", body, receipt=receipt, key=key),
                       attempt_id=attempt.attempt_id, attempt_version=attempt.version,
                       definition_version=attempt.definition_version)

    def reconciled_pair(self, name="r1-alias", inode=1100):
        left = replace(workspace(f"/synthetic/{name}-left", inode), device=None, inode=None)
        right = replace(left, canonical_path=f"/synthetic/{name}-right")
        a, b = self.f.register(left, self.p), self.f.register(right, self.p)
        self.f.service.execute(self.f.command("reconcile_resources", {
            "resource_ids": (a, b), "canonical_claim": workspace(f"/synthetic/{name}-canonical", inode),
            "reason": "host-attested physical equivalence"}, receipt=self.p))
        return min(a, b), max(a, b)

    def origin(self):
        events = self.rows("SELECT payload FROM events WHERE kind='import_plan'")
        self.assertEqual(len(events), 1)
        return json.loads(events[0][0])

    # -------------------------------------------------- P1 canonical source identity
    def test_redirected_existing_source_commits_canonical_id_and_keeps_raw_provenance(self):
        survivor, retired = self.reconciled_pair()
        source = active_legacy_snapshot()
        first = self.do_import(self.payload(source, directory_id=retired), key="r1-redirect")
        self.assertEqual(first.committed["source_directory_id"], survivor)
        self.assertEqual(self.rows("SELECT source_directory_id FROM imports"), ((retired,),))
        host = self.f.service.read_service_info().host_id
        expected = "legacy:" + hashlib.sha256(plans.canonical_bytes(
            [host, retired, source["taskId"], source.get("generation"), source["scope"]["sinceRevision"]])).hexdigest()
        plan = self.f.service.read_plan(first.current_metadata.plan_id)
        self.assertEqual(plan.draft.authorization.interval, expected)
        self.assertEqual(self.origin()["source_directory_id"], retired)
        for ident, key in ((survivor, "r1-by-survivor"), (retired, "r1-by-retired")):
            retry = self.do_import(self.payload(source, directory_id=ident), key=key)
            self.assertEqual(retry.current_metadata.plan_id, first.current_metadata.plan_id)
            self.assertEqual(retry.committed, first.committed)
        self.assertEqual(self.rows("SELECT count(*) FROM imports"), ((1,),))

    def test_source_rejections_leave_no_residue(self):
        repo = self.f.register(replace(workspace("/synthetic/r1-repo", 1110), kind="repository"), self.p)
        source = active_legacy_snapshot()
        cases = {
            "repository-kind": self.payload(source, directory_id=repo),
            "unknown-id": self.payload(source, directory_id="no-such-resource"),
            "irrelevant-known-key-with-new-claim": self.payload(
                source, path="/synthetic/r1-new-claim", inode=1112,
                known={"step-2": known_claim("/synthetic/r1-irrelevant", 1111)}),
            "foreign-host-claim": self.payload(source, claim=replace(workspace("/synthetic/r1-foreign", 1113), host_id="other-host")),
            "known-claim-conflicting-parent": self.payload(source, path="/synthetic/r1-parent", inode=1114, known={
                "step-1": ExecutionContextClaims("repository", replace(workspace("/synthetic/r1-p-repo", 1115), kind="repository"),
                                                  workspace("/synthetic/r1-p-ws", 1116, repository_id="not-the-parent"),
                                                  False, (), True, None)}),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                before = self.counts()
                with self.assertRaises(DomainError):
                    self.do_import(payload)
                self.assertEqual(self.counts(), before)

    def test_multiple_collapsed_mappings_are_ambiguous_even_for_one_plan(self):
        survivor, retired = self.reconciled_pair("r1-collapse", 1120)
        source = active_legacy_snapshot()
        self.do_import(self.payload(source, directory_id=retired), key="r1-collapse-first")

        def clone(db):
            row = db.execute("SELECT host_id, original_task_id, plan_id, content_digest, created_at FROM imports").fetchone()
            db.execute("INSERT INTO imports (import_id, host_id, source_directory_id, original_task_id, plan_id, "
                       "content_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ("clone-import", row[0], survivor, row[1], row[2], row[3], row[4]))
        self.f.store.write(clone)
        before = self.counts()
        with self.assertRaises(DomainError) as error:
            self.do_import(self.payload(source, directory_id=survivor), key="r1-collapse-second")
        self.assertEqual(error.exception.code, "handoff_conflict")
        self.assertEqual(self.counts(), before)

    # ------------------------------------------- P3/P6 obligations, holds, namespace
    def test_each_imported_obligation_has_source_owned_stale_hold(self):
        for known in (True, False):
            with self.subTest(known=known):
                self.reset()
                if known:
                    receipt, attempt = self.import_known("/synthetic/r1-hold-known", 1200)
                else:
                    receipt = self.do_import(self.payload(path="/synthetic/r1-hold-unknown", inode=1210))
                    attempt = self.attempt(receipt)
                kinds = sorted(hold.kind for hold in self.holds(receipt.current_metadata.plan_id))
                self.assertEqual(kinds, ["stale"] if known else ["identity", "stale"])
                stale = next(hold for hold in self.holds(receipt.current_metadata.plan_id) if hold.kind == "stale")
                self.assertEqual(stale.step_id, "step-1")
                self.assertEqual(self.f.service.read_service_info().store_hold, not known)
                self.assertEqual(attempt.last_activity, "")
                self.assertEqual(self.rows("SELECT started_at FROM attempts"), ((None,),))

    def test_known_context_namespace_is_strict_and_unknown_is_not_invented(self):
        with ExitStack() as _owned_store_lifetimes:
            receipt, attempt = self.import_known("/synthetic/r1-ns-known", 1220)
            plan_id = receipt.current_metadata.plan_id
            generation = receipt.current_metadata.generation
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            for fixture in (self.f, reopened):
                namespace, owned = fixture.store.read(lambda db: resources.execution_uses(db, plan_id, attempt.attempt_id))
                self.assertIsNone(namespace["implementation"]["writer"])
                self.assertEqual(namespace["groups"], {})
                self.assertEqual(len(namespace["implementation"]["members"]), 1)
                self.assertEqual(namespace["source"]["origin_epoch"], self.f.epoch)
                self.assertEqual(namespace["source"]["origin_generation"], generation)
            readonly, ro_attempt = self.import_known("/synthetic/r1-ns-readonly", 1230, writes=False)
            namespace, _ = self.f.store.read(lambda db: resources.execution_uses(
                db, readonly.current_metadata.plan_id, ro_attempt.attempt_id))
            self.assertEqual(namespace["implementation"]["members"], [])
            unknown = self.do_import(self.payload(path="/synthetic/r1-ns-unknown", inode=1240))
            unknown_attempt = self.attempt(unknown)
            with self.assertRaises(DomainError):
                self.f.store.read(lambda db: resources.execution_uses(db, unknown.current_metadata.plan_id, unknown_attempt.attempt_id))
            self.assertEqual(self.rows("SELECT json_extract(payload,'$.execution_uses') FROM attempts WHERE attempt_id=?",
                                       unknown_attempt.attempt_id), ((None,),))
            self.assertEqual(self.rows("SELECT count(*) FROM resource_uses WHERE attempt_id=?", unknown_attempt.attempt_id), ((0,),))

    def test_import_time_cross_plan_conflict_rolls_back_every_record(self):
        rid = self.f.register(workspace("/synthetic/r1-conflict-live", 1250), self.p)
        self.f.service.execute(self.f.command("assign_step", {
            "step_id": "setup", "assignee": "worker-b", "context": context(rid)}, receipt=self.p))
        before = self.counts()
        with self.assertRaises(DomainError) as error:
            self.do_import(self.payload(path="/synthetic/r1-conflict-source", inode=1251,
                                        known={"step-1": known_claim("/synthetic/r1-conflict-live", 1250)}))
        self.assertEqual(error.exception.code, "resource_conflict")
        self.assertEqual(self.counts(), before)

    # --------------------------------------------------- P4 positive imported proof
    def test_tampered_imported_origin_never_gains_tokenless_authority(self):
        def duplicate_origin(db):
            db.execute("INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) "
                       "SELECT plan_id, plan_revision, kind, actor, published_at, payload FROM events WHERE kind='import_plan'")
        tampers = {
            "assignee-suffix": lambda db: db.execute("UPDATE attempts SET assignee = assignee || '-x'"),
            "foreign-import-id": lambda db: db.execute(
                "UPDATE attempts SET payload = json_set(payload, '$.import_source.import_id', 'foreign')"),
            "origin-event-deleted": lambda db: db.execute("DELETE FROM events WHERE kind='import_plan'"),
            "origin-event-duplicated": duplicate_origin,
            "source-step-not-in-progress": lambda db: db.execute(
                "UPDATE events SET payload = json_set(payload, '$.original_snapshot.steps[0].status', 'pending') WHERE kind='import_plan'"),
            "constructor-attempt-swapped": lambda db: db.execute(
                "UPDATE events SET payload = json_set(payload, '$.constructor_attempts.\"step-1\"', 'other') WHERE kind='import_plan'"),
            "origin-epoch-relabelled": lambda db: db.execute(
                "UPDATE attempts SET payload = json_set(payload, '$.epoch', 'other-epoch')"),
            "native-capability-bound": lambda db: db.execute(
                "UPDATE attempts SET assignee = 'worker-native'"),
        }
        for name, tamper in tampers.items():
            with self.subTest(name):
                self.reset()
                receipt, attempt = self.import_known("/synthetic/r1-tamper", 1300)
                self.f.store.write(tamper)
                with self.assertRaises(DomainError):
                    self.f.service.execute(self.resolve(receipt, attempt, "release", "blocked"))
                self.assertEqual(self.rows("SELECT state, version FROM attempts"), (("stale", 1),))
                self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((0,),))

    def test_origin_event_carries_version_context_and_constructor_lineage(self):
        known, known_attempt = self.import_known("/synthetic/r1-origin-known", 1310)
        origin = self.origin()
        self.assertEqual(origin["v"], 1)
        self.assertEqual(origin["constructor_attempts"], {"step-1": known_attempt.attempt_id})
        self.assertEqual(origin["initial_contexts"]["step-1"]["writes"], True)
        self.assertEqual(origin["plan_generation"], known.current_metadata.generation)
        self.assertEqual(origin["provenance"]["path"], "legacy.json")
        self.assertFalse(origin["authorization_note_missing"])
        self.reset()
        unknown = self.do_import(self.payload(path="/synthetic/r1-origin-unknown", inode=1320))
        self.assertIsNone(self.origin()["initial_contexts"]["step-1"])
        self.assertEqual(self.origin()["constructor_attempts"], {"step-1": self.attempt(unknown).attempt_id})

    def test_definition_version_hashes_the_complete_original_step(self):
        source = active_legacy_snapshot()
        receipt = self.do_import(self.payload(source))
        host = self.f.service.read_service_info().host_id
        raw_dir = self.rows("SELECT source_directory_id FROM imports")[0][0]
        stored = dict(self.rows("SELECT step_id, definition_version FROM steps WHERE plan_id=?", receipt.current_metadata.plan_id))
        for original in source["steps"]:
            expected = "legacy:" + hashlib.sha256(plans.canonical_bytes(
                [host, raw_dir, source["taskId"], source.get("generation"), source["revision"], original])).hexdigest()
            self.assertEqual(stored[original["id"]], expected)

    def test_missing_authorization_note_is_explicit_and_not_fabricated(self):
        # The validator permits a plan-only revision-1 opening that retains only a recoveryNote.
        proposal = copy.deepcopy(legacy_proposal())
        proposal["executionMode"] = "plan-only"
        source = prepare_publication(
            proposal, None, timestamp="2026-10-06T00:00:00Z", generation="00000000-0000-4000-8000-000000000001",
            expected_revision=0, expected_generation=None, authorization_note="synthetic legacy authorization",
            max_file_bytes=10 * 1024 * 1024, max_history=100)
        source = copy.deepcopy(source)
        entry = source["history"][0]
        entry.pop("authorizationNote")
        entry["recoveryNote"] = "recovery-only opening note"
        receipt = self.do_import(self.payload(source))
        origin = self.origin()
        self.assertTrue(origin["authorization_note_missing"])
        self.assertEqual(origin["original_snapshot"], json.loads(json.dumps(source)))
        self.assertEqual(origin["original_snapshot"]["history"][0]["recoveryNote"], "recovery-only opening note")
        plan = self.f.service.read_plan(receipt.current_metadata.plan_id)
        self.assertEqual(plan.draft.authorization.note, "")
        self.assertEqual(plan.draft.authorization.since_revision, 1)
        self.assertEqual(receipt.committed_metadata.revision, 1)

    # -------------------------------------------- P5 receipt linkage, private persistence
    def test_corrupt_origin_receipt_fails_closed_without_residue(self):
        def duplicate_origin(db):
            db.execute("INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) "
                       "SELECT plan_id, plan_revision, kind, actor, published_at, payload FROM events WHERE kind='import_plan'")
        cases = {
            "origin-payload-not-json": ("UPDATE events SET payload='not-json' WHERE kind='import_plan'", ()),
            "origin-payload-array": ("UPDATE events SET payload='[]' WHERE kind='import_plan'", ()),
            "origin-import-id-changed": ("UPDATE events SET payload=json_set(payload,'$.import_id','other') WHERE kind='import_plan'", ()),
            "origin-digest-changed": ("UPDATE events SET payload=json_set(payload,'$.content_digest','0') WHERE kind='import_plan'", ()),
            "origin-version-changed": ("UPDATE events SET payload=json_set(payload,'$.v',2) WHERE kind='import_plan'", ()),
            "request-payload-malformed": ("UPDATE requests SET response_payload='{}' WHERE key='r1-origin'", ()),
            "request-digest-changed": ("UPDATE requests SET command_digest=? WHERE key='r1-origin'", ("0" * 64,)),
        }
        for name, (sql, params) in {**cases, "origin-event-duplicated": (None, None)}.items():
            with self.subTest(name):
                self.reset()
                self.do_import(self.payload(), key="r1-origin")
                if sql is None:
                    self.f.store.write(duplicate_origin)
                else:
                    self.f.store.write(lambda db: db.execute(sql, params))
                before = self.counts()
                with self.assertRaises(DomainError):
                    self.do_import(self.payload(), key=f"r1-retry-{name}")
                self.assertEqual(self.counts(), before)

    def test_only_original_request_row_keeps_private_grant(self):
        first = self.do_import(self.payload(), key="r1-private-one")
        token = first.private.orchestrator_token
        second = self.do_import(self.payload(), key="r1-private-two")
        self.assertEqual(second.private.orchestrator_token, token)
        holders = self.rows("SELECT key FROM requests WHERE response_payload LIKE ?", "%" + token + "%")
        self.assertEqual(holders, (("r1-private-one",),))
        again = self.do_import(self.payload(), key="r1-private-two")
        self.assertTrue(again.replayed)
        self.assertEqual(again.private.orchestrator_token, token)
        self.assertEqual(again.committed, first.committed)
        self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": first.current_metadata.ownership_version, "reason": "r1 takeover"}, receipt=first))
        for key in ("r1-private-one", "r1-private-two"):
            fenced = self.do_import(self.payload(), key=key)
            self.assertIsNone(fenced.private)
            self.assertEqual(fenced.committed_metadata.revision, first.committed_metadata.revision)

    def test_new_key_retry_ignores_changed_auxiliary_instructions(self):
        first = self.do_import(self.payload(), key="r1-aux-one")
        before = self.counts()
        changed = self.payload(
            directory_id=first.committed["source_directory_id"],
            provenance=DocumentRef("other.json", "branch", "commit", "v9", "task"),
            writers="a different handoff statement",
            known={"step-1": known_claim("/synthetic/r1-aux-live", 1400)})
        retry = self.do_import(changed, key="r1-aux-two")
        after = self.counts()
        self.assertEqual({k: v for k, v in after.items() if k != "requests"},
                         {k: v for k, v in before.items() if k != "requests"})
        self.assertEqual(after["requests"], before["requests"] + 1)
        self.assertEqual(retry.committed, first.committed)
        self.assertEqual(retry.private.orchestrator_token, first.private.orchestrator_token)
        self.assertEqual(self.origin()["provenance"]["path"], "legacy.json")
        self.assertEqual(self.origin()["writers_stopped"], "legacy publisher stopped")

    def test_concurrent_duplicate_bootstrap_commits_one_import(self):
        with ExitStack() as _owned_store_lifetimes:
            other = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            source = active_legacy_snapshot()
            barrier = threading.Barrier(2)
            results = {}

            def run(name, fixture, key):
                try:
                    command = fixture.command("import_plan", self.payload(source), key=key)
                    barrier.wait(timeout=20)
                    results[name] = fixture.service.execute(command)
                except BaseException as error:  # reported by the assertions below
                    results[name] = error
            threads = [threading.Thread(target=run, args=("a", self.f, "r1-race-a")),
                       threading.Thread(target=run, args=("b", other, "r1-race-b"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            for outcome in results.values():
                self.assertNotIsInstance(outcome, BaseException, repr(outcome))
            self.assertEqual(results["a"].current_metadata.plan_id, results["b"].current_metadata.plan_id)
            self.assertEqual(results["a"].private.orchestrator_token, results["b"].private.orchestrator_token)
            self.assertEqual(self.rows("SELECT count(*) FROM imports"), ((1,),))
            self.assertEqual(self.rows("SELECT count(*) FROM events WHERE kind='import_plan'"), ((1,),))
            imported = results["a"].current_metadata.plan_id
            self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE plan_id=? AND role='orchestrator'", imported), ((1,),))

    # ------------------------------------------------------- P7 attachment lifecycle
    def test_attach_replay_counts_one_decision_and_never_overwrites(self):
        receipt = self.do_import(self.payload())
        attempt = self.attempt(receipt)
        command = self.resolve(receipt, attempt, "attach-context", "blocked",
                               attached=known_claim("/synthetic/r1-attach-live", 1500), key="r1-attach")
        first = self.f.service.execute(command)
        again = self.f.service.execute(command)
        self.assertTrue(again.replayed)
        self.assertEqual(again.committed, first.committed)
        self.assertEqual(first.committed_metadata.revision, receipt.committed_metadata.revision + 1)
        self.assertEqual(self.rows("SELECT state, version, started_at FROM attempts"), (("stale", 2, None),))
        self.assertEqual(self.attempt(receipt).last_activity, "")
        self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((0,),))
        with self.assertRaises(DomainError):
            self.f.service.execute(self.resolve(receipt, self.attempt(receipt), "attach-context", "blocked",
                                                attached=known_claim("/synthetic/r1-attach-other", 1501)))
        self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": receipt.current_metadata.ownership_version, "reason": "r1 takeover"}, receipt=receipt))
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(command)
        self.assertEqual(error.exception.code, "fenced")

    def test_attach_conflict_rolls_back_atomically(self):
        rid = self.f.register(workspace("/synthetic/r1-attach-busy", 1510), self.p)
        self.f.service.execute(self.f.command("assign_step", {
            "step_id": "setup", "assignee": "worker-b", "context": context(rid)}, receipt=self.p))
        receipt = self.do_import(self.payload())
        attempt = self.attempt(receipt)
        before = self.counts()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.resolve(receipt, attempt, "attach-context", "blocked",
                                                attached=known_claim("/synthetic/r1-attach-busy", 1510)))
        self.assertEqual(error.exception.code, "resource_conflict")
        self.assertEqual(self.counts(), before)
        self.assertTrue(self.f.service.read_service_info().store_hold)

    def test_attachment_clears_only_its_own_identity_reason(self):
        first = self.do_import(self.payload(path="/synthetic/r1-sel-a", inode=1520), key="r1-sel-a")
        second = self.do_import(self.payload(path="/synthetic/r1-sel-b", inode=1530), key="r1-sel-b")
        self.assertEqual(self.rows("SELECT count(*) FROM store_hold_reasons WHERE resolved=0"), ((2,),))
        self.f.service.execute(self.resolve(first, self.attempt(first), "attach-context", "blocked",
                                            attached=known_claim("/synthetic/r1-sel-a-live", 1521)))
        self.assertEqual(self.rows("SELECT count(*) FROM store_hold_reasons WHERE resolved=0"), ((1,),))
        self.assertTrue(self.f.service.read_service_info().store_hold)
        self.assertEqual(sorted(h.kind for h in self.holds(first.current_metadata.plan_id)), ["stale"])
        self.assertEqual(sorted(h.kind for h in self.holds(second.current_metadata.plan_id)), ["identity", "stale"])
        self.f.service.execute(self.resolve(second, self.attempt(second), "attach-context", "blocked",
                                            attached=known_claim("/synthetic/r1-sel-b-live", 1531)))
        self.assertFalse(self.f.service.read_service_info().store_hold)

    # ------------------------------------------- P10 real imported terminal integration
    def test_known_imported_terminal_resolutions_stay_tokenless_and_blocking(self):
        for name, resolution, next_state in (("release", "release", "blocked"), ("reject", "reject", "blocked"),
                                             ("retire", "release", "retired")):
            with self.subTest(name):
                self.reset()
                receipt, attempt = self.import_known(
                    f"/synthetic/r1-term-{name}", 1600,
                    source=independent_legacy_snapshot() if name == "retire" else None)
                plan_id = receipt.current_metadata.plan_id
                self.assertEqual([h.kind for h in self.holds(plan_id)], ["stale"])
                self.f.service.execute(self.resolve(receipt, attempt, resolution, next_state))
                self.assertEqual(self.rows("SELECT state, version FROM attempts"),
                                 (("rejected" if resolution == "reject" else "released", 2),))
                self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((0,),))
                self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses WHERE plan_id=?", plan_id), (("unresolved",),))
                self.assertEqual(self.rows("SELECT state FROM steps WHERE plan_id=? AND step_id='step-1'", plan_id), ((next_state,),))
                self.assertEqual(self.holds(plan_id), [])
                live = self.f.register(workspace(f"/synthetic/r1-term-{name}-live", 1601), self.p)
                with self.assertRaises(DomainError) as error:
                    self.f.service.execute(self.f.command("assign_step", {
                        "step_id": "setup", "assignee": "worker-c", "context": context(live)}, receipt=self.p))
                self.assertEqual(error.exception.code, "resource_conflict")

    def test_physical_release_requires_complete_stopped_proof(self):
        receipt, attempt = self.import_known("/synthetic/r1-stop", 1700)
        wid = self.rows("SELECT resource_id FROM resource_uses")[0][0]
        before = self.counts()
        with self.assertRaises(DomainError):
            self.f.service.execute(self.resolve(receipt, attempt, "release", "blocked", stopped=("no-such-resource",)))
        self.assertEqual(self.counts(), before)
        self.f.service.execute(self.resolve(receipt, attempt, "release", "blocked", stopped=(wid,)))
        self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses"), (("released",),))

    def test_replacement_conflict_rolls_back_and_stopped_proof_enables_it(self):
        receipt, attempt = self.import_known("/synthetic/r1-rep", 1800)
        wid = self.rows("SELECT resource_id FROM resource_uses")[0][0]
        before = self.counts()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker",
                                                replacement=context(wid)))
        self.assertEqual(error.exception.code, "resource_conflict")
        self.assertEqual(self.counts(), before)
        replaced = self.f.service.execute(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker",
                                                       replacement=context(wid), stopped=(wid,), key="r1-rep-key"))
        self.assertIsNotNone(replaced.private.attempt_token)
        self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((1,),))
        self.assertEqual(self.rows("SELECT state FROM attempts WHERE attempt_id=?", attempt.attempt_id), (("released",),))
        retry = self.do_import(self.payload(path="/synthetic/r1-rep", inode=1800), key="r1-rep-retry")
        self.assertEqual(retry.private.orchestrator_token, receipt.private.orchestrator_token)

    # ------------------------------------------------- historical completion is strict
    def test_completed_legacy_step_does_not_fabricate_accepted_output(self):
        proposal = copy.deepcopy(legacy_proposal())
        proposal["executionMode"] = "complete-task"
        proposal["steps"][0]["status"] = "complete"
        proposal["steps"][0]["evidence"] = ["historical completion note"]
        proposal["nextStepId"] = "step-2"
        source = prepare_publication(
            proposal, None, timestamp="2026-10-06T00:00:00Z", generation="00000000-0000-4000-8000-000000000001",
            expected_revision=0, expected_generation=None, authorization_note="synthetic legacy authorization",
            max_file_bytes=10 * 1024 * 1024, max_history=100)
        receipt = self.do_import(self.payload(source))
        plan_id = receipt.current_metadata.plan_id
        self.assertEqual(self.rows("SELECT count(*) FROM attempts WHERE plan_id=?", plan_id), ((0,),))
        self.assertEqual(self.rows("SELECT count(*) FROM result_versions WHERE plan_id=?", plan_id), ((0,),))
        attempt = self.f.assign(receipt, step_id="step-2")
        before = self.state()
        guard = self.refused(self.f.command("start_step", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "started"},
            receipt=receipt, attempt=attempt), before)
        self.assertEqual(guard.code, "invalid")
        self.assertEqual(self.rows("SELECT state FROM attempts WHERE plan_id=?", plan_id), (("assigned",),))
        self.assertEqual(self.rows("SELECT count(*) FROM result_versions WHERE plan_id=?", plan_id), ((0,),))

    # ------------------------------------------------------------ P2 typed MCP route
    def test_typed_mcp_import_route_and_wire_contract(self):
        from adaptive_planning.transport import ImportPlanInput, build_mcp
        server = build_mcp(self.f.service)
        raw = plans.to_jsonable(self.f.command("import_plan", self.payload(
            known={"step-1": known_claim("/synthetic/r1-mcp-live", 1900)}), key="r1-mcp"))

        async def call(arguments):
            try:
                result = await server.call_tool("import_plan", {"command": arguments})
            except Exception:
                return None
            return None if result.is_error else result

        before = self.counts()
        mutations = {
            "missing provenance": lambda p: p.pop("provenance"),
            "null provenance": lambda p: p.__setitem__("provenance", None),
            "missing source id": lambda p: p.pop("source_directory_id"),
            "missing source claim": lambda p: p.pop("source_directory_claim"),
            "missing known contexts": lambda p: p.pop("known_contexts"),
            "unknown nested field": lambda p: p["known_contexts"]["step-1"].__setitem__("surprise", 1),
            "malformed nested claim": lambda p: p["known_contexts"]["step-1"]["workspace_claim"].pop("host_id"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                arguments = copy.deepcopy(raw)
                mutate(arguments["payload"])
                self.assertIsNone(asyncio.run(call(arguments)))
                self.assertEqual(self.counts(), before)
        model = ImportPlanInput.model_validate(copy.deepcopy(raw))
        self.assertNotIsInstance(model.payload.provenance, dict)
        self.assertNotIn("/synthetic/r1-mcp-live", repr(model))
        result = asyncio.run(call(raw))
        self.assertIsNotNone(result)
        self.assertTrue(result.structured_content["private"]["orchestrator_token"])
        self.assertEqual(self.counts()["imports"], before["imports"] + 1)
        nullable = copy.deepcopy(raw)
        nullable["key"] = "r1-mcp-nullable"
        nullable["payload"]["provenance"].update(branch=None, commit=None, task_id=None)
        self.assertIsNotNone(asyncio.run(call(nullable)))

    # ------------------------------------ P10 revise_plan(retire_steps) integration
    def retire_command(self, receipt):
        current = self.f.service.read_plan(receipt.current_metadata.plan_id).draft
        return self.f.command("revise_plan", {
            "draft": current, "reason": "r1 retire imported step", "authorization_change": None,
            "retire_steps": (RetirementRequest("step-1", "r1 retire imported step"),)}, receipt=receipt)

    def test_revise_plan_retires_known_imported_step_tokenless_and_clears_stale_hold(self):
        receipt, attempt = self.import_known("/synthetic/r1-revise-known", 1800,
                                             source=independent_legacy_snapshot())
        plan_id = receipt.current_metadata.plan_id
        self.assertEqual([h.kind for h in self.holds(plan_id)], ["stale"])
        self.f.service.execute(self.retire_command(receipt))
        self.assertEqual(self.rows("SELECT state, version FROM attempts"), (("released", 2),))
        self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((0,),))
        self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses WHERE plan_id=?", plan_id), (("unresolved",),))
        self.assertEqual(self.rows("SELECT state FROM steps WHERE plan_id=? AND step_id='step-1'", plan_id), (("retired",),))
        self.assertEqual(self.holds(plan_id), [])
        live = self.f.register(workspace("/synthetic/r1-revise-known-live", 1801), self.p)
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command("assign_step", {
                "step_id": "setup", "assignee": "worker-c", "context": context(live)}, receipt=self.p))
        self.assertEqual(error.exception.code, "resource_conflict")

    def test_revise_plan_cannot_retire_unknown_context_imported_step(self):
        receipt = self.do_import(self.payload(source=independent_legacy_snapshot()))
        before = self.counts()
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.retire_command(receipt))
        self.assertEqual(error.exception.code, "held")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows("SELECT state, version FROM attempts"), (("stale", 1),))
        self.assertTrue(self.f.service.read_service_info().store_hold)

    def test_retiring_imported_step_with_live_dependent_is_rejected_without_residue(self):
        receipt, _ = self.import_known("/synthetic/r1-revise-dependent", 1810)
        before = self.counts()
        with self.assertRaises(DomainError):
            self.f.service.execute(self.retire_command(receipt))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows("SELECT state, version FROM attempts"), (("stale", 1),))


    # ================================================================ R2 helpers
    STATE_TABLES = ("plans", "steps", "attempts", "resource_uses", "events", "holds", "store_hold_reasons",
                    "capabilities", "imports", "resources", "requests", "result_versions")

    def state(self):
        """Per-table digest of every existing row; failures print digests only, never grants."""
        return self.f.store.read(lambda db: {
            table: hashlib.sha256(repr(tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid"))).encode()).hexdigest()
            for table in self.STATE_TABLES})

    def rewrite_json(self, table, column, where, params, mutate):
        def run(db):
            raw = db.execute(f"SELECT {column} FROM {table} WHERE {where}", params).fetchone()[0]
            value = json.loads(raw)
            mutate(value)
            db.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (json.dumps(value, sort_keys=True), *params))
        self.f.store.write(run)

    def is_imported(self, plan_id, attempt_id):
        return self.f.store.read(lambda db: recovery.is_imported_attempt(db, plan_id, attempt_id))

    def refused(self, command, before=None):
        before = self.state() if before is None else before
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(command)
        self.assertEqual(self.state(), before)
        return error.exception

    def snapshot17(self):
        source = None
        generation = "00000000-0000-4000-8000-000000000001"
        for revision in range(1, 18):
            proposal = copy.deepcopy(legacy_proposal())
            proposal["status"] = "running"
            proposal["steps"][0]["status"] = "in-progress"
            source = prepare_publication(
                proposal, source, timestamp="2026-10-06T00:00:00Z", generation=generation,
                expected_revision=revision - 1, expected_generation=None if revision == 1 else generation,
                authorization_note="synthetic legacy authorization" if revision in (1, 12) else None,
                max_file_bytes=10 * 1024 * 1024, max_history=100)
        return source

    def cohort(self, tag, base):
        repository = replace(workspace(f"/synthetic/r2-{tag}-repo", base), kind="repository")
        member = workspace(f"/synthetic/r2-{tag}-ws", base + 1)
        external = ResourceClaim("synthetic-host", "external", None, None, None, False, None, f"r2-{tag}-ext")
        return ExecutionContextClaims("repository", repository, member, True, (external,), True, None)

    def import_cohort(self, tag, base):
        receipt = self.do_import(self.payload(path=f"/synthetic/r2-{tag}-src", inode=base + 9,
                                              known={"step-1": self.cohort(tag, base)}), key=f"r2-{tag}")
        return receipt, self.attempt(receipt)

    def live_accept(self, receipt, target, version="producer-v1"):
        assigned = self.f.assign(receipt, target)
        active = self.f.start(receipt, assigned)
        value = checkpoint(artifact_version=version)
        self.f.submit(receipt, active, value)
        return self.f.accept(receipt, active, value), value

    # ====================================== R2 item 6: bootstrap lifecycle and bounds
    def test_import_bootstrap_lost_response_current_grant_only(self):
        with ExitStack() as _owned_store_lifetimes:
            payload = self.payload()
            command = self.f.command("import_plan", payload, key="r2-lost")
            first = self.f.service.execute(command)
            token = first.private.orchestrator_token
            self.assertEqual(self.rows("SELECT key FROM requests WHERE response_payload LIKE ?", "%" + token + "%"), (("r2-lost",),))
            error = self.refused(replace(command, epoch="r2-old-epoch"))
            self.assertEqual(error.code, "stale_epoch")
            again = self.f.service.execute(command)
            self.assertTrue(again.replayed)
            self.assertEqual(again.private.orchestrator_token, token)
            self.assertEqual(again.current_metadata.plan_id, first.current_metadata.plan_id)
            fresh_key = self.do_import(payload, key="r2-lost-new")
            self.assertFalse(fresh_key.replayed)
            self.assertEqual(fresh_key.private.orchestrator_token, token)
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            self.assertEqual(reopened.service.execute(command).private.orchestrator_token, token)
            self.assertEqual(self.rows("SELECT count(*) FROM imports"), ((1,),))
            self.assertEqual(self.rows("SELECT key FROM requests WHERE response_payload LIKE ?", "%" + token + "%"), (("r2-lost",),))
            reopened.service.execute(reopened.command("takeover", {
                "expected_ownership_version": first.current_metadata.ownership_version,
                "reason": "r2 explicit takeover"}, receipt=first))
            self.assertIsNone(reopened.service.execute(command).private)
            self.assertIsNone(reopened.service.execute(reopened.command("import_plan", payload, key="r2-lost-third")).private)

    def test_import_origin_and_page_bounds_fail_closed_with_exact_rollback(self):
        source = active_legacy_snapshot()
        size = len(plans.canonical_bytes(source))
        before = self.state()
        with mock.patch.object(plans, "MAX_FILE_BYTES", size + 16):
            with self.assertRaises(DomainError) as error:
                self.do_import(self.payload(source), key="r2-origin-bound")
        self.assertEqual(error.exception.code, "limit_exceeded")
        self.assertEqual(self.state(), before)
        with mock.patch.object(plans, "MAX_PAGE_BYTES", 64):
            with self.assertRaises(DomainError) as error:
                self.do_import(self.payload(source), key="r2-page-bound")
        self.assertEqual(error.exception.code, "limit_exceeded")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.rows("SELECT count(*) FROM imports"), ((0,),))
        self.assertIsNotNone(self.do_import(self.payload(source), key="r2-bound-restored").private)

    def test_native_attempt_without_live_capability_is_never_imported_authority(self):
        for name, mutate in (("deleted", lambda db: db.execute("DELETE FROM capabilities WHERE role='attempt'")),
                             ("revoked", lambda db: db.execute("UPDATE capabilities SET revoked=1 WHERE role='attempt'"))):
            with self.subTest(name):
                self.reset()
                assigned = self.f.assign(self.p, "setup")
                self.f.start(self.p, assigned)
                plan_id, attempt_id = self.p.current_metadata.plan_id, assigned.committed["attempt_id"]
                self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((1,),))
                self.f.store.write(mutate)
                self.assertFalse(self.is_imported(plan_id, attempt_id))
                if name == "deleted":
                    current = self.f.service.read_plan(plan_id).draft
                    error = self.refused(self.f.command("revise_plan", {
                        "draft": current, "reason": "r2 native retire", "authorization_change": None,
                        "retire_steps": (RetirementRequest("setup", "r2 native retire"),)}, receipt=self.p))
                    self.assertEqual(error.code, "invalid")

    def test_supported_existing_path_only_source_imports_with_its_own_identity(self):
        path_only = replace(workspace("/synthetic/r2-pathonly", 1900), device=None, inode=None)
        rid = self.f.register(path_only, self.p)
        receipt = self.do_import(self.payload(directory_id=rid), key="r2-pathonly")
        self.assertEqual(receipt.committed["source_directory_id"], rid)
        self.assertEqual(self.rows("SELECT source_directory_id FROM imports"), ((rid,),))
        self.assertEqual(self.origin()["committed_source_directory_id"], rid)

    # ====================================== R2 item 6/11: cohort, authorization, continuation
    def test_repository_exclusive_original_cohort_partial_and_full_stop(self):
        receipt, attempt = self.import_cohort("coh", 100)
        plan_id = receipt.current_metadata.plan_id
        uses = self.rows("SELECT resource_id, scope FROM resource_uses WHERE plan_id=?", plan_id)
        ids = {scope: rid for rid, scope in uses}
        self.assertEqual(sorted(ids), ["external", "repository"])
        namespace = self.f.store.read(lambda db: resources.execution_uses(db, plan_id, attempt.attempt_id))[0]
        self.assertIsNone(namespace["implementation"]["writer"])
        self.assertEqual(namespace["groups"], {})
        self.assertEqual(len(namespace["implementation"]["members"]), 2)
        rival = replace(context(None, repository_id=ids["repository"]), repository_exclusive=True)
        error = self.refused(self.f.command("assign_step", {
            "step_id": "setup", "assignee": "worker-b", "context": rival}, receipt=self.p))
        self.assertEqual(error.code, "resource_conflict")
        replacement = ExecutionContext("repository", ids["repository"], None, True, (), True, None)
        before = self.state()
        partial = self.refused(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker",
                                            replacement=replacement, stopped=(ids["external"],)), before)
        self.assertEqual(partial.code, "resource_conflict")
        outsider = self.f.register(workspace("/synthetic/r2-outsider", 2999), self.p)
        before = self.state()
        self.refused(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker", replacement=replacement,
                                  stopped=tuple(ids.values()) + (outsider,)), before)
        self.refused(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker", replacement=replacement,
                                  stopped=(outsider,)))
        replaced = self.f.service.execute(self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker",
                                                       replacement=replacement, stopped=tuple(ids.values()), key="r2-full-stop"))
        self.assertIsNotNone(replaced.private.attempt_token)
        self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses WHERE attempt_id=?", attempt.attempt_id),
                         (("released",),))
        self.assertEqual(self.rows("SELECT state FROM attempts WHERE attempt_id=?", attempt.attempt_id), (("released",),))

    def test_revision_seventeen_authorization_survives_attachment_and_terminal_change(self):
        source = self.snapshot17()
        scope = source["scope"]
        receipt = self.do_import(self.payload(source, path="/synthetic/r2-r17", inode=2200), key="r2-r17")
        plan_id = receipt.current_metadata.plan_id

        def authority():
            view = self.f.service.read_plan(plan_id)
            auth = view.draft.authorization
            return (view.metadata.revision, auth, view.legacy_snapshot["scope"])
        revision, auth, legacy_scope = authority()
        self.assertEqual(revision, 17)
        self.assertEqual((auth.mode, auth.since_revision, tuple(auth.baseline), auth.claimed, auth.note),
                         (scope.get("mode", source["executionMode"]), scope["sinceRevision"],
                          tuple(scope.get("baseline", ())), scope.get("claimed"), "synthetic legacy authorization"))
        self.assertTrue(auth.interval.startswith("legacy:"))
        self.f.service.execute(self.resolve(receipt, self.attempt(receipt), "attach-context", "blocked",
                                            attached=known_claim("/synthetic/r2-r17-live", 2201)))
        attached = authority()
        self.assertEqual((attached[0], attached[1], attached[2]), (18, auth, legacy_scope))
        self.f.service.execute(self.resolve(receipt, self.attempt(receipt), "release", "blocked"))
        released = authority()
        self.assertEqual((released[1], released[2]), (auth, legacy_scope))

    def test_completed_legacy_step_continues_only_through_a_genuine_independent_producer(self):
        proposal = copy.deepcopy(legacy_proposal())
        proposal["executionMode"] = "complete-task"
        proposal["steps"][0]["status"] = "complete"
        proposal["steps"][0]["evidence"] = ["historical completion note"]
        proposal["steps"].append({"id": "step-3", "title": "Independent validation", "status": "pending",
                                  "dependsOn": [], "check": "Validation exits 0", "evidence": []})
        proposal["steps"].append({"id": "step-4", "title": "Consume validation", "status": "pending",
                                  "dependsOn": ["step-1"], "check": "Consumer exits 0", "evidence": []})
        proposal["nextStepId"] = "step-2"
        source = prepare_publication(
            proposal, None, timestamp="2026-10-06T00:00:00Z", generation="00000000-0000-4000-8000-000000000001",
            expected_revision=0, expected_generation=None, authorization_note="synthetic legacy authorization",
            max_file_bytes=10 * 1024 * 1024, max_history=100)
        receipt = self.do_import(self.payload(source), key="r2-hist")
        plan_id = receipt.current_metadata.plan_id
        self.assertEqual(self.rows("SELECT count(*) FROM result_versions WHERE plan_id=?", plan_id), ((0,),))
        assigned = self.f.assign(receipt, step_id="step-2")
        before = self.state()
        guard = self.refused(self.f.command("start_step", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "started"}, receipt=receipt, attempt=assigned), before)
        self.assertEqual(guard.code, "invalid")
        self.assertEqual(self.rows("SELECT state FROM attempts WHERE attempt_id=?", assigned.committed["attempt_id"]),
                         (("assigned",),))
        fabricated = Availability("step-1", "fabricated-result", "ancestor", "synthetic-consumer-base", None,
                                  checkpoint().evidence[0])
        self.refused(self.f.command("bind_availability", {
            "bindings": (fabricated,), "checked_base": "synthetic-consumer-base"}, receipt=receipt, attempt=assigned))
        producer, value = self.live_accept(receipt, "step-3")
        current = self.f.service.read_plan(plan_id).draft
        revised = replace(current, steps=tuple(
            replace(s, dependencies=("step-3",), definition_version="v2") if s.step_id == "step-4" else s
            for s in current.steps))
        self.f.service.execute(self.f.command("revise_plan", {
            "draft": revised, "reason": "r2 explicit dependency on the genuine producer",
            "authorization_change": None, "retire_steps": ()}, receipt=receipt))
        consumer = self.f.assign(receipt, "step-4")
        self.f.service.execute(self.f.command("bind_availability", {
            "bindings": (Availability("step-3", producer.committed["result_version"], "ancestor",
                                      "synthetic-consumer-base", None, value.evidence[0]),),
            "checked_base": "synthetic-consumer-base"}, receipt=receipt, attempt=consumer))
        active = self.f.start(receipt, consumer)
        done = checkpoint(artifact_version="consumer-v1")
        self.f.submit(receipt, active, done)
        self.f.accept(receipt, active, done)
        view = self.f.service.read_plan(plan_id)
        step_one = next(s for s in view.legacy_snapshot["steps"] if s["id"] == "step-1")
        self.assertEqual((step_one["status"], step_one["evidence"]), ("complete", ["historical completion note"]))
        self.assertEqual(sorted(view.coordination.result_versions), ["step-3", "step-4"])

    # ====================================== R2 item 3 / B2: source-owned hold binding
    def test_stale_hold_clearing_is_bound_to_the_exact_source_owned_row(self):
        for name in ("foreign-hold-id", "altered-reason", "duplicated-hold", "missing-hold"):
            with self.subTest(name):
                self.reset()
                first, attempt_a = self.import_known("/synthetic/r2-ha", 2400)
                second, _ = self.import_known("/synthetic/r2-hb", 2410)
                plan_a, plan_b = first.current_metadata.plan_id, second.current_metadata.plan_id
                hold_a = self.rows("SELECT hold_id FROM holds WHERE plan_id=?", plan_a)[0][0]
                hold_b = self.rows("SELECT hold_id FROM holds WHERE plan_id=?", plan_b)[0][0]
                if name == "foreign-hold-id":
                    self.rewrite_json("attempts", "payload", "attempt_id=?", (attempt_a.attempt_id,),
                                      lambda data: data["import_source"].__setitem__("stale_hold_id", hold_b))
                elif name == "altered-reason":
                    self.rewrite_json("holds", "reason", "hold_id=?", (hold_a,),
                                      lambda data: data.__setitem__("reason", "altered"))
                elif name == "duplicated-hold":
                    self.f.store.write(lambda db: db.execute(
                        "INSERT INTO holds SELECT 'r2-dup-hold', plan_id, reason, active, created_at FROM holds WHERE hold_id=?",
                        (hold_a,)))
                else:
                    self.f.store.write(lambda db: db.execute("DELETE FROM holds WHERE hold_id=?", (hold_a,)))
                self.assertFalse(self.is_imported(plan_a, attempt_a.attempt_id))
                self.refused(self.resolve(first, attempt_a, "release", "blocked"))
                self.assertEqual(self.rows("SELECT active FROM holds WHERE hold_id=?", hold_b), ((1,),))

    def test_identity_clearing_is_bound_to_the_exact_source_owned_reason(self):
        for name in ("foreign-id", "altered-details", "resolved-while-unknown"):
            with self.subTest(name):
                self.reset()
                first = self.do_import(self.payload(path="/synthetic/r2-ia", inode=2500), key="r2-ia")
                second = self.do_import(self.payload(path="/synthetic/r2-ib", inode=2510), key="r2-ib")
                attempt_a, attempt_b = self.attempt(first), self.attempt(second)
                payload_of = lambda receipt: json.loads(self.rows(
                    "SELECT payload FROM attempts WHERE plan_id=?", receipt.current_metadata.plan_id)[0][0])
                reason_a, reason_b = payload_of(first)["identity_hold_id"], payload_of(second)["identity_hold_id"]
                if name == "foreign-id":
                    self.rewrite_json("attempts", "payload", "attempt_id=?", (attempt_a.attempt_id,),
                                      lambda data: data.__setitem__("identity_hold_id", reason_b))
                elif name == "altered-details":
                    self.f.store.write(lambda db: db.execute(
                        "UPDATE store_hold_reasons SET details=? WHERE reason_id=?",
                        (json.dumps({"plan_id": "other"}), reason_a)))
                else:
                    self.f.store.write(lambda db: db.execute(
                        "UPDATE store_hold_reasons SET resolved=1 WHERE reason_id=?", (reason_a,)))
                self.refused(self.resolve(first, attempt_a, "attach-context", "blocked",
                                          attached=known_claim(f"/synthetic/r2-ia-live-{name}", 2501)))
                self.assertEqual(self.rows("SELECT resolved FROM store_hold_reasons WHERE reason_id=?", reason_b), ((0,),))
                self.assertEqual(self.rows("SELECT active FROM holds WHERE hold_id=?", reason_b), ((1,),))

    def test_terminal_history_must_stay_coherent_with_its_cleared_stale_hold(self):
        receipt, attempt = self.import_known("/synthetic/r2-term", 2550)
        plan_id = receipt.current_metadata.plan_id
        hold = self.rows("SELECT hold_id FROM holds WHERE plan_id=?", plan_id)[0][0]
        self.assertTrue(self.is_imported(plan_id, attempt.attempt_id))
        self.f.service.execute(self.resolve(receipt, attempt, "release", "blocked"))
        self.assertTrue(self.is_imported(plan_id, attempt.attempt_id))
        self.f.store.write(lambda db: db.execute("UPDATE holds SET active=1 WHERE hold_id=?", (hold,)))
        self.assertFalse(self.is_imported(plan_id, attempt.attempt_id))

    # ====================================== R2 item 4: reconstructed immutable proof
    def test_original_step_definition_is_reconstructed_not_compared_with_mutable_copies(self):
        receipt, attempt = self.import_known("/synthetic/r2-def", 2600)
        plan_id = receipt.current_metadata.plan_id

        def retitle(value):
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key == "title" and item == "Set up database schema":
                        value[key] = "tampered title"
                    else:
                        retitle(item)
            elif isinstance(value, list):
                for item in value:
                    retitle(item)
        self.rewrite_json("attempts", "payload", "attempt_id=?", (attempt.attempt_id,), retitle)
        self.rewrite_json("steps", "payload", "plan_id=? AND step_id='step-1'", (plan_id,), retitle)
        self.f.store.write(lambda db: db.execute(
            "UPDATE steps SET title='tampered title' WHERE plan_id=? AND step_id='step-1'", (plan_id,)))
        self.assertFalse(self.is_imported(plan_id, attempt.attempt_id))
        self.refused(self.resolve(receipt, attempt, "release", "blocked"))

    def test_attachment_lineage_must_name_this_attempt_step_definition_and_versions(self):
        for number, (field, forged) in enumerate((("step_id", "other-step"), ("definition_version", "legacy:forged"),
                                                  ("version", 9), ("attempt_id", "other-attempt"),
                                                  ("before_version", 4))):
            with self.subTest(field):
                self.reset()
                receipt = self.do_import(self.payload(), key=f"r2-al-{field}")
                attempt = self.attempt(receipt)
                plan_id = receipt.current_metadata.plan_id
                self.f.service.execute(self.resolve(receipt, attempt, "attach-context", "blocked",
                                                    attached=known_claim(f"/synthetic/r2-al-{field}", 2700 + number)))
                self.rewrite_json("events", "payload", "kind='resolve_attempt' AND plan_id=?", (plan_id,),
                                  lambda data: data.__setitem__(field, forged))
                self.assertFalse(self.is_imported(plan_id, attempt.attempt_id))
                self.refused(self.resolve(receipt, self.attempt(receipt), "release", "blocked"))

    def test_origin_snapshot_digest_and_constructor_entries_are_recomputed(self):
        mutations = {
            "objective": lambda e: e["original_snapshot"].__setitem__("objective", "tampered objective"),
            "missing-initial-context": lambda e: e["initial_contexts"].pop("step-1"),
            "extra-constructor": lambda e: e["constructor_attempts"].__setitem__("ghost", "ghost-attempt"),
            "extra-initial-context": lambda e: e["initial_contexts"].__setitem__("ghost", None),
        }
        for number, (name, mutate) in enumerate(mutations.items()):
            with self.subTest(name):
                self.reset()
                first = self.do_import(self.payload(path=f"/synthetic/r2-od-{name}", inode=2800 + number), key=f"r2-od-{name}")
                attempt = self.attempt(first)
                self.rewrite_json("events", "payload", "kind='import_plan'", (), mutate)
                self.refused(self.f.command("import_plan", self.payload(path=f"/synthetic/r2-od-{name}", inode=2800 + number),
                                            key=f"r2-od-retry-{name}"))
                self.refused(self.resolve(first, attempt, "attach-context", "blocked",
                                          attached=known_claim(f"/synthetic/r2-od-live-{name}", 2850 + number)))

    # ====================================== R2 item 5 / B1: original receipt and replay binding
    def test_original_receipt_contradictions_fail_closed_without_disclosure(self):
        cases = {
            "revision": lambda r: replace(r, committed_metadata=replace(r.committed_metadata, revision=999)),
            "ownership": lambda r: replace(r, committed_metadata=replace(r.committed_metadata, ownership_version=999)),
            "generation": lambda r: replace(r, committed_metadata=replace(r.committed_metadata, generation="foreign-generation")),
            "committed-shape": lambda r: replace(r, committed={**r.committed, "extra": "x"}),
        }
        for name, forge in cases.items():
            with self.subTest(name):
                self.reset()
                first = self.do_import(self.payload(), key="r2-m-one")
                forged = plans.encode_response(forge(first))
                self.f.store.write(lambda db: db.execute(
                    "UPDATE requests SET response_payload=? WHERE key='r2-m-one'", (forged,)))
                self.refused(self.f.command("import_plan", self.payload(), key="r2-m-two"))
                self.refused(self.f.command("import_plan", self.payload(), key="r2-m-one"))

    def test_replayed_receipt_must_agree_with_request_row_and_origin(self):
        first = self.do_import(self.payload(path="/synthetic/r2-fa", inode=2300), key="r2-fa")
        second = self.do_import(self.payload(path="/synthetic/r2-fb", inode=2310), key="r2-fb")
        dedup_command = self.f.command("import_plan", self.payload(path="/synthetic/r2-fa", inode=2300), key="r2-fa-dedup")
        dedup = self.f.service.execute(dedup_command)
        self.assertEqual(dedup.private.orchestrator_token, first.private.orchestrator_token)
        self.assertEqual(self.f.service.execute(dedup_command).private.orchestrator_token, first.private.orchestrator_token)
        swapped = plans.encode_response(replace(second, private=None))
        self.f.store.write(lambda db: db.execute(
            "UPDATE requests SET response_payload=? WHERE key='r2-fa-dedup'", (swapped,)))
        self.refused(dedup_command)
        self.assertEqual(self.f.service.execute(self.f.command(
            "import_plan", self.payload(path="/synthetic/r2-fb", inode=2310), key="r2-fb-again")).private.orchestrator_token,
            second.private.orchestrator_token)

    # ====================================== R3 items 5/B1/6(a): incoming source binding on exact replay
    def test_exact_replay_binds_incoming_source_to_the_validated_origin(self):
        source = active_legacy_snapshot()
        first = self.do_import(self.payload(source, path="/synthetic/r3-a", inode=3001), key="r3-a")
        second = self.do_import(self.payload(source, path="/synthetic/r3-b", inode=3011), key="r3-b")
        plan_a, plan_b = first.current_metadata.plan_id, second.current_metadata.plan_id
        self.assertNotEqual(plan_a, plan_b)
        bodies = {
            "claim": self.payload(source, path="/synthetic/r3-a", inode=3001),
            "existing-id": self.payload(source, directory_id=first.committed["source_directory_id"]),
        }
        for name, body in bodies.items():
            for mode in ("plan-and-response", "plan-only"):
                with self.subTest(name, mode=mode):
                    key = f"r3-{name}-{mode}"
                    command = self.f.command("import_plan", body, key=key)
                    dedup = self.f.service.execute(command)
                    self.assertEqual(dedup.private.orchestrator_token, first.private.orchestrator_token)
                    digest = self.rows("SELECT command_digest FROM requests WHERE key=?", key)
                    swapped = plans.encode_response(replace(second, private=None))
                    if mode == "plan-only":
                        self.f.store.write(lambda db: db.execute(
                            "UPDATE requests SET plan_id=? WHERE key=?", (plan_b, key)))
                    else:
                        self.f.store.write(lambda db: db.execute(
                            "UPDATE requests SET plan_id=?, response_payload=? WHERE key=?", (plan_b, swapped, key)))
                    error = self.refused(command)
                    self.assertEqual(error.code, "invalid")
                    self.assertEqual(self.rows("SELECT command_digest FROM requests WHERE key=?", key), digest)
                    self.assertEqual(self.rows("SELECT plan_id FROM requests WHERE key=?", key), ((plan_b,),))
                    self.f.store.write(lambda db: db.execute(
                        "UPDATE requests SET plan_id=?, response_payload=? WHERE key=?",
                        (plan_a, plans.encode_response(replace(dedup, private=None)), key)))
                    self.assertEqual(self.f.service.execute(command).private.orchestrator_token,
                                     first.private.orchestrator_token)

    def test_exact_replay_source_proof_is_read_only_and_keeps_alias_reconciliation(self):
        with ExitStack() as _owned_store_lifetimes:
            survivor, retired = self.reconciled_pair("r3-alias", 3100)
            source = active_legacy_snapshot()
            command = self.f.command("import_plan", self.payload(source, directory_id=retired), key="r3-alias-first")
            first = self.f.service.execute(command)
            token = first.private.orchestrator_token
            before = self.state()
            again = self.f.service.execute(command)
            self.assertTrue(again.replayed)
            self.assertEqual(again.private.orchestrator_token, token)
            self.assertEqual(self.state(), before)
            canonical = self.f.command("import_plan", self.payload(source, directory_id=survivor), key="r3-alias-survivor")
            dedup = self.f.service.execute(canonical)
            self.assertEqual(dedup.private.orchestrator_token, token)
            before = self.state()
            self.assertEqual(self.f.service.execute(canonical).private.orchestrator_token, token)
            self.assertEqual(self.state(), before)
            claim = self.f.command("import_plan", self.payload(source, path="/synthetic/r3-claim-src", inode=3150), key="r3-claim-first")
            fresh = self.f.service.execute(claim)
            self.assertNotEqual(fresh.current_metadata.plan_id, first.current_metadata.plan_id)
            before = self.state()
            self.assertEqual(self.f.service.execute(claim).private.orchestrator_token, fresh.private.orchestrator_token)
            self.assertEqual(self.state(), before)
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            self.assertEqual(reopened.service.execute(claim).private.orchestrator_token, fresh.private.orchestrator_token)
            self.assertEqual(self.state(), before)

    # ====================================== R3 items 6(b)/11: retained imported history across success
    LIFECYCLE_KEYS = frozenset({"cancellation_reason", "cancelled_at", "cancelled_from"})

    def retained(self, plan_id, attempt_id):
        """Everything that must survive an imported terminal or replacement, with only status/version excluded."""
        def read(db):
            row = db.execute("SELECT step_id, assignee, definition_version, started_at, stopped_at, payload "
                             "FROM attempts WHERE plan_id=? AND attempt_id=?", (plan_id, attempt_id)).fetchone()
            members, namespace = resources.execution_uses(db, plan_id, attempt_id)
            uses = tuple(db.execute("SELECT use_id, resource_id, scope, created_at FROM resource_uses "
                                    "WHERE plan_id=? AND attempt_id=? ORDER BY use_id", (plan_id, attempt_id)))
            origin = tuple(db.execute("SELECT payload FROM events WHERE plan_id=? AND kind='import_plan'", (plan_id,)))
            mapped = tuple(db.execute("SELECT import_id, host_id, source_directory_id, original_task_id, content_digest "
                                      "FROM imports WHERE plan_id=?", (plan_id,)))
            return row[:5], json.loads(row[5]), members, {k: tuple(v[:6]) + tuple(v[7:]) for k, v in namespace.items()}, \
                uses, origin, mapped
        head, payload, members, namespace, uses, origin, mapped = self.f.store.read(read)
        view = self.f.service.read_plan(plan_id)
        return {"head": head, "payload": payload, "members": members, "namespace": namespace, "uses": uses,
                "origin": origin, "mapped": mapped, "authorization": view.draft.authorization,
                "legacy_scope": view.legacy_snapshot["scope"],
                "definitions": {s.step_id: s for s in view.draft.steps}}

    def assert_history_retained(self, before, after):
        extra = set(after["payload"]) - set(before["payload"])
        self.assertLessEqual(extra, self.LIFECYCLE_KEYS)
        self.assertEqual({k: after["payload"][k] for k in before["payload"]}, before["payload"])
        for key in ("head", "members", "namespace", "uses", "origin", "mapped", "authorization", "legacy_scope"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["definitions"]["step-1"], before["definitions"]["step-1"])

    def test_successful_imported_terminal_resolutions_retain_the_original_history(self):
        for name, resolution, next_state in (("release", "release", "blocked"), ("reject", "reject", "blocked"),
                                              ("retire", "release", "retired")):
            with self.subTest(name):
                self.reset()
                receipt, attempt = self.import_known(
                    f"/synthetic/r3-term-{name}", 3200,
                    source=independent_legacy_snapshot() if name == "retire" else None)
                plan_id = receipt.current_metadata.plan_id
                before = self.retained(plan_id, attempt.attempt_id)
                self.assertTrue(self.is_imported(plan_id, attempt.attempt_id))
                self.f.service.execute(self.resolve(receipt, attempt, resolution, next_state))
                after = self.retained(plan_id, attempt.attempt_id)
                self.assert_history_retained(before, after)
                self.assertEqual(self.rows("SELECT state, version FROM attempts WHERE attempt_id=?", attempt.attempt_id),
                                 (("rejected" if resolution == "reject" else "released", 2),))
                self.assertTrue(self.is_imported(plan_id, attempt.attempt_id))
                self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((0,),))
                self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses WHERE attempt_id=?", attempt.attempt_id),
                                 (("unresolved",),))

    def test_successful_full_cohort_replacement_retains_history_and_replays_exactly(self):
        with ExitStack() as _owned_store_lifetimes:
            receipt, attempt = self.import_cohort("hist", 3300)
            plan_id = receipt.current_metadata.plan_id
            ids = {scope: rid for rid, scope in self.rows("SELECT resource_id, scope FROM resource_uses WHERE plan_id=?", plan_id)}
            replacement = ExecutionContext("repository", ids["repository"], None, True, (), True, None)
            before = self.retained(plan_id, attempt.attempt_id)
            command = self.resolve(receipt, attempt, "replace", "pending", assignee="new-worker",
                                   replacement=replacement, stopped=tuple(ids.values()), key="r3-hist-replace")
            replaced = self.f.service.execute(command)
            after = self.retained(plan_id, attempt.attempt_id)
            self.assert_history_retained(before, after)
            self.assertEqual(self.rows("SELECT state, version FROM attempts WHERE attempt_id=?", attempt.attempt_id),
                             (("released", 2),))
            self.assertEqual(self.rows("SELECT DISTINCT status FROM resource_uses WHERE attempt_id=?", attempt.attempt_id),
                             (("released",),))
            successor = self.rows("SELECT attempt_id FROM attempts WHERE plan_id=? AND step_id='step-1' AND attempt_id<>?",
                                  plan_id, attempt.attempt_id)
            self.assertEqual(len(successor), 1)
            self.assertTrue(self.is_imported(plan_id, attempt.attempt_id))
            self.assertFalse(self.is_imported(plan_id, successor[0][0]))
            self.assertEqual(self.rows("SELECT count(*) FROM capabilities WHERE role='attempt'"), ((1,),))
            settled = self.state()
            self.assertEqual(self.f.service.execute(command).private.attempt_token, replaced.private.attempt_token)
            self.assertEqual(self.state(), settled)
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            self.assertEqual(reopened.service.execute(command).private.attempt_token, replaced.private.attempt_token)
            self.assertEqual(self.state(), settled)
            self.assert_history_retained(before, self.retained(plan_id, attempt.attempt_id))


if __name__ == "__main__":
    unittest.main()
