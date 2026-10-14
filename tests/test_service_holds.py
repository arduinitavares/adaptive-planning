"""Owner classifications and exact invalidation proof preserve immutable history."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import unittest
import uuid
from unittest.mock import patch

from adaptive_planning import attempts, checkpoints, plans
from adaptive_planning.transport import build_mcp
from adaptive_planning.types import ArtifactRef, Availability, DomainError, RetirementRequest
from tests.service_fixtures import checkpoint, context, make_service, seed_accepted_result, step, workspace


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = make_service()
        self.addCleanup(self.f.cleanup)
        self.p = self.f.create(steps=(step("a"), step("b"), step("c", ("a",)), step("d", ("c",))))
        self.plan_id = self.p.current_metadata.plan_id

    def new_plan(self, steps, mode="complete-task"):
        self.p = self.f.create(mode=mode, steps=steps, creation_id=uuid.uuid4().hex)
        self.plan_id = self.p.current_metadata.plan_id

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    def rows(self, table):
        return self.f.store.read(lambda db: tuple(db.execute(f"SELECT * FROM {table}")))

    def command(self, kind, payload, **kwargs):
        return self.f.command(kind, payload, receipt=self.p, **kwargs)

    def execute(self, command):
        try:
            return self.f.service.execute(command)
        except DomainError as error:
            if error.code == "unsupported":
                self.fail("Task16 owner hold operation is not enabled")
            raise

    def denied(self, command, code="invalid"):
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(command() if callable(command) else command)
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(self.snapshot() == before, "Rejected owner command changed durable state")

    def active(self, sid="a", execution=None):
        assigned = self.f.service.execute(self.command("assign_step", {
            "step_id": sid, "assignee": "worker-a",
            "context": context(None, writes=False) if execution is None else execution,
        }))
        dependencies = next(s.dependencies for s in self.f.service.read_plan(self.plan_id).draft.steps if s.step_id == sid)
        if dependencies:
            def inputs(db):
                plan = plans._load_plan(db, self.plan_id)
                result = []
                for prerequisite in dependencies:
                    version = db.execute("SELECT result_version FROM result_versions WHERE plan_id=? AND step_id=?",
                                         (self.plan_id, prerequisite)).fetchone()[0]
                    artifact = attempts._accepted_output(db, plan, prerequisite, version)
                    result.append(Availability(prerequisite, version, "ancestor" if artifact.kind == "code" else "observation",
                                               "synthetic-consumer-base", None, self.proof(artifact)[0]))
                return tuple(result)
            bindings = self.f.service._run_read(inputs)
            self.f.service.execute(self.command("bind_availability", {
                "bindings": bindings, "checked_base": "synthetic-consumer-base",
            }, attempt=assigned))
        cmd = self.f.command("start_step", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "Actual synthetic start",
        }, receipt=self.p, attempt=assigned)
        self.f.service.execute(replace(cmd, expected_revision=None, ownership_version=None))
        return assigned

    def publish(self, active, value):
        cmd = self.f.command("record_checkpoint", {"checkpoint": value}, receipt=self.p, attempt=active)
        return self.f.service.execute(replace(cmd, expected_revision=None, ownership_version=None))

    def classify(self, checkpoint_id, decision="hold", *, affected=("c",), unbounded=False, draft=None, reason="Owner classified actual effects"):
        return self.command("resolve_reassessment", {
            "checkpoint_id": checkpoint_id, "decision": decision, "reason": reason,
            "draft": draft, "affected_steps": affected, "unbounded": unbounded,
        })

    def invalidate_command(self, result, **updates):
        payload = {"step_id": result.step_id, "result_version": result.result_version,
                   "reason": "Synthetic invalidated assumption", "evidence": checkpoint("failed").evidence}
        return self.command("invalidate_result", {**payload, **updates})

    def invalidate(self, result):
        return self.execute(self.invalidate_command(result)).committed["invalidation_id"]

    def proof(self, artifact):
        return (replace(checkpoint().evidence[0], artifact=artifact,
                        limitations=("Synthetic fixture proof; no real execution asserted",)),)

    def resolution(self, invalidation_id, sid, result=None, *, disposition="revalidated", corrective=None, evidence=None, reason="Explicit artifact recheck"):
        return self.command("resolve_invalidation", {
            "invalidation_id": invalidation_id, "affected_step_id": sid, "disposition": disposition,
            "corrective_step_id": None if corrective is None else corrective.step_id,
            "corrected_result_version": None if result is None else result.result_version,
            "evidence": self.proof(result.artifact) if evidence is None and result is not None else (evidence or ()),
            "reason": reason,
        })

    def holds(self):
        return self.f.store.read(lambda db: tuple(plans._hold_view(h, raw) for h, raw in db.execute(
            "SELECT hold_id,reason FROM holds WHERE plan_id=? AND active=1 ORDER BY rowid", (self.plan_id,))))

    def seed_review(self, result, *, verdict="passed", first_links=None, artifact=None, reviewer="fresh-independent-reviewer", evidence=None, link=True):
        identity = str(uuid.uuid4())
        supplied = result.artifact if artifact is None else artifact
        proof = self.proof(supplied) if evidence is None else evidence
        if verdict == "failed" and evidence is None:
            proof = (replace(proof[0], result="failed", blockers=("Synthetic failed current review",)),)
        def insert(db):
            definition = json.loads(db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                (self.plan_id, result.attempt_id)).fetchone()[0])["definition"]
            encoded = lambda value: json.dumps(plans.to_jsonable(value), sort_keys=True)
            db.execute("INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,created_at) "
                       "VALUES(?,?,?,?,?,?,?, ?,?,?)", (identity, self.plan_id, result.attempt_id, result.step_id,
                       reviewer, verdict, "[]", encoded(proof), encoded(supplied), self.f.clock().isoformat()))
            links = {"review_id": identity, "step_id": result.step_id, "attempt_id": result.attempt_id,
                     "definition_version": definition["definition_version"], "definition": definition,
                     "result_version": result.result_version, "artifact": plans.to_jsonable(supplied)}
            if first_links is not None:
                links.update(first_links)
            if link:
                plans._insert_event(db, self.plan_id, None, "synthetic_fresh_review", reviewer,
                                    self.f.clock().isoformat(), links)
            return identity
        return self.f.store.write(insert)

    def note(self, payload):
        self.f.store.write(lambda db: plans._insert_event(db, self.plan_id, None, "synthetic_note",
                               "synthetic-fixture", self.f.clock().isoformat(), payload))

    def valid_output(self, result):
        return self.f.service._run_read(lambda db: attempts._accepted_output(
            db, plans._load_plan(db, self.plan_id), result.step_id, result.result_version))

    def revise(self, value):
        return self.f.service.execute(self.command("revise_plan", {
            "draft": value, "reason": "Actual graph correction", "authorization_change": None, "retire_steps": (),
        }))

    def test_held_sibling_and_transitive_consumers(self):
        self.new_plan((step("a"), step("b"), step("c", ("b",)), step("d", ("c",))))
        a = self.active("a")
        assigned_b = self.f.assign(self.p, "b")
        published = self.publish(a, checkpoint(affected=("b",)))
        self.execute(self.classify(published.committed["checkpoint_id"], affected=("b",)))
        self.assertEqual({h.step_id for h in self.holds()}, {"b", "c", "d"})
        self.assertNotIn("a", {h.step_id for h in self.holds()})
        ready = {s.step_id: s for s in self.f.service.list_ready(self.plan_id)}
        self.assertFalse(ready["b"].ready)
        self.assertFalse(ready["c"].ready)
        self.denied(self.command("assign_step", {"step_id": "c", "assignee": "worker-a", "context": context(None, writes=False)}), "held")
        start = self.f.command("start_step", {"origin_worker": "worker-a", "relay": None, "observed_activity": "Held start"},
                               receipt=self.p, attempt=assigned_b)
        self.denied(replace(start, expected_revision=None, ownership_version=None), "held")

    def test_narrowed_global_source_preserves_other_sources(self):
        a, b = self.active("a"), self.active("b")
        first = self.publish(a, checkpoint(unbounded=True)).committed["checkpoint_id"]
        second = self.publish(b, checkpoint(affected=("d",))).committed["checkpoint_id"]
        self.execute(self.classify(first, affected=("c",)))
        self.assertEqual({(h.source_id, h.step_id) for h in self.holds()}, {(first, "c"), (first, "d"), (second, "d")})
        self.execute(self.classify(first, "reject", affected=()))
        self.assertEqual({(h.source_id, h.step_id) for h in self.holds()}, {(second, "d")})

    def test_unbounded_resolution_gates_new_graph_work(self):
        a = self.active()
        identity = self.publish(a, checkpoint(affected=("c",))).committed["checkpoint_id"]
        self.execute(self.classify(identity, unbounded=True, affected=()))
        old = self.f.service.read_plan(self.plan_id).draft
        self.revise(replace(old, steps=(*old.steps, step("new"))))
        self.denied(self.command("assign_step", {"step_id": "new", "assignee": "worker-a", "context": context(None, writes=False)}), "held")

    def test_corrective_prerequisite_requires_graph_revision(self):
        a = self.active()
        value = checkpoint(affected=("c",))
        value = replace(value, reassessment=replace(value.reassessment,
            summary="Step c needs an actual corrective prerequisite, not prose"))
        identity = self.publish(a, value).committed["checkpoint_id"]
        old = self.f.service.read_plan(self.plan_id).draft
        self.denied(self.classify(identity, "apply", affected=(), draft=None))
        changed = tuple(replace(s, dependencies=("a", "fix"), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        revision = self.f.service.read_plan(self.plan_id).metadata.revision
        before_events = len(self.rows("events"))
        self.execute(self.classify(identity, "apply", affected=(), draft=replace(old, steps=(*changed, step("fix")))))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(next(s.dependencies for s in view.draft.steps if s.step_id == "c"), ("a", "fix"))
        self.assertFalse(next(s.ready for s in self.f.service.list_ready(self.plan_id) if s.step_id == "c"))
        self.assertEqual(view.metadata.revision, revision + 1)
        self.assertEqual(len(self.rows("events")), before_events + 1)
        self.assertEqual(self.f.service.read_events(self.plan_id)[-1].kind, "resolve_reassessment")

    def test_apply_preserves_active_frozen_payload_and_sibling_evidence(self):
        a, b = self.active(), self.active("b")
        identity = self.publish(a, checkpoint(affected=("c",))).committed["checkpoint_id"]
        before = self.rows("attempts")
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, title="Changed active instruction", definition_version="v2") if s.step_id == "b" else s for s in old.steps)
        self.denied(self.classify(identity, "apply", affected=(), draft=replace(old, steps=changed)))
        changed = tuple(replace(s, title="Corrected pending consumer", definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        evidence = copy.deepcopy(self.f.service.read_plan(self.plan_id).legacy_snapshot["steps"][0]["evidence"])
        self.execute(self.classify(identity, "apply", affected=(), draft=replace(old, steps=changed)))
        self.assertEqual(self.rows("attempts"), before)
        self.assertEqual(self.f.service.read_plan(self.plan_id).legacy_snapshot["steps"][0]["evidence"], evidence)
        self.assertTrue(any(row[1] == b.committed["attempt_id"] for row in before))

    def test_reject_requires_reason_and_preserves_history(self):
        result = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = self.active("b")
        identity = self.publish(b, checkpoint(affected=("c",))).committed["checkpoint_id"]
        history = tuple(self.rows(t) for t in ("attempts", "checkpoints", "reviews", "result_versions", "steps"))
        self.denied(lambda: self.classify(identity, "reject", affected=(), reason=" "))
        self.execute(self.classify(identity, "reject", affected=(), reason="The proposed correction is contradicted by retained proof"))
        self.assertEqual(tuple(self.rows(t) for t in ("attempts", "checkpoints", "reviews", "result_versions", "steps")), history)
        self.assertEqual(self.valid_output(result), result.artifact)

    def test_reassessment_exact_scoped_id_and_durable_links(self):
        identity = self.publish(self.active(), checkpoint(affected=("c",))).committed["checkpoint_id"]
        for bad in ("0", "01", "+1", "1", "unknown"):
            self.denied(self.classify(bad))
        other = self.f.create(steps=(step("x"),), creation_id="other-checkpoint-scope")
        self.denied(self.f.command("resolve_reassessment", dict(self.classify(identity).payload), receipt=other))
        def corrupt(db):
            raw = json.loads(db.execute("SELECT payload FROM events WHERE sequence=?", (int(identity),)).fetchone()[0])
            raw["step_id"] = "contradictory-step"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(raw), int(identity)))
        self.f.store.write(corrupt)
        self.denied(self.classify(identity))

    def test_reassessment_rejects_invalid_classification_and_scope_widening(self):
        identity = self.publish(self.active(), checkpoint(affected=("c",))).committed["checkpoint_id"]
        self.denied(self.classify(identity, affected=()))
        self.denied(self.classify(identity, affected=("unknown",)))
        old = self.f.service.read_plan(self.plan_id).draft
        self.denied(self.classify(identity, "hold", draft=old))
        self.denied(self.classify(identity, "reject", draft=old))
        self.denied(self.classify(identity, "apply", draft=replace(old, authorization=replace(old.authorization, mode="plan-only"))))
        self.denied(self.classify(identity, "apply", draft=replace(old, steps=old.steps[1:])))

    def test_evidence_bearing_retirement_preserves_history(self):
        self.new_plan((step("a"), step("b")), mode="first-step")
        rid = self.f.register(workspace("/synthetic/failed-owner-hold", 2101), self.p)
        a = self.active(execution=context(rid))
        self.publish(a, checkpoint("failed"))
        old = self.f.service.read_plan(self.plan_id)
        frozen = self.rows("attempts")[0]
        durable = self.rows("checkpoints")
        self.f.service.execute(self.command("revise_plan", {
            "draft": old.draft, "reason": "Explicit cancellation after retained failed proof", "authorization_change": None,
            "retire_steps": (RetirementRequest("a", "Scope no longer needs the failed operation"),),
        }))
        view = self.f.service.read_plan(self.plan_id)
        node = next(s for s in view.legacy_snapshot["steps"] if s["id"] == "a")
        self.assertEqual(node["status"], "retired")
        self.assertEqual(node["evidence"], old.legacy_snapshot["steps"][0]["evidence"])
        self.assertEqual(node["check"], old.legacy_snapshot["steps"][0]["check"])
        after = self.rows("attempts")[0]
        self.assertEqual(after[4], "released")
        before_payload, after_payload = json.loads(frozen[-1]), json.loads(after[-1])
        self.assertEqual({key: after_payload[key] for key in before_payload}, before_payload)
        self.assertEqual(self.rows("checkpoints"), durable)
        self.assertEqual(view.draft.authorization.claimed, "a")
        self.assertTrue(all(row[6] == "unresolved" for row in self.rows("resource_uses")))
        self.denied(self.command("assign_step", {"step_id": "b", "assignee": "worker-a", "context": context(None, writes=False)}), "forbidden")

    def test_seeded_result_invalidation_holds_consumers(self):
        artifact = ArtifactRef("observation", "synthetic-observation", "v1", None, "durable measurement")
        a = seed_accepted_result(self.f, self.plan_id, "a", artifact)
        accepted_before = tuple(self.rows(t) for t in ("attempts", "checkpoints", "reviews", "result_versions", "steps"))
        identity = self.invalidate(a)
        self.assertEqual({(h.source_id, h.step_id) for h in self.holds()}, {(identity, "a"), (identity, "c"), (identity, "d")})
        self.assertEqual(tuple(self.rows(t) for t in ("attempts", "checkpoints", "reviews", "result_versions", "steps")), accepted_before)
        self.assertEqual(next(s["status"] for s in self.f.service.read_plan(self.plan_id).legacy_snapshot["steps"] if s["id"] == "a"), "complete")
        self.assertFalse(next(s.ready for s in self.f.service.list_ready(self.plan_id) if s.step_id == "c"))
        with self.assertRaises(DomainError) as caught:
            self.valid_output(a)
        self.assertEqual(caught.exception.code, "held")

    def test_invalidation_holds_accepted_consumers_and_running_descendants(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        c = seed_accepted_result(self.f, self.plan_id, "c", checkpoint().artifact)
        d = self.active("d")
        before = self.rows("attempts")
        identity = self.invalidate(a)
        self.assertEqual({h.step_id for h in self.holds() if h.source_id == identity}, {"a", "c", "d"})
        self.assertEqual(self.rows("attempts"), before)
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.coordination.result_versions["c"], c.result_version)
        self.assertTrue(any(v.attempt_id == d.committed["attempt_id"] and v.state == "active" for v in view.coordination.attempts))

    def test_invalidation_requires_exact_history_reason_and_nonempty_evidence(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        for updates in ({"reason": " "}, {"evidence": ()}, {"evidence": (replace(checkpoint().evidence[0], check=" "),)},
                        {"result_version": a.artifact.version}, {"step_id": "b"}, {"result_version": "unknown"}):
            self.denied(lambda: self.invalidate_command(a, **updates))
        def corrupt(db):
            raw = json.loads(db.execute("SELECT payload FROM attempts WHERE attempt_id=?", (a.attempt_id,)).fetchone()[0])
            raw["artifact"]["version"] = "different-artifact-version"
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(raw), a.attempt_id))
        self.f.store.write(corrupt)
        self.denied(self.invalidate_command(a))

    def test_old_availability_is_rejected_after_producer_supersession(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        fix = seed_accepted_result(self.f, self.plan_id, "b", replace(checkpoint().artifact, version="corrected"))
        assigned = self.f.assign(self.p, "c")
        identity = self.invalidate(a)
        self.execute(self.resolution(identity, "a", fix, disposition="superseded", corrective=fix))
        binding = Availability("a", a.result_version, "ancestor", "consumer-base", None, self.proof(a.artifact)[0])
        self.denied(self.command("bind_availability", {"bindings": (binding,), "checked_base": "consumer-base"}, attempt=assigned), "held")
        self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.result_versions["a"], a.result_version)
        with self.assertRaises(DomainError) as caught:
            self.valid_output(a)
        self.assertEqual(caught.exception.code, "held")

    def test_seeded_supersession_and_missing_proof_preserve_history(self):
        self.new_plan((step("a", review_required=True), step("b", ("a",), review_required=True), step("fix", review_required=True)))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = seed_accepted_result(self.f, self.plan_id, "b", checkpoint().artifact)
        fix = seed_accepted_result(self.f, self.plan_id, "fix", replace(checkpoint().artifact, version="corrected"))
        before = copy.deepcopy(self.f.service.read_plan(self.plan_id).legacy_snapshot["steps"])
        identity = self.invalidate(a)
        self.denied(self.resolution(identity, "b", b))
        self.execute(self.resolution(identity, "b", fix, disposition="superseded", corrective=fix))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.legacy_snapshot["steps"], before)
        self.assertEqual(view.coordination.result_versions["b"], b.result_version)
        self.assertEqual({h.step_id for h in self.holds()}, {"a"})

    def test_revalidated_consumer_cannot_restore_producer_or_descendant(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        c = seed_accepted_result(self.f, self.plan_id, "c", checkpoint().artifact)
        identity = self.invalidate(a)
        self.execute(self.resolution(identity, "c", c))
        self.assertEqual({h.step_id for h in self.holds()}, {"a", "d"})
        self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.result_versions["c"], c.result_version)
        with self.assertRaises(DomainError):
            self.valid_output(a)

    def test_only_explicit_producer_revalidation_restores_exact_output(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        self.execute(self.resolution(identity, "a", a))
        self.assertEqual(self.valid_output(a), a.artifact)
        self.assertEqual({h.step_id for h in self.holds()}, {"c", "d"})
        self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.result_versions["a"], a.result_version)

    def test_newer_invalidation_remains_independent(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        first, second = self.invalidate(a), self.invalidate(a)
        self.execute(self.resolution(first, "a", a))
        self.assertTrue(any(h.source_id == second and h.step_id == "a" for h in self.holds()))
        with self.assertRaises(DomainError):
            self.valid_output(a)
        self.execute(self.resolution(second, "a", a))
        self.assertEqual(self.valid_output(a), a.artifact)
        self.assertEqual({(h.source_id, h.step_id) for h in self.holds()}, {(first, "c"), (first, "d"), (second, "c"), (second, "d")})

    def test_resolution_rejects_unknown_unaffected_wrong_plan_and_false_proof(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        self.denied(self.resolution("unknown", "a", a))
        self.denied(self.resolution(identity, "b", a))
        other = self.f.create(steps=(step("x"),), creation_id="wrong-invalidation-scope")
        self.denied(self.f.command("resolve_invalidation", dict(self.resolution(identity, "a", a).payload), receipt=other))
        evidence_cases = ((), checkpoint(artifact_version="unrelated").evidence,
            (replace(self.proof(a.artifact)[0], result="failed"),), (replace(self.proof(a.artifact)[0], blockers=("unresolved",)),),
            (replace(self.proof(a.artifact)[0], artifact=None),), (replace(self.proof(a.artifact)[0], summary=" "),))
        for evidence in evidence_cases:
            self.denied(self.resolution(identity, "a", a, evidence=evidence))
        self.denied(lambda: self.resolution(identity, "a", a, reason=" "))
        self.denied(self.resolution(identity, "a", a, corrective=a))
        self.denied(self.resolution(identity, "a", a, disposition="superseded", corrective=a))

    def test_supersession_requires_valid_distinct_corrective_result(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = seed_accepted_result(self.f, self.plan_id, "b", replace(checkpoint().artifact, version="corrected"))
        identity = self.invalidate(a)
        self.denied(self.resolution(identity, "a", a, disposition="superseded", corrective=b))
        self.invalidate(b)
        self.denied(self.resolution(identity, "a", b, disposition="superseded", corrective=b), "held")

    def test_review_requires_first_original_audit_link_after_invalidation(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        old_id = self.f.store.read(lambda db: db.execute("SELECT review_id FROM reviews WHERE attempt_id=?", (a.attempt_id,)).fetchone()[0])
        identity = self.invalidate(a)
        self.note({"review_id": old_id, "summary": "Later mention cannot refresh old proof"})
        self.denied(self.resolution(identity, "a", a))
        self.seed_review(a)
        self.execute(self.resolution(identity, "a", a))
        self.assertEqual(self.valid_output(a), a.artifact)

    def test_review_rowid_and_timestamp_are_not_audit_chronology(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        for index in range(110):
            self.note({"summary": f"Unrelated synthetic event {index}"})
        identity = self.invalidate(a)
        self.seed_review(a)
        rowid = self.f.store.read(lambda db: db.execute("SELECT MAX(rowid) FROM reviews").fetchone()[0])
        sequence = next(e.sequence for e in self.f.service.read_events(self.plan_id, limit=1000) if e.payload.get("invalidation_id") == identity)
        self.assertLess(rowid, sequence)
        self.execute(self.resolution(identity, "a", a))
        self.assertEqual(self.valid_output(a), a.artifact)

    def test_contradictory_first_review_link_and_missing_link_cannot_be_freshened(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        bad = self.seed_review(a, first_links={"attempt_id": "contradictory-attempt"})
        self.note({"review_id": bad})
        self.seed_review(a, link=False)
        self.denied(self.resolution(identity, "a", a))
        self.seed_review(a)
        self.execute(self.resolution(identity, "a", a))

    def test_latest_current_failed_review_cannot_be_bypassed_by_older_pass(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        passed = self.seed_review(a)
        self.seed_review(a, verdict="failed")
        self.note({"review_id": passed})
        self.denied(self.resolution(identity, "a", a))
        self.seed_review(a)
        self.execute(self.resolution(identity, "a", a))

    def test_review_exact_artifact_distinct_reviewer_and_blocker_free_proof(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        self.seed_review(a, artifact=replace(a.artifact, version="different"))
        self.seed_review(a, reviewer="synthetic-history-worker")
        self.seed_review(a, evidence=(replace(self.proof(a.artifact)[0], blockers=("unresolved",)),))
        self.denied(self.resolution(identity, "a", a))
        self.seed_review(a)
        self.execute(self.resolution(identity, "a", a))

    def test_later_failed_review_does_not_rewrite_accepted_history(self):
        self.new_plan((step("a", review_required=True), step("c", ("a",))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        history = tuple(self.rows(t) for t in ("attempts", "checkpoints", "result_versions", "steps"))
        self.seed_review(a, verdict="failed")
        self.assertEqual(self.valid_output(a), a.artifact)
        self.assertEqual(tuple(self.rows(t) for t in ("attempts", "checkpoints", "result_versions", "steps")), history)
        self.assertEqual(self.holds(), ())

    def test_replanned_requires_actual_graph_change_and_valid_correction(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = seed_accepted_result(self.f, self.plan_id, "b", replace(checkpoint().artifact, version="corrected"))
        identity = self.invalidate(a)
        self.denied(self.resolution(identity, "c", b, disposition="replanned", corrective=b))
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=("b",), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        self.execute(self.resolution(identity, "c", b, disposition="replanned", corrective=b))
        self.assertEqual({h.step_id for h in self.holds()}, {"a", "d"})
        self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.result_versions["a"], a.result_version)

    def test_replanned_removal_does_not_require_a_fabricated_corrective_result(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        evidence = (replace(checkpoint().evidence[0], artifact=None, summary="Actual revised c removes the invalid prerequisite"),)
        self.execute(self.resolution(identity, "c", disposition="replanned", evidence=evidence))
        self.assertTrue(next(s.ready for s in self.f.service.list_ready(self.plan_id) if s.step_id == "c"))
        self.assertEqual({h.step_id for h in self.holds()}, {"a", "d"})

    def test_replanned_transitive_route_can_change_at_intermediate_prerequisite(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        evidence = (replace(checkpoint().evidence[0], artifact=None, summary="Actual c revision removes d's indirect invalid prerequisite"),)
        self.execute(self.resolution(identity, "d", disposition="replanned", evidence=evidence))
        current = self.f.service.read_plan(self.plan_id)
        self.assertEqual(next(s for s in current.draft.steps if s.step_id == "d"), next(s for s in old.steps if s.step_id == "d"))
        self.assertEqual({h.step_id for h in self.holds()}, {"a", "c"})

    def test_unrelated_revision_cannot_authorize_an_unrecorded_dependency_edit(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        def unrecorded(db):
            value = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='c'", (self.plan_id,)).fetchone()[0])
            value["definition"]["dependencies"] = []
            db.execute("UPDATE steps SET payload=? WHERE plan_id=? AND step_id='c'", (json.dumps(value), self.plan_id))
        self.f.store.write(unrecorded)
        current = self.f.service.read_plan(self.plan_id).draft
        self.revise(replace(current, facts=("Unrelated newer metadata change",)))
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        self.denied(self.resolution(identity, "c", disposition="replanned", evidence=evidence))

    def test_replanned_rejects_false_preceding_dependency_provenance(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        def corrupt(db):
            row = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='revise_plan' ORDER BY sequence DESC LIMIT 1", (self.plan_id,)).fetchone()
            value = json.loads(row[1])
            value["prior_dependencies"] = {"c": []}
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(value), row[0]))
        self.f.store.write(corrupt)
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        self.denied(self.resolution(identity, "c", disposition="replanned", evidence=evidence))

    def test_later_metadata_edits_preserve_real_replanned_edge_proof(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        current = self.f.service.read_plan(self.plan_id).draft
        cosmetic = tuple(replace(s, title="Later clarified consumer title", definition_version="v3") if s.step_id == "c" else s for s in current.steps)
        self.revise(replace(current, steps=cosmetic))
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        self.execute(self.resolution(identity, "c", disposition="replanned", evidence=evidence))
        self.assertEqual({h.step_id for h in self.holds()}, {"a", "d"})

    def test_replanned_requires_later_actual_plan_revision_provenance(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        invalid_revision = self.f.service.read_plan(self.plan_id).metadata.revision
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        current_revision = self.f.service.read_plan(self.plan_id).metadata.revision
        def corrupt(db):
            db.execute("UPDATE events SET plan_revision=? WHERE plan_id=? AND kind='revise_plan'", (invalid_revision, self.plan_id))
            plans._insert_event(db, self.plan_id, current_revision, "synthetic_unrelated_revision", "synthetic-fixture",
                                self.f.clock().isoformat(), {"summary": "Unrelated event cannot prove a dependency transition"})
        self.f.store.write(corrupt)
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        self.denied(self.resolution(identity, "c", disposition="replanned", evidence=evidence))

    def test_replanned_removal_rejects_unbound_artifact_and_mixed_correction_fields(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        self.denied(self.resolution(identity, "c", disposition="replanned", evidence=checkpoint().evidence))
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        payload = dict(self.resolution(identity, "c", disposition="replanned", evidence=evidence).payload)
        self.denied(self.command("resolve_invalidation", {**payload, "corrective_step_id": "b"}))
        self.denied(self.command("resolve_invalidation", {**payload, "corrected_result_version": a.result_version}))
        self.execute(self.command("resolve_invalidation", payload))

    def test_reassessment_apply_audits_only_actual_changed_definitions(self):
        identity = self.publish(self.active(), checkpoint(affected=("c",))).committed["checkpoint_id"]
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=("b",), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.execute(self.classify(identity, "apply", affected=(), draft=replace(old, steps=changed)))
        event = self.f.service.read_events(self.plan_id)[-1]
        def definitions(value):
            if isinstance(value, dict):
                return ([value] if {"step_id", "dependencies", "definition_version"} <= set(value) else []) + [
                    node for child in value.values() for node in definitions(child)]
            if isinstance(value, (list, tuple)):
                return [node for child in value for node in definitions(child)]
            return []
        self.assertEqual([node["step_id"] for node in definitions(event.payload)], ["c"])
        self.assertEqual(event.payload["prior_dependencies"], {"c": ["a"]})

    def test_replanned_cannot_bypass_indirect_invalid_prerequisite_or_active_definition(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        assigned = self.active("c")
        identity = self.invalidate(a)
        evidence = (replace(checkpoint().evidence[0], artifact=None),)
        self.denied(self.resolution(identity, "c", disposition="replanned", evidence=evidence))
        old = self.f.service.read_plan(self.plan_id).draft
        changed = tuple(replace(s, dependencies=(), definition_version="v2") if s.step_id == "c" else s for s in old.steps)
        self.denied(self.command("revise_plan", {"draft": replace(old, steps=changed), "reason": "Attempted active edit",
                    "authorization_change": None, "retire_steps": ()}))
        self.assertTrue(any(row[1] == assigned.committed["attempt_id"] and row[4] == "active" for row in self.rows("attempts")))
        changed = tuple(replace(s, title="A prose change to d", definition_version="v2") if s.step_id == "d" else s for s in old.steps)
        self.revise(replace(old, steps=changed))
        self.denied(self.resolution(identity, "d", disposition="replanned", evidence=evidence))

    def test_authoritative_closure_and_resolution_exceed_public_summaries(self):
        self.new_plan((step("a"), *(step(f"c{i}", ("a",)) for i in range(60))))
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        identity = self.invalidate(a)
        view = self.f.service.read_plan(self.plan_id)
        self.assertLess(len(view.coordination.holds), 61)
        self.assertEqual(len(self.holds()), 61)
        for index in range(110):
            self.note({"summary": f"Newer unrelated event {index}"})
        self.execute(self.resolution(identity, "a", a))
        self.assertEqual(len(self.holds()), 60)
        self.assertEqual(self.valid_output(a), a.artifact)

    def test_all_owner_operations_authenticate_before_replay_and_use_current_cas(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = self.active("b")
        checkpoint_id = self.publish(b, checkpoint(affected=("c",))).committed["checkpoint_id"]
        invalidate = self.invalidate_command(a)
        self.denied(replace(invalidate, actor="another-label", orchestrator_token=None), "forbidden")
        self.denied(replace(invalidate, attempt_token="forbidden-extra"))
        self.denied(replace(invalidate, epoch="old-epoch"), "stale_epoch")
        self.denied(replace(invalidate, generation="old-generation"), "stale")
        self.denied(replace(invalidate, ownership_version=0), "conflict")
        self.denied(replace(invalidate, expected_revision=1), "conflict")
        receipt = self.execute(replace(invalidate, actor="capability-holder-label"))
        commands = [replace(invalidate, actor="capability-holder-label"), self.classify(checkpoint_id, "reject", affected=()),
                    self.resolution(receipt.committed["invalidation_id"], "a", a)]
        committed = [receipt, self.execute(commands[1])]
        commands[2] = self.resolution(receipt.committed["invalidation_id"], "a", a)
        committed.append(self.execute(commands[2]))
        before = self.snapshot()
        for cmd, original in zip(commands, committed):
            replayed = self.execute(cmd)
            self.assertTrue(replayed.replayed)
            self.assertEqual(replayed.committed_metadata, original.committed_metadata)
            self.assertEqual(replayed.committed, original.committed)
            self.assertTrue(self.snapshot() == before)
            self.denied(replace(cmd, key=uuid.uuid4().hex), "conflict")
        takeover = self.f.service.execute(self.command("takeover", {"expected_ownership_version": 1, "reason": "Explicit synthetic owner change"}))
        self.assertEqual(takeover.current_metadata.ownership_version, 2)
        for cmd in commands:
            self.denied(cmd, "fenced")

    def test_failure_after_mutation_rolls_back_each_owner_operation(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = self.active("b")
        checkpoint_id = self.publish(b, checkpoint(affected=("c",))).committed["checkpoint_id"]
        commands = [self.classify(checkpoint_id, "reject", affected=()), self.invalidate_command(a)]
        for cmd in commands:
            before = self.snapshot()
            with patch.object(plans, "_commit_projection", side_effect=RuntimeError("Synthetic publication failure")):
                with self.assertRaisesRegex(RuntimeError, "Synthetic publication failure"):
                    self.execute(cmd)
            self.assertTrue(self.snapshot() == before)
        identity = self.invalidate(a)
        before = self.snapshot()
        with patch.object(plans, "_commit_projection", side_effect=RuntimeError("Synthetic publication failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic publication failure"):
                self.execute(self.resolution(identity, "a", a))
        self.assertTrue(self.snapshot() == before)

    def test_mcp_owner_routes_strict_payload_and_kind(self):
        a = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        b = self.active("b")
        checkpoint_id = self.publish(b, checkpoint(affected=("c",))).committed["checkpoint_id"]
        server = build_mcp(self.f.service)
        async def exercise():
            names = {t.name for t in await server.list_tools()}
            self.assertTrue({"resolve_reassessment", "invalidate_result", "resolve_invalidation"} <= names)
            raw = plans.to_jsonable(self.invalidate_command(a))
            bad = copy.deepcopy(raw)
            bad["payload"]["unexpected"] = "secret-like-synthetic-input"
            before = self.snapshot()
            result = await server.call_tool("invalidate_result", {"command": bad})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error"]["code"], "invalid")
            self.assertNotIn("secret-like-synthetic-input", str(result.structured_content))
            self.assertTrue(self.snapshot() == before)
            result = await server.call_tool("resolve_invalidation", {"command": raw})
            self.assertTrue(result.is_error)
            result = await server.call_tool("invalidate_result", {"command": raw})
            self.assertFalse(result.is_error)
            invalidation_id = result.structured_content["committed"]["invalidation_id"]
            classify = plans.to_jsonable(self.classify(checkpoint_id, "reject", affected=()))
            classify["payload"]["unbounded"] = "false"
            result = await server.call_tool("resolve_reassessment", {"command": classify})
            self.assertTrue(result.is_error)
            classify["payload"]["unbounded"] = False
            result = await server.call_tool("resolve_reassessment", {"command": classify})
            self.assertFalse(result.is_error)
            resolve = plans.to_jsonable(self.resolution(invalidation_id, "a", a))
            result = await server.call_tool("resolve_invalidation", {"command": resolve})
            self.assertFalse(result.is_error)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
