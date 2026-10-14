"""Global scope regressions and isolated synthetic accepted-history fixtures."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
import unittest

from adaptive_planning.core import (
    MAX_FILE_BYTES, MAX_HISTORY, PlanValidationError, prepare_publication,
    validate_execution_scope, validate_published_snapshot,
)
from adaptive_planning.types import ArtifactRef, AuthorizationChange, DocumentRef, DomainError, RetirementRequest
from tests import service_fixtures as fixtures
from tests.service_fixtures import make_service, step


class TestService(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = make_service(Path(directory.name))
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    def snapshot(self, fixture=None):
        return (fixture or self.f).store.read(lambda db: tuple(db.iterdump()))

    def revise(self, receipt, *, change=None, reason="synthetic ordinary resume", **changes):
        current = self.f.service.read_plan(receipt.current_metadata.plan_id).draft
        return self.f.service.execute(self.f.command("revise_plan", {
            "draft": replace(current, **changes), "reason": reason,
            "authorization_change": change, "retire_steps": (),
        }, receipt=receipt))

    def seed_function(self):
        function = getattr(fixtures, "seed_accepted_result", None)
        self.assertTrue(callable(function), "TEST-ONLY accepted-result seed is missing")
        return function

    def owned_fixture(self):
        fixture = make_service()
        self.addCleanup(fixture.cleanup)
        return fixture

    def test_plan_only_no_assignment(self):
        planning = self.f.create("plan-only", creation_id="planning-only")
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.assign(planning, "a")
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertTrue(self.snapshot() == before, "Denied assignment changed stored state")
        self.assertEqual(self.f.service.read_plan(planning.current_metadata.plan_id).coordination.attempts, ())
        counts = self.f.store.read(lambda db: tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                                   for table in ("attempts", "resources", "resource_uses")))
        self.assertEqual(counts, (0, 0, 0))

    def test_first_step_global_latch_survives_every_transition(self):
        generation = "d99b8e00-0c86-4d82-9a05-230687f2a6ae"
        proposal = {
            "schemaVersion": 2, "taskId": "pure-latch", "objective": "Synthetic transition matrix",
            "status": "running", "executionMode": "first-step", "nextStepId": "a",
            "changeSummary": "Synthetic initial claim",
            "steps": [
                {"id": "a", "title": "A", "status": "in-progress", "dependsOn": [], "check": "check a", "evidence": []},
                {"id": "b", "title": "B", "status": "pending", "dependsOn": [], "check": "check b", "evidence": []},
            ],
        }

        def publish(proposed, previous=None, note=None):
            return prepare_publication(
                proposed, previous, timestamp="2026-10-06T12:00:00+00:00", generation=generation,
                expected_revision=0 if previous is None else previous["revision"],
                expected_generation=None if previous is None else generation, authorization_note=note,
                max_file_bytes=MAX_FILE_BYTES, max_history=MAX_HISTORY)

        claimed = publish(proposal, note="Synthetic explicit one-step assertion")
        blocked = copy.deepcopy(proposal)
        blocked["steps"][0]["status"] = "blocked"
        blocked["nextStepId"] = "b"
        blocked["changeSummary"] = "Synthetic blocked claim"
        previous = publish(blocked, claimed)
        expected_scope = {"mode": "first-step", "sinceRevision": 1, "baseline": [], "claimed": "a"}
        for state in ("pending", "in-progress", "blocked", "retired", "complete", "removed"):
            with self.subTest(state=state):
                candidate = copy.deepcopy(blocked)
                candidate["changeSummary"] = f"Synthetic transition to {state}"
                node = candidate["steps"][0]
                if state == "removed":
                    candidate["steps"].pop(0)
                else:
                    node["status"] = state
                    if state == "retired":
                        node["retiredReason"] = "Synthetic obsolete route"
                    if state == "complete":
                        node["evidence"] = ["Synthetic verified completion"]
                candidate["nextStepId"] = "a" if state in ("pending", "in-progress") else "b"
                transitioned = publish(candidate, previous)
                self.assertEqual(transitioned["scope"], expected_scope)
                competitor = copy.deepcopy(candidate)
                competitor["steps"][-1]["status"] = "in-progress"
                competitor["nextStepId"] = "b"
                competitor["changeSummary"] = "Synthetic second logical winner"
                with self.assertRaises(PlanValidationError):
                    publish(competitor, transitioned)
                if state == "removed":
                    retry = copy.deepcopy(candidate)
                    retry["steps"].insert(0, copy.deepcopy(proposal["steps"][0]))
                    retry["nextStepId"] = "a"
                    self.assertEqual(publish(retry, transitioned)["scope"], expected_scope)

        receipt = self.f.create("first-step", steps=(step("a"), step("b")), creation_id="lasting-scope")
        plan_id = receipt.current_metadata.plan_id
        self.f.assign(receipt, "a")
        authorization = self.f.service.read_plan(plan_id).draft.authorization
        self.assertEqual(authorization.claimed, "a")
        self.assertEqual(self.f.service.read_plan(plan_id).legacy_snapshot["steps"][0]["status"], "pending")
        receipt = self.revise(receipt, stop_reason="Synthetic pause")
        receipt = self.revise(receipt, stop_reason=None)
        restarted = make_service(self.f.directory, self.f.clock)
        self.f.service, self.f.store = restarted.service, restarted.store
        receipt = self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": receipt.current_metadata.ownership_version,
            "reason": "Synthetic ownership transfer",
        }, receipt=receipt))
        self.assertEqual(self.f.service.read_plan(plan_id).draft.authorization, authorization)
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.assign(receipt, "b")
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertTrue(self.snapshot() == before, "Takeover allowed a different logical winner")
        current = self.f.service.read_plan(plan_id).draft
        self.f.service.execute(self.f.command("revise_plan", {
            "draft": current, "reason": "Synthetic retirement of claimed work", "authorization_change": None,
            "retire_steps": (RetirementRequest("a", "Synthetic obsolete route"),),
        }, receipt=receipt))
        self.assertEqual(self.f.service.read_plan(plan_id).draft.authorization, authorization)
        with self.assertRaises(DomainError):
            self.f.assign(receipt, "b")

    def test_first_step_renewal_accounts_for_assigned_work(self):
        receipt = self.f.create("first-step", creation_id="assigned-scope")
        self.f.assign(receipt, "a")
        change = AuthorizationChange("first-step", "Synthetic actual authorization assertion")
        updated = self.revise(receipt, change=change)
        self.assertEqual(self.f.service.read_plan(receipt.current_metadata.plan_id).draft.authorization.claimed, "a")
        with self.assertRaises(DomainError):
            self.f.assign(updated, "b")
        other = self.f.create(creation_id="two-active")
        self.f.assign(other, "a")
        self.f.assign(other, "b")
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.revise(other, change=change)
        self.assertEqual(caught.exception.code, "invalid")
        self.assertTrue(self.snapshot() == before, "Rejected multi-step renewal changed stored state")

    def test_scope_change_requires_grounded_note(self):
        receipt = self.f.create("first-step", creation_id="scope-plan")
        before = self.f.service.read_plan(receipt.current_metadata.plan_id).draft
        updated = self.revise(receipt, reason="resume same instructions")
        after = self.f.service.read_plan(receipt.current_metadata.plan_id).draft.authorization
        self.assertEqual(after, before.authorization)
        snapshot = self.snapshot()
        with self.assertRaises(DomainError):
            self.f.service.execute(self.f.command("revise_plan", {
                "draft": replace(before, authorization=replace(after, claimed="b")), "reason": "tamper",
                "authorization_change": None, "retire_steps": (),
            }, receipt=updated))
        for note in ("", "   ", "x" * 10001):
            with self.subTest(note_length=len(note)), self.assertRaises(DomainError):
                self.revise(updated, change=AuthorizationChange("first-step", note))
        self.assertTrue(self.snapshot() == snapshot, "Ungrounded scope change mutated state")
        changed = self.revise(updated, change=AuthorizationChange("first-step", "  Synthetic grounded assertion  "))
        auth = self.f.service.read_plan(receipt.current_metadata.plan_id).draft.authorization
        self.assertNotEqual(auth.interval, after.interval)
        self.assertEqual((auth.since_revision, auth.baseline, auth.claimed, auth.note),
                         (changed.current_metadata.revision, (), None, "Synthetic grounded assertion"))

    def test_seed_has_coherent_artifact_bound_history(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        definition = replace(step("a", review_required=True, final_gate=True),
                             title="Synthetic reviewed output", completion_check="Synthetic exact check",
                             document=DocumentRef("synthetic-plan.md", "synthetic-branch", "synthetic-commit", "v2", "a"),
                             definition_version="synthetic-definition-v2")
        receipt = fixture.create(steps=(definition, step("b")))
        plan_id = receipt.current_metadata.plan_id
        artifact = ArtifactRef("document", "synthetic-document", "artifact-v7", "synthetic-digest", "Synthetic output")
        authorization = fixture.service.read_plan(plan_id).draft.authorization
        before_capabilities = fixture.store.read(lambda db: db.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0])
        result = seed(fixture, plan_id, "a", artifact)
        self.assertIsInstance(result, fixtures.SeededResult)
        self.assertEqual((result.plan_id, result.step_id, result.artifact), (plan_id, "a", artifact))
        self.assertNotEqual(result.result_version, artifact.version)
        with self.assertRaises(FrozenInstanceError):
            result.step_id = "b"
        view = fixture.service.read_plan(plan_id)
        validate_published_snapshot(view.legacy_snapshot)
        self.assertIsNotNone(view.legacy_publication)
        self.assertEqual(view.draft.authorization, authorization)
        self.assertEqual(view.metadata.revision, receipt.current_metadata.revision + 1)
        attempt = next(a for a in view.coordination.attempts if a.attempt_id == result.attempt_id)
        self.assertEqual((attempt.step_id, attempt.state, attempt.definition_version, attempt.result_version),
                         ("a", "accepted", "synthetic-definition-v2", result.result_version))
        review = next(r for r in view.coordination.reviews if r.attempt_id == result.attempt_id)
        self.assertNotEqual(review.review.reviewer, attempt.assignee)
        self.assertEqual((review.step_id, review.definition_version, review.review.artifact, review.review.verdict),
                         ("a", "synthetic-definition-v2", artifact, "passed"))
        self.assertTrue(review.review.evidence)
        self.assertTrue(all(e.artifact == artifact and e.result == "passed" for e in review.review.evidence))
        self.assertEqual(view.coordination.result_versions, {"a": result.result_version})
        node = view.legacy_snapshot["steps"][0]
        self.assertEqual(node["status"], "complete")
        self.assertTrue(any("artifact-v7" in evidence for evidence in node["evidence"]))

        def history(db):
            frozen = json.loads(db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                            (plan_id, result.attempt_id)).fetchone()[0])
            checkpoint = db.execute("SELECT sequence,outcome,check_name,evidence,origin_worker,artifact FROM checkpoints "
                                    "WHERE plan_id=? AND attempt_id=?", (plan_id, result.attempt_id)).fetchone()
            artifact_row = db.execute("SELECT step_id,artifact_kind,identity,summary,digest FROM result_versions "
                                     "WHERE plan_id=? AND result_version=?", (plan_id, result.result_version)).fetchone()
            event_rows = db.execute("SELECT sequence,plan_revision,kind,payload,origin_worker FROM events "
                                    "WHERE plan_id=? ORDER BY sequence", (plan_id,)).fetchall()
            return frozen, checkpoint, artifact_row, event_rows, db.execute("PRAGMA foreign_key_check").fetchall(), \
                db.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]

        frozen, checkpoint, artifact_row, event_rows, foreign_keys, capabilities = fixture.store.read(history)
        self.assertEqual(frozen["definition"], fixtures_json(definition))
        self.assertEqual((frozen["epoch"], frozen["generation"]), (view.metadata.epoch, view.metadata.generation))
        self.assertEqual(frozen["context"]["kind"], "read-only")
        self.assertFalse(frozen["recovery_required"])
        self.assertEqual(frozen["result_version"], result.result_version)
        self.assertEqual(checkpoint[1:3], ("passed", "Synthetic exact check"))
        self.assertEqual(checkpoint[4], attempt.assignee)
        self.assertEqual(json.loads(checkpoint[5]), fixtures_json(artifact))
        self.assertTrue(all(e["artifact"] == fixtures_json(artifact) and e["result"] == "passed"
                            for e in json.loads(checkpoint[3])))
        self.assertEqual(artifact_row, ("a", "document", "synthetic-document", "Synthetic output", "synthetic-digest"))
        checkpoint_event = next(row for row in event_rows if row[0] == checkpoint[0])
        self.assertIsNone(checkpoint_event[1])
        self.assertEqual(checkpoint_event[4], attempt.assignee)
        synthetic = [row for row in event_rows if json.loads(row[3]).get("synthetic_history")]
        self.assertEqual(len(synthetic), 3)
        self.assertEqual([row[1] for row in synthetic], [None, None, view.metadata.revision])
        self.assertEqual([row[0] for row in synthetic], sorted(row[0] for row in synthetic))
        review_event = synthetic[1]
        self.assertNotEqual(review.recorded_sequence, review_event[0])
        for row in synthetic:
            payload = json.loads(row[3])
            self.assertEqual((payload["step_id"], payload["attempt_id"], payload["result_version"]),
                             ("a", result.attempt_id, result.result_version))
            self.assertNotIn("authorization_note", payload)
        self.assertEqual(foreign_keys, [])
        self.assertEqual(capabilities, before_capabilities)

    def test_seed_preserves_existing_evidence_and_non_target_history(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        receipt = fixture.create()
        plan_id = receipt.current_metadata.plan_id
        other = seed(fixture, plan_id, "b", ArtifactRef("code", "synthetic-b", "v-b", None, "Synthetic B"))

        def prefix(db):
            raw = db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'", (plan_id,)).fetchone()[0]
            payload = json.loads(raw)
            payload["evidence"] = ["Synthetic earlier observation", "Synthetic retained limitation"]
            db.execute("UPDATE steps SET payload=? WHERE plan_id=? AND step_id='a'", (json.dumps(payload), plan_id))
        fixture.store.write(prefix)
        before = fixture.service.read_plan(plan_id)
        events = fixture.service.read_events(plan_id)
        seed(fixture, plan_id, "a", ArtifactRef("code", "synthetic-a", "v-a", None, "Synthetic A"))
        after = fixture.service.read_plan(plan_id)
        self.assertEqual(after.legacy_snapshot["steps"][0]["evidence"][:2],
                         ["Synthetic earlier observation", "Synthetic retained limitation"])
        self.assertEqual(after.legacy_snapshot["steps"][1:], before.legacy_snapshot["steps"][1:])
        self.assertEqual(fixture.service.read_events(plan_id)[:len(events)], events)
        self.assertEqual(after.coordination.result_versions["b"], other.result_version)
        self.assertEqual(after.draft.authorization, before.draft.authorization)

    def test_seed_requires_owned_temporary_fixture(self):
        seed = self.seed_function()
        before = self.snapshot()
        artifact = ArtifactRef("observation", "synthetic-observation", "v1", None, "Synthetic evidence")
        with self.assertRaises(DomainError):
            seed(self.f, self.plan_id, "a", artifact)
        self.assertTrue(self.snapshot() == before, "Unowned temporary directory accepted a seed")
        arbitrary = fixtures.ServiceFixture(self.f.service, self.f.store, Path.cwd(), self.f.clock, self.f.epoch)
        with self.assertRaises(DomainError):
            seed(arbitrary, self.plan_id, "a", artifact)
        self.assertTrue(self.snapshot() == before, "An arbitrary directory accepted a synthetic seed")

    def test_seed_rejects_replaced_live_temporary_directory(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        receipt = fixture.create()
        directory = fixture.directory
        moved = directory.with_name(directory.name + "-old")
        directory.rename(moved)
        self.addCleanup(rmtree, moved, ignore_errors=True)
        replacement = make_service(directory)
        before = self.snapshot(replacement)
        with self.assertRaises(DomainError):
            seed(fixture, receipt.current_metadata.plan_id, "a",
                 ArtifactRef("code", "synthetic-replaced", "v1", None, "Synthetic evidence"))
        self.assertTrue(self.snapshot(replacement) == before, "A replaced directory reused stale temporary ownership")

    def test_seed_is_bound_to_the_named_plan_and_step(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        first = fixture.create(creation_id="seed-first-plan")
        second = fixture.create(steps=(replace(step("a"), title="Synthetic second plan A"), step("b")),
                                creation_id="seed-second-plan")
        first_id, second_id = first.current_metadata.plan_id, second.current_metadata.plan_id
        first_before = fixture.service.read_plan(first_id)
        events_before = fixture.service.read_events(first_id)
        result = seed(fixture, second_id, "a", ArtifactRef("code", "synthetic-second-plan", "v1", None, "Synthetic evidence"))
        self.assertEqual((result.plan_id, result.step_id), (second_id, "a"))
        self.assertEqual(fixture.service.read_plan(first_id).legacy_snapshot, first_before.legacy_snapshot)
        self.assertEqual(fixture.service.read_events(first_id), events_before)
        self.assertEqual(fixture.service.read_plan(first_id).coordination.result_versions, {})
        self.assertEqual(fixture.service.read_plan(second_id).legacy_snapshot["steps"][0]["title"], "Synthetic second plan A")

    def test_seed_requires_matching_directory_and_store(self):
        seed = self.seed_function()
        fixture, other = self.owned_fixture(), self.owned_fixture()
        receipt = fixture.create()
        plan_id = receipt.current_metadata.plan_id
        artifact = ArtifactRef("code", "synthetic-path-guard", "v1", None, "Synthetic evidence")
        before, other_before = self.snapshot(fixture), self.snapshot(other)
        original_directory = fixture.directory
        fixture.directory = other.directory
        with self.assertRaises(DomainError):
            seed(fixture, plan_id, "a", artifact)
        fixture.directory = original_directory
        original_store = fixture.store
        fixture.store = other.store
        with self.assertRaises(DomainError):
            seed(fixture, plan_id, "a", artifact)
        fixture.store = original_store
        self.assertTrue(self.snapshot(fixture) == before, "Directory mismatch changed original store")
        self.assertTrue(self.snapshot(other) == other_before, "Store mismatch changed unrelated store")

    def test_seed_rejects_cleaned_temporary_ownership_without_recreating_store(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        receipt = fixture.create()
        directory, database = fixture.directory, fixture.store.path
        fixture.cleanup()
        with self.assertRaises(DomainError):
            seed(fixture, receipt.current_metadata.plan_id, "a", ArtifactRef("code", "synthetic-cleaned", "v1", None, "Synthetic"))
        self.assertFalse(directory.exists())
        self.assertFalse(database.exists())
        recreated = make_service(directory)
        self.addCleanup(rmtree, directory, ignore_errors=True)
        before = self.snapshot(recreated)
        with self.assertRaises(DomainError):
            seed(fixture, receipt.current_metadata.plan_id, "a", ArtifactRef("code", "synthetic-cleaned", "v1", None, "Synthetic"))
        self.assertTrue(self.snapshot(recreated) == before, "Cleaned ownership became valid after path recreation")

    def test_seed_requires_complete_task_scope_and_preserves_it(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        artifact = ArtifactRef("code", "synthetic-scope-guard", "v1", None, "Synthetic evidence")
        for mode in ("plan-only", "first-step"):
            with self.subTest(mode=mode):
                receipt = fixture.create(mode, creation_id=f"scope-{mode}")
                before = self.snapshot(fixture)
                with self.assertRaises(DomainError):
                    seed(fixture, receipt.current_metadata.plan_id, "a", artifact)
                self.assertTrue(self.snapshot(fixture) == before, "Seed renewed authorization")

    def test_seed_rejects_live_or_terminal_target_and_incomplete_prerequisite_atomically(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        receipt = fixture.create()
        plan_id = receipt.current_metadata.plan_id
        artifact = ArtifactRef("code", "synthetic-invalid-target", "v1", None, "Synthetic evidence")
        assigned = fixture.assign(receipt, "a")
        for target in ("a", "c", "missing"):
            with self.subTest(target=target):
                before = self.snapshot(fixture)
                with self.assertRaises(DomainError):
                    seed(fixture, plan_id, target, artifact)
                self.assertTrue(self.snapshot(fixture) == before, "Invalid seed target changed history")
        result = seed(fixture, plan_id, "b", artifact)
        before = self.snapshot(fixture)
        with self.assertRaises(DomainError):
            seed(fixture, plan_id, "b", artifact)
        self.assertTrue(self.snapshot(fixture) == before, "Terminal target was seeded twice")
        self.assertEqual(fixture.service.read_plan(plan_id).coordination.result_versions["b"], result.result_version)
        self.assertEqual(next(a for a in fixture.service.read_plan(plan_id).coordination.attempts
                              if a.attempt_id == assigned.committed["attempt_id"]).state, "assigned")

    def test_seed_rejects_strictly_invalid_history_even_when_projection_succeeds(self):
        seed = self.seed_function()
        fixture = self.owned_fixture()
        receipt = fixture.create()
        plan_id = receipt.current_metadata.plan_id

        def invalidate(db):
            sequence, raw = db.execute("SELECT sequence,payload FROM events WHERE plan_id=?", (plan_id,)).fetchone()
            payload = json.loads(raw)
            del payload["authorization_note"]
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(payload), sequence))
        fixture.store.write(invalidate)
        self.assertIsNone(fixture.service.read_plan(plan_id).legacy_publication)
        before = self.snapshot(fixture)
        with self.assertRaises(DomainError):
            seed(fixture, plan_id, "a", ArtifactRef("code", "synthetic-invalid-history", "v1", None, "Synthetic"))
        self.assertTrue(self.snapshot(fixture) == before, "Invalid strict history was extended")

    def test_seeded_baseline_completion_does_not_consume_a_new_interval(self):
        seed = self.seed_function()
        self.f = self.owned_fixture()
        receipt = self.f.create()
        plan_id = receipt.current_metadata.plan_id
        original = self.f.service.read_plan(plan_id).draft.authorization
        seed(self.f, plan_id, "a", ArtifactRef("code", "synthetic-baseline", "baseline-v1", None, "Synthetic baseline"))
        self.assertEqual(self.f.service.read_plan(plan_id).draft.authorization, original)
        receipt = self.revise(receipt, change=AuthorizationChange("first-step", "Synthetic new interval assertion"))
        renewed = self.f.service.read_plan(plan_id).draft.authorization
        self.assertEqual((renewed.baseline, renewed.claimed), (("a",), None))
        with self.assertRaises(PlanValidationError):
            validate_execution_scope(renewed, "a")
        self.f.assign(receipt, "b")
        latched = self.f.service.read_plan(plan_id).draft.authorization
        self.assertEqual((latched.baseline, latched.claimed), (("a",), "b"))
        resumed = self.revise(receipt, reason="Resume the same stored instructions")
        self.assertEqual(self.f.service.read_plan(plan_id).draft.authorization, latched)
        with self.assertRaises(DomainError):
            self.f.assign(resumed, "c")


def fixtures_json(value):
    """Independent dataclass-to-JSON representation used in relational assertions."""
    from dataclasses import asdict
    return json.loads(json.dumps(asdict(value)))


if __name__ == "__main__":
    unittest.main()
