"""Consumer-scoped accepted inputs and authenticated semantic worker progress."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch

from adaptive_planning import plans
from adaptive_planning.app import PlanningService
from adaptive_planning.ownership import hash_token
from adaptive_planning.store import Store
from adaptive_planning.types import ArtifactRef, Availability, DomainError, Evidence
from tests.service_fixtures import context, make_service, seed_accepted_result, seed_orphan_use, step, workspace


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = make_service()
        self.addCleanup(self.f.cleanup)
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    def execute(self, command):
        try:
            return self.f.service.execute(command)
        except DomainError as error:
            if error.code == "unsupported":
                self.fail(f"Task14 semantic operation {command.kind} is not enabled")
            raise

    def denied(self, command, codes=("invalid",)):
        before = self.snapshot()
        with self.assertRaises(DomainError) as caught:
            self.f.service.execute(command)
        self.assertIn(caught.exception.code, codes)
        self.assertTrue(self.snapshot() == before, "Rejected operation changed persisted state")

    def assign(self, step_id="a", *, execution=None, assignee="worker-a"):
        return self.execute(self.f.command("assign_step", {
            "step_id": step_id, "assignee": assignee,
            "context": context(None, writes=False) if execution is None else execution,
        }, receipt=self.p))

    def worker(self, assigned, kind="start_step", *, relay=None, origin="worker-a", observation="Observed execution"):
        command = self.f.command(kind, {
            "origin_worker": origin, "relay": relay, "observed_activity": observation,
        }, receipt=self.p, attempt=assigned)
        return replace(command, actor=origin if relay is None else relay,
                       expected_revision=None if relay is None else command.expected_revision,
                       ownership_version=None if relay is None else command.ownership_version)

    def bind_command(self, assigned, bindings, base="final-base"):
        return self.f.command("bind_availability", {"bindings": tuple(bindings), "checked_base": base},
                              receipt=self.p, attempt=assigned)

    def bind(self, assigned, bindings, base="final-base"):
        return self.execute(self.bind_command(assigned, bindings, base))

    def artifact(self, kind="observation", *, identity="durable-input", version="artifact-v7", digest=None):
        return ArtifactRef(kind, identity, version, digest, "Retained output with essential evidence")

    def seed(self, step_id="a", artifact=None):
        return seed_accepted_result(self.f, self.plan_id, step_id, self.artifact() if artifact is None else artifact)

    def binding(self, result, *, base="final-base", method="observation", artifact=None, result_kind="passed",
                digest=None, workspace_id=None):
        supplied = result.artifact if artifact is None else artifact
        evidence = Evidence("Input available at the final execution base", supplied, workspace_id,
                            "Verified availability of the selected accepted result at this base", result_kind,
                            ("Host recorded this attestation; service does not inspect output paths.",), ())
        return Availability(result.step_id, result.result_version, method, base, digest, evidence)

    def attempt(self, assigned):
        return next(a for a in self.f.service.read_plan(self.plan_id).coordination.attempts
                    if a.attempt_id == assigned.committed["attempt_id"])

    def payload(self, assigned):
        return self.f.store.read(lambda db: json.loads(db.execute(
            "SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
            (self.plan_id, assigned.committed["attempt_id"])).fetchone()[0]))

    def edit_payload(self, attempt_id, update):
        def edit(db):
            data = json.loads(db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                         (self.plan_id, attempt_id)).fetchone()[0])
            update(data)
            db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data), self.plan_id, attempt_id))
        self.f.store.write(edit)

    def hold(self, step_id=None):
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO holds(hold_id,plan_id,reason) VALUES('task14-hold',?,?)",
            (self.plan_id, json.dumps({"kind": "invalidation", "step_id": step_id,
                                      "source_id": "task14-source", "reason": "Unresolved observation"}))))

    def configured_service(self, seconds):
        try:
            return PlanningService(self.f.store, base_url="http://127.0.0.1:8790",
                                   clock=self.f.clock, stale_seconds=seconds)
        except TypeError:
            self.fail("PlanningService does not expose the configured inactivity policy")

    def history_event(self, kind, payload, *, actor="synthetic-orchestrator"):
        def insert(db):
            plans._insert_event(db, self.plan_id, None, kind, actor, self.f.clock().isoformat(), payload)
            return plans._store_sequence(db)
        return self.f.service._run_write(insert)

    def invalidate(self, result, *, invalidation_id="invalidated-input"):
        return self.history_event("invalidate_result", {
            "invalidation_id": invalidation_id, "producer_step_id": result.step_id,
            "result_version": result.result_version, "affected_steps": [result.step_id, "c"],
            "reason": "Changed output requires explicit producer revalidation",
        })

    def revalidate(self, result, *, affected="a", disposition="revalidated", artifact=None,
                   result_version=None, invalidation_id="invalidated-input"):
        evidence = replace(self.binding(result).evidence,
                           artifact=result.artifact if artifact is None else artifact)
        return self.history_event("resolve_invalidation", {
            "invalidation_id": invalidation_id, "affected_step_id": affected, "disposition": disposition,
            "corrective_step_id": None,
            "corrected_result_version": result.result_version if result_version is None else result_version,
            "evidence": plans.to_jsonable((evidence,)), "reason": "Explicit recorded disposition",
        })

    def test_dependents_need_final_checked_base_bindings(self):
        result = self.seed()
        consumer = self.assign("c")
        original_context = self.attempt(consumer).context
        self.assertNotEqual(result.result_version, result.artifact.version)
        self.denied(self.worker(consumer))
        self.denied(self.bind_command(consumer, (replace(self.binding(result), result_version="unknown-result"),)))
        bound = self.bind(consumer, (self.binding(result, base="observed-base"),), "observed-base")
        prepared = self.attempt(consumer)
        self.assertEqual(prepared.context, replace(original_context, checked_base="observed-base"))
        self.assertEqual(bound.committed["version"], 2)
        started = self.execute(self.worker(consumer))
        self.assertEqual(started.committed["version"], 3)
        self.assertEqual(self.attempt(consumer).state, "active")
        self.assertEqual(next(s["status"] for s in self.f.service.read_plan(self.plan_id).legacy_snapshot["steps"]
                              if s["id"] == "c"), "in-progress")

    def test_worker_version_not_global_revision(self):
        a, b = self.assign("a"), self.assign("b", assignee="worker-b")
        a_command = self.worker(a)
        self.execute(self.worker(b, origin="worker-b"))
        self.execute(self.worker(b, "report_activity", origin="worker-b", observation="Completed a real scan"))
        started = self.execute(a_command)
        self.assertEqual(started.committed["version"], 2)
        self.assertEqual({a.state for a in self.f.service.read_plan(self.plan_id).coordination.attempts}, {"active"})
        self.assertEqual(self.attempt(b).version, 3)

    def test_relay_records_origin_and_relay(self):
        assigned = self.assign()
        start = self.execute(self.worker(assigned, relay="declared-relay", observation="Read the actual input"))
        self.execute(self.worker(assigned, "report_activity", relay="declared-relay", observation="Ran the declared check"))
        events = [e for e in self.f.service.read_events(self.plan_id) if e.kind in ("start_step", "report_activity")]
        self.assertEqual([(e.actor, e.origin_worker, e.relay) for e in events],
                         [("declared-relay", "worker-a", "declared-relay")] * 2)
        self.assertEqual([e.payload["observed_activity"] for e in events],
                         ["Read the actual input", "Ran the declared check"])
        self.assertEqual(start.committed_metadata.revision + 1, events[-1].plan_revision)
        self.assertEqual(self.attempt(assigned).version, 3)

    def test_direct_progress_records_columns_observation_and_own_timestamp(self):
        assigned = self.assign()
        self.f.clock.advance(3)
        self.execute(self.worker(assigned, observation="Read durable input"))
        self.f.clock.advance(4)
        self.execute(self.worker(assigned, "report_activity", observation="Measured seven samples"))
        events = [e for e in self.f.service.read_events(self.plan_id) if e.kind in ("start_step", "report_activity")]
        self.assertEqual([(e.actor, e.origin_worker, e.relay) for e in events], [("worker-a", "worker-a", None)] * 2)
        self.assertEqual(events[-1].payload["observed_activity"], "Measured seven samples")
        self.assertEqual(self.attempt(assigned).last_activity, self.f.clock().isoformat())
        self.assertEqual(self.attempt(assigned).state, "active")

    def test_two_consumers_keep_different_bases_after_reopen(self):
        self.p = self.f.create(steps=(step("a"), step("c", ("a",)), step("d", ("a",))), creation_id="two-consumers")
        self.plan_id = self.p.current_metadata.plan_id
        result = self.seed()
        c, d = self.assign("c"), self.assign("d")
        self.bind(c, (self.binding(result, base="base-c"),), "base-c")
        c_payload, c_version = self.payload(c), self.attempt(c).version
        self.bind(d, (self.binding(result, base="base-d"),), "base-d")
        self.assertTrue(self.payload(c) == c_payload, "Another consumer overwrote retained bindings")
        self.assertEqual(self.attempt(c).version, c_version)
        self.f.service = PlanningService(Store(self.f.store.path, self.f.clock),
                                        base_url="http://127.0.0.1:8790", clock=self.f.clock)
        self.execute(self.worker(d))
        self.execute(self.worker(c))
        self.assertEqual((self.attempt(c).context.checked_base, self.attempt(d).context.checked_base), ("base-c", "base-d"))

    def test_global_availability_row_is_not_consumer_authority(self):
        result = self.seed()
        consumer = self.assign("c", execution=replace(context(None, writes=False), checked_base="final-base"))
        binding = self.binding(result)
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO availability(plan_id,prerequisite,result_version,method,checked_base,evidence) VALUES(?,?,?,?,?,?)",
            (self.plan_id, "a", result.result_version, "observation", "final-base", json.dumps(plans.to_jsonable(binding.evidence)))))
        self.denied(self.worker(consumer))

    def test_partial_bindings_merge_only_at_the_same_final_base(self):
        self.p = self.f.create(steps=(step("a"), step("b"), step("c", ("a", "b"))), creation_id="multiple-inputs")
        self.plan_id = self.p.current_metadata.plan_id
        a, b = self.seed("a"), self.seed("b", self.artifact(identity="other-input"))
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(a),))
        self.denied(self.worker(consumer))
        self.denied(self.bind_command(consumer, (self.binding(a), self.binding(b, base="different-base"))))
        self.bind(consumer, (self.binding(b, base="changed-base"),), "changed-base")
        self.denied(self.worker(consumer))
        self.bind(consumer, (self.binding(a, base="changed-base"),), "changed-base")
        self.execute(self.worker(consumer))
        self.assertEqual(self.attempt(consumer).context.checked_base, "changed-base")

    def test_invalid_binding_batches_are_atomic(self):
        result = self.seed()
        consumer = self.assign("c")
        valid = self.binding(result)
        other_plan = self.f.create(creation_id="foreign-plan")
        foreign = seed_accepted_result(self.f, other_plan.current_metadata.plan_id, "a", self.artifact())
        for bindings in ((), (valid, valid), (valid, replace(valid, prerequisite="b")),
                         (valid, replace(valid, prerequisite="b", checked_base="other-base")),
                         (replace(valid, result_version=foreign.result_version),),
                         (replace(valid, result_version=result.artifact.version),),
                         (replace(valid, prerequisite=" "),), (replace(valid, checked_base=" "),)):
            with self.subTest(entries=len(bindings)):
                self.denied(self.bind_command(consumer, bindings))

    def test_bind_checks_target_cas_definition_and_does_not_refresh_activity(self):
        result = self.seed()
        consumer, sibling = self.assign("c"), self.assign("b")
        command = self.bind_command(consumer, (self.binding(result),))
        self.execute(self.worker(sibling, "report_activity"))
        self.denied(command, ("conflict",))
        self.denied(replace(self.bind_command(consumer, (self.binding(result),)), attempt_version=0), ("stale",))
        before = self.attempt(consumer).last_activity
        self.edit_payload(consumer.committed["attempt_id"], lambda data: data.update(unrelated={"retained": True}))
        self.f.clock.advance(7)
        self.bind(consumer, (self.binding(result),))
        self.assertEqual(self.attempt(consumer).last_activity, before)
        self.assertEqual(self.payload(consumer)["unrelated"], {"retained": True})
        self.execute(self.worker(consumer))
        self.denied(self.bind_command(consumer, (self.binding(result),)))

    def test_bind_requires_exact_same_plan_assigned_target(self):
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        self.denied(replace(command, attempt_id=None))
        self.denied(replace(command, attempt_id="absent-attempt"), ("not_found",))
        self.denied(replace(command, definition_version="other"), ("stale",))
        self.edit_payload(consumer.committed["attempt_id"], lambda data: data.update(recovery_required=True))
        self.denied(command, ("held",))

    def test_all_integration_methods_preserve_exact_artifact_versions(self):
        for method in ("ancestor", "merge", "cherry-pick", "equivalent", "observation"):
            with self.subTest(method=method):
                f = make_service()
                self.addCleanup(f.cleanup)
                self.f, self.p = f, f.create()
                self.plan_id = self.p.current_metadata.plan_id
                artifact = self.artifact("observation" if method == "observation" else "code", digest="original-bytes")
                result = self.seed(artifact=artifact)
                consumer = self.assign("c")
                supplied = replace(artifact, identity="equivalent-delivery", version="integrated-v2", digest="different-bytes") if method == "equivalent" else artifact
                binding = self.binding(result, method=method, artifact=supplied,
                                       digest=supplied.digest if method == "equivalent" else None)
                self.bind(consumer, (binding,))
                self.execute(self.worker(consumer))
                self.assertEqual(self.attempt(consumer).state, "active")

    def test_observation_accepts_document_data_and_observation_results(self):
        for kind in ("document", "data", "observation"):
            with self.subTest(kind=kind):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                self.p = self.f.create()
                self.plan_id = self.p.current_metadata.plan_id
                result = self.seed(artifact=self.artifact(kind))
                consumer = self.assign("c")
                self.bind(consumer, (self.binding(result, result_kind="observation"),))
                self.execute(self.worker(consumer))

    def test_malformed_attestations_cannot_supply_availability(self):
        result = self.seed(artifact=self.artifact("code", digest="original"))
        consumer = self.assign("c")
        valid = self.binding(result, method="equivalent", digest="original")
        variants = (replace(valid, artifact_digest=None), replace(valid, artifact_digest="mismatch"),
                    replace(valid, evidence=replace(valid.evidence, artifact=replace(result.artifact, kind="data"))),
                    replace(valid, evidence=replace(valid.evidence, result="failed")),
                    replace(valid, evidence=replace(valid.evidence, result="blocked")),
                    replace(valid, evidence=replace(valid.evidence, result="observation")),
                    replace(valid, evidence=replace(valid.evidence, blockers=("Unresolved input",))),
                    replace(valid, evidence=replace(valid.evidence, summary=" ")),
                    replace(valid, evidence=replace(valid.evidence, check=" ")),
                    replace(valid, method="observation"),
                    replace(valid, method="ancestor", evidence=replace(valid.evidence, artifact=replace(result.artifact, version="wrong-version"))))
        for binding in variants:
            with self.subTest(method=binding.method, result=binding.evidence.result):
                self.denied(self.bind_command(consumer, (binding,)))

    def test_observation_requires_exact_artifact_and_consumer_workspace(self):
        result = self.seed()
        ws = self.f.register(workspace("/synthetic/consumer", 71), self.p)
        other = self.f.register(workspace("/synthetic/unrelated", 72), self.p)
        consumer = self.assign("c", execution=context(ws, writes=False))
        self.denied(self.bind_command(consumer, (self.binding(result, artifact=replace(result.artifact, version="wrong-version")),)))
        self.denied(self.bind_command(consumer, (self.binding(result, workspace_id=other),)))
        self.bind(consumer, (self.binding(result, workspace_id=ws),))
        self.execute(self.worker(consumer))

    def test_accepted_output_needs_coherent_checkpoint_and_required_review(self):
        self.p = self.f.create(steps=(step("a", review_required=True), step("c", ("a",))), creation_id="review-required")
        self.plan_id = self.p.current_metadata.plan_id
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        statements = (
            ("UPDATE checkpoints SET artifact=? WHERE plan_id=? AND attempt_id=?", (json.dumps(plans.to_jsonable(replace(result.artifact, version="wrong"))), self.plan_id, result.attempt_id)),
            ("UPDATE checkpoints SET outcome='failed' WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)),
            ("UPDATE reviews SET verdict='failed' WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)),
            ("UPDATE reviews SET artifact=? WHERE plan_id=? AND attempt_id=?", (json.dumps(plans.to_jsonable(replace(result.artifact, version="wrong"))), self.plan_id, result.attempt_id)),
            ("UPDATE reviews SET reviewer='synthetic-history-worker' WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)),
        )
        for sql, args in statements:
            with self.subTest(corruption=sql.split()[1]):
                before = self.snapshot()
                checkpoint_before = self.f.store.read(lambda db: db.execute(
                    "SELECT outcome,artifact FROM checkpoints WHERE plan_id=? AND attempt_id=?",
                    (self.plan_id, result.attempt_id)).fetchone())
                review_before = self.f.store.read(lambda db: db.execute(
                    "SELECT verdict,reviewer,artifact FROM reviews WHERE plan_id=? AND attempt_id=?",
                    (self.plan_id, result.attempt_id)).fetchone())
                self.f.store.write(lambda db: db.execute(sql, args))
                self.denied(command)
                # Undo only the deliberately corrupted value in this owned test store.
                if "checkpoints" in sql:
                    self.f.store.write(lambda db: db.execute("UPDATE checkpoints SET outcome=?,artifact=? WHERE plan_id=? AND attempt_id=?",
                                                           (*checkpoint_before, self.plan_id, result.attempt_id)))
                else:
                    self.f.store.write(lambda db: db.execute("UPDATE reviews SET verdict=?,reviewer=?,artifact=? WHERE plan_id=? AND attempt_id=?",
                                                           (*review_before, self.plan_id, result.attempt_id)))
                self.assertTrue(self.snapshot() == before, "Test corruption restoration changed unrelated history")
        self.f.store.write(lambda db: db.execute("DELETE FROM reviews WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)))
        self.denied(command)

    def test_accepted_history_needs_no_grant_flag_or_optional_pointer_layout(self):
        result = self.seed()
        self.edit_payload(result.attempt_id, lambda data: [data.pop(name, None) for name in (
            "synthetic_history", "checkpoint_sequence", "review_sequence", "accepted_sequence")])
        self.f.store.write(lambda db: db.execute("DELETE FROM reviews WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)))
        self.f.store.write(lambda db: db.execute("UPDATE events SET kind='retained-history' WHERE plan_id=? AND kind LIKE 'synthetic_history_%'", (self.plan_id,)))
        def clear_optional_links(db):
            for sequence, raw in db.execute("SELECT sequence,payload FROM events WHERE plan_id=?", (self.plan_id,)).fetchall():
                data = json.loads(raw)
                for name in ("synthetic_history", "checkpoint_sequence", "review_sequence", "review_id", "accepted_sequence"):
                    data.pop(name, None)
                db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(data), sequence))
        self.f.store.write(clear_optional_links)
        self.edit_payload(result.attempt_id, lambda data: data.update(epoch="historical-epoch", generation="historical-generation"))
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.execute(self.worker(consumer))

    def test_result_metadata_and_explicit_audit_links_must_agree(self):
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        self.f.store.write(lambda db: db.execute("UPDATE result_versions SET identity='wrong-output' WHERE plan_id=? AND result_version=?",
                                               (self.plan_id, result.result_version)))
        self.denied(command)
        self.f.store.write(lambda db: db.execute("UPDATE result_versions SET identity=? WHERE plan_id=? AND result_version=?",
                                               (result.artifact.identity, self.plan_id, result.result_version)))
        self.edit_payload(result.attempt_id, lambda data: data.update(checkpoint_sequence=999999))
        self.denied(command)

    def test_missing_or_ambiguous_accepted_result_is_not_available(self):
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO attempts(plan_id,attempt_id,step_id,assignee,state,version,definition_version,payload) "
            "SELECT plan_id,'ambiguous-producer',step_id,assignee,state,version,definition_version,payload "
            "FROM attempts WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)))
        self.denied(command)
        self.f.store.write(lambda db: db.execute("DELETE FROM attempts WHERE plan_id=? AND attempt_id='ambiguous-producer'", (self.plan_id,)))
        self.f.store.write(lambda db: db.execute("DELETE FROM result_versions WHERE plan_id=? AND result_version=?", (self.plan_id, result.result_version)))
        self.denied(command)

    def test_start_revalidates_accepted_history_after_binding(self):
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.f.store.write(lambda db: db.execute("UPDATE checkpoints SET outcome='failed' WHERE plan_id=? AND attempt_id=?",
                                               (self.plan_id, result.attempt_id)))
        self.denied(self.worker(consumer))

    def test_stale_own_version_and_full_frozen_definition_fail_new_progress(self):
        assigned = self.assign()
        old = self.worker(assigned, "report_activity")
        self.execute(self.worker(assigned, "report_activity"))
        self.denied(old, ("stale",))
        self.denied(replace(self.worker(assigned), definition_version="wrong"), ("stale",))
        def corrupt(db):
            data = json.loads(db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id='a'", (self.plan_id,)).fetchone()[0])
            data["definition"]["title"] = "Changed without a version change"
            db.execute("UPDATE steps SET payload=? WHERE plan_id=? AND step_id='a'", (json.dumps(data), self.plan_id))
        self.f.store.write(corrupt)
        self.denied(self.worker(assigned), ("stale",))

    def test_exact_replay_precedes_versions_holds_elapsed_and_accepted_terminal(self):
        assigned = self.assign()
        command = self.worker(assigned)
        original = self.execute(command)
        self.execute(self.worker(assigned, "report_activity"))
        self.hold()
        self.f.clock.advance(901)
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='accepted' WHERE plan_id=? AND attempt_id=?",
                                               (self.plan_id, assigned.committed["attempt_id"])))
        before = self.snapshot()
        replay = self.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed, original.committed)
        self.assertEqual(replay.committed_metadata, original.committed_metadata)
        self.assertGreater(replay.current_metadata.revision, original.committed_metadata.revision)
        self.assertTrue(self.snapshot() == before, "Exact replay changed terminal activity or history")
        self.denied(self.worker(assigned, "report_activity"))

    def test_worker_and_relay_fencing_and_declared_identity_precede_replay(self):
        assigned = self.assign()
        command = self.worker(assigned, relay="declared-relay")
        self.execute(command)
        self.denied(replace(command, actor="spoofed-relay"), ("forbidden",))
        self.denied(replace(command, payload={**command.payload, "origin_worker": "other-worker"}), ("forbidden",))
        self.denied(replace(command, attempt_token="unknown-token"), ("forbidden",))
        self.denied(replace(command, generation="wrong-generation"), ("stale",))
        self.denied(replace(command, epoch="wrong-epoch"), ("stale_epoch",))
        takeover = self.f.command("takeover", {"expected_ownership_version": self.f.service.read_plan(self.plan_id).metadata.ownership_version,
                                              "reason": "Synthetic owner transfer"}, receipt=self.p)
        self.execute(takeover)
        self.denied(command, ("fenced",))
        direct = self.worker(assigned, "report_activity")
        self.f.store.write(lambda db: db.execute("UPDATE capabilities SET revoked=1 WHERE secret_hash=?",
                                               (hash_token(direct.attempt_token),)))
        self.denied(direct, ("fenced",))

    def test_current_authenticated_relay_replays_before_stale_global_cas(self):
        assigned, sibling = self.assign(), self.assign("b")
        command = self.worker(assigned, relay="declared-relay")
        original = self.execute(command)
        self.execute(self.worker(sibling, "report_activity"))
        before = self.snapshot()
        replay = self.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed_metadata, original.committed_metadata)
        self.assertTrue(self.snapshot() == before, "Relayed replay mutated current state")
        stale = self.worker(sibling, "report_activity", relay="declared-relay")
        self.execute(self.worker(assigned, "report_activity"))
        self.denied(stale, ("conflict",))

    def test_bind_exact_replay_preserves_activity_and_resource_state(self):
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        original = self.execute(command)
        self.execute(self.worker(consumer))
        self.hold()
        self.f.clock.advance(1000)
        before = self.snapshot()
        replay = self.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed, original.committed)
        self.assertTrue(self.snapshot() == before, "Binding replay refreshed liveness or context")

    def test_default_inactivity_boundary_is_inclusive_and_has_no_hidden_writes(self):
        for elapsed, succeeds in ((899, True), (900, False), (901, False)):
            with self.subTest(elapsed=elapsed):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                self.p = self.f.create()
                self.plan_id = self.p.current_metadata.plan_id
                assigned = self.assign()
                self.f.clock.advance(elapsed)
                command = self.worker(assigned)
                if succeeds:
                    self.execute(command)
                    self.assertEqual(self.attempt(assigned).last_activity, self.f.clock().isoformat())
                else:
                    self.denied(command, ("stale",))
                    self.assertEqual(self.attempt(assigned).state, "assigned")

    def test_configured_activity_boundary_includes_submitted_and_is_isolated(self):
        for state in ("assigned", "active", "submitted"):
            with self.subTest(state=state):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                self.p = self.f.create()
                self.plan_id = self.p.current_metadata.plan_id
                short = self.configured_service(10)
                longer = self.configured_service(20)
                self.f.service = short
                assigned = self.assign()
                self.f.store.write(lambda db: db.execute("UPDATE attempts SET state=? WHERE plan_id=? AND attempt_id=?",
                                                       (state, self.plan_id, assigned.committed["attempt_id"])))
                self.f.clock.advance(10)
                command = self.worker(assigned, "report_activity")
                self.denied(command, ("stale",))
                self.f.service = longer
                self.execute(command)
                self.assertEqual(self.attempt(assigned).state, state)
                self.assertEqual(self.attempt(assigned).version, 2)
                self.f.service = short
                self.f.clock.advance(10)
                self.denied(self.worker(assigned, "report_activity"), ("stale",))

    def test_configured_threshold_just_before_equal_after_and_positive_validation(self):
        for invalid in (0, -1, True, "10", 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(DomainError) as caught:
                    self.configured_service(invalid)
                self.assertEqual(caught.exception.code, "invalid")
        self.f.service = self.configured_service(10)
        assigned = self.assign()
        self.f.clock.advance(9)
        self.execute(self.worker(assigned, "report_activity"))
        self.f.clock.advance(10)
        self.denied(self.worker(assigned, "report_activity"), ("stale",))
        self.f.clock.advance(1)
        self.denied(self.worker(assigned, "report_activity"), ("stale",))

    def test_configured_policy_is_isolated_between_concurrent_services(self):
        a, b = self.assign(), self.assign("b", assignee="worker-b")
        commands = (self.worker(a, "report_activity"), self.worker(b, "report_activity", origin="worker-b"))
        services = (self.configured_service(10), self.configured_service(20))
        self.f.clock.advance(15)
        barrier = threading.Barrier(2)
        def run(pair):
            service, command = pair
            barrier.wait()
            try:
                service.execute(command)
                return "committed"
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(run, zip(services, commands)))
        self.assertEqual(results, ("stale", "committed"))
        self.assertEqual((self.attempt(a).version, self.attempt(b).version), (1, 2))

    def test_activity_lease_never_borrows_future_plan_timestamp(self):
        assigned = self.assign()
        self.f.store.write(lambda db: db.execute("UPDATE plans SET updated_at='2026-10-07T12:00:00+00:00' WHERE plan_id=?",
                                               (self.plan_id,)))
        self.f.clock.advance(1)
        self.execute(self.worker(assigned, "report_activity"))
        self.assertEqual(self.attempt(assigned).last_activity, self.f.clock().isoformat())
        self.f.clock.advance(900)
        self.denied(self.worker(assigned, "report_activity"), ("stale",))

    def test_unrelated_activity_and_binding_cannot_extend_worker_inactivity(self):
        result = self.seed()
        consumer, sibling = self.assign("c"), self.assign("b")
        self.f.clock.advance(899)
        self.execute(self.worker(sibling, "report_activity"))
        self.bind(consumer, (self.binding(result),))
        self.f.clock.advance(1)
        self.denied(self.worker(consumer), ("stale",))
        self.denied(self.worker(consumer, "report_activity"), ("stale",))
        self.denied(self.bind_command(consumer, (self.binding(result),)), ("stale",))

    def test_invalid_or_missing_attempt_timestamp_cannot_become_fresh(self):
        assigned = self.assign()
        for timestamp in (None, "invalid-timestamp", "2026-10-06T12:00:00"):
            with self.subTest(timestamp=timestamp):
                self.edit_payload(assigned.committed["attempt_id"], lambda data: data.update(last_activity=timestamp))
                self.denied(self.worker(assigned, "report_activity"))

    def test_start_rechecks_scope_store_plan_and_prerequisite_holds(self):
        for guard in ("store", "global", "consumer", "prerequisite", "stop", "block", "scope", "stale-sibling"):
            with self.subTest(guard=guard):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                self.p = self.f.create()
                self.plan_id = self.p.current_metadata.plan_id
                result = self.seed()
                consumer = self.assign("c")
                self.bind(consumer, (self.binding(result),))
                if guard == "store":
                    self.f.store.write(lambda db: db.execute("UPDATE installation SET store_hold=1"))
                elif guard in ("global", "consumer", "prerequisite"):
                    self.hold(None if guard == "global" else "c" if guard == "consumer" else "a")
                elif guard == "stale-sibling":
                    sibling = self.assign("b")
                    self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='stale' WHERE plan_id=? AND attempt_id=?",
                                                           (self.plan_id, sibling.committed["attempt_id"])))
                else:
                    def change(db):
                        payload = json.loads(db.execute("SELECT payload FROM plans WHERE plan_id=?", (self.plan_id,)).fetchone()[0])
                        if guard == "scope":
                            payload["draft"]["authorization"]["mode"] = "plan-only"
                        else:
                            payload["draft"][guard + "_reason"] = "Explicit stop/block instruction"
                        db.execute("UPDATE plans SET payload=? WHERE plan_id=?", (json.dumps(payload), self.plan_id))
                    self.f.store.write(change)
                self.denied(self.worker(consumer), ("held", "forbidden"))

    def test_activity_records_observation_without_clearing_holds_or_reopening(self):
        assigned = self.assign()
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET state='submitted' WHERE plan_id=? AND attempt_id=?",
                                               (self.plan_id, assigned.committed["attempt_id"])))
        self.hold("a")
        self.execute(self.worker(assigned, "report_activity", observation="Recorded the completed immutable check"))
        self.assertEqual(self.attempt(assigned).state, "submitted")
        self.assertEqual(len(self.f.service.read_plan(self.plan_id).coordination.holds), 1)

    def test_unresolved_exact_result_invalidation_blocks_start(self):
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.f.service._run_write(lambda db: plans._insert_event(
            db, self.plan_id, None, "invalidate_result", "synthetic-orchestrator", self.f.clock().isoformat(),
            {"invalidation_id": "invalidated-input", "producer_step_id": "a", "result_version": result.result_version,
             "affected_steps": ["c"], "reason": "Changed observation"}))
        self.denied(self.worker(consumer), ("held",))

    def test_coherent_history_remains_inspectable_without_granting_output_availability(self):
        from adaptive_planning import attempts
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.hold("a")
        resolver = getattr(attempts, "_accepted_history", None)
        self.assertTrue(callable(resolver), "Coherent accepted history is not separated from output validity")
        history = self.f.service._run_read(lambda db: resolver(
            db, plans._load_plan(db, self.plan_id), "a", result.result_version))
        self.assertEqual(history[0], result.artifact)
        self.denied(self.worker(consumer), ("held",))
        self.denied(self.bind_command(consumer, (self.binding(result),)), ("held",))

    def test_only_exact_producer_revalidation_can_restore_original_output(self):
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.revalidate(result, affected="c")
        self.denied(self.worker(consumer), ("held",))
        self.revalidate(result, artifact=replace(result.artifact, version="different-version"))
        self.denied(self.worker(consumer), ("held",))
        self.revalidate(result, result_version="different-result")
        self.denied(self.worker(consumer), ("held",))
        for disposition in ("superseded", "replanned"):
            self.revalidate(result, disposition=disposition)
            self.denied(self.worker(consumer), ("held",))
        self.revalidate(result)
        self.execute(self.worker(consumer))

    def test_revalidation_does_not_clear_remaining_holds_or_newer_invalidations(self):
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.revalidate(result)
        self.hold("c")
        self.denied(self.worker(consumer), ("held",))
        self.f.store.write(lambda db: db.execute("UPDATE holds SET active=0 WHERE plan_id=?", (self.plan_id,)))
        self.invalidate(result, invalidation_id="later-invalidation")
        self.denied(self.worker(consumer), ("held",))

    def test_revalidation_rejects_contradictory_declared_audit_links(self):
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.history_event("resolve_invalidation", {
            "invalidation_id": "invalidated-input", "affected_step_id": "a", "disposition": "revalidated",
            "corrected_result_version": result.result_version, "attempt_id": "unrelated-producer",
            "evidence": plans.to_jsonable((self.binding(result).evidence,)),
        })
        self.denied(self.worker(consumer), ("held",))
        self.revalidate(result)
        self.execute(self.worker(consumer))

    def test_malformed_review_audit_link_cannot_restore_required_output(self):
        self.p = self.f.create(steps=(step("a", review_required=True), step("c", ("a",))), creation_id="malformed-review-link")
        self.plan_id = self.p.current_metadata.plan_id
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.revalidate(result)
        self.history_event("recorded-review", {"review_id": {"malformed": "link"}})
        self.denied(self.worker(consumer), ("held",))

    def test_later_reference_cannot_freshen_a_pre_invalidation_review(self):
        self.p = self.f.create(steps=(step("a", review_required=True), step("c", ("a",))), creation_id="old-review-reference")
        self.plan_id = self.p.current_metadata.plan_id
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        original_review = self.f.store.read(lambda db: db.execute(
            "SELECT review_id FROM reviews WHERE plan_id=? AND attempt_id=?", (self.plan_id, result.attempt_id)).fetchone()[0])
        self.invalidate(result)
        self.revalidate(result)
        self.history_event("later-reference", {"review_id": original_review, "step_id": "a",
                                              "attempt_id": result.attempt_id, "artifact": plans.to_jsonable(result.artifact)})
        self.denied(self.worker(consumer), ("held",))

    def test_required_review_must_match_artifact_and_follow_invalidation(self):
        self.p = self.f.create(steps=(step("a", review_required=True), step("c", ("a",))), creation_id="revalidation-review")
        self.plan_id = self.p.current_metadata.plan_id
        result = self.seed()
        consumer = self.assign("c")
        self.bind(consumer, (self.binding(result),))
        self.invalidate(result)
        self.revalidate(result)
        self.denied(self.worker(consumer), ("held",))
        for version in ("wrong-version", result.artifact.version):
            artifact = replace(result.artifact, version=version)
            evidence = replace(self.binding(result).evidence, artifact=artifact)
            review_id = "after-review-" + version
            self.f.store.write(lambda db: db.execute(
                "INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,created_at) "
                "VALUES(?,?,?,'a','after-invalidation-reviewer','passed','[]',?,?,?)",
                (review_id, self.plan_id, result.attempt_id, json.dumps(plans.to_jsonable((evidence,))),
                 json.dumps(plans.to_jsonable(artifact)), self.f.clock().isoformat())))
            self.denied(self.worker(consumer), ("held",))
            self.history_event("recorded-review", {"review_id": review_id, "attempt_id": result.attempt_id,
                                                  "step_id": "a", "artifact": plans.to_jsonable(artifact),
                                                  "definition_version": "contradictory-definition"})
            self.denied(self.worker(consumer), ("held",))
            self.history_event("recorded-review", {"review_id": review_id, "attempt_id": result.attempt_id,
                                                  "step_id": "a", "artifact": plans.to_jsonable(artifact)},
                               actor="after-invalidation-reviewer")
            self.denied(self.worker(consumer), ("held",))
        review_id = "fresh-revalidation-review"
        artifact = result.artifact
        evidence = self.binding(result).evidence
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,created_at) "
            "VALUES(?,?,?,'a','after-invalidation-reviewer','passed','[]',?,?,?)",
            (review_id, self.plan_id, result.attempt_id, json.dumps(plans.to_jsonable((evidence,))),
             json.dumps(plans.to_jsonable(artifact)), self.f.clock().isoformat())))
        self.history_event("recorded-review", {"review_id": review_id, "attempt_id": result.attempt_id,
                                              "step_id": "a", "artifact": plans.to_jsonable(artifact)},
                           actor="after-invalidation-reviewer")
        self.execute(self.worker(consumer))

    def test_own_active_reservations_are_retained_without_self_conflict(self):
        ws = self.f.register(workspace("/synthetic/writer", 81), self.p)
        assigned = self.assign(execution=context(ws))
        before = self.f.store.read(lambda db: db.execute("SELECT * FROM resource_uses ORDER BY use_id").fetchall())
        self.execute(self.worker(assigned))
        after = self.f.store.read(lambda db: db.execute("SELECT * FROM resource_uses ORDER BY use_id").fetchall())
        self.assertEqual(after, before)
        self.assertEqual(self.attempt(assigned).context.workspace_id, ws)

    def test_start_rejects_unresolved_missing_released_foreign_and_orphan_use(self):
        for conflict in ("unresolved", "missing", "released", "foreign", "orphan"):
            with self.subTest(conflict=conflict):
                self.f = make_service()
                self.addCleanup(self.f.cleanup)
                self.p = self.f.create()
                self.plan_id = self.p.current_metadata.plan_id
                ws = self.f.register(workspace("/synthetic/writer", 82), self.p)
                assigned = self.assign(execution=context(ws))
                if conflict == "missing":
                    self.f.store.write(lambda db: db.execute("DELETE FROM resource_uses WHERE plan_id=?", (self.plan_id,)))
                elif conflict in ("released", "unresolved"):
                    self.f.store.write(lambda db: db.execute("UPDATE resource_uses SET status=? WHERE plan_id=?", (conflict, self.plan_id)))
                elif conflict == "orphan":
                    seed_orphan_use(self.f, ws)
                else:
                    sibling = self.assign("b")
                    self.f.store.write(lambda db: db.execute(
                        "INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,scope,status) VALUES('foreign-use',?,?,?,'workspace','active')",
                        (ws, self.plan_id, sibling.committed["attempt_id"])))
                self.denied(self.worker(assigned), ("resource_conflict",))

    def test_identity_uncertainty_after_assignment_blocks_start(self):
        ws = self.f.register(workspace("/synthetic/identity", 83), self.p)
        assigned = self.assign(execution=context(ws))
        self.f.store.write(lambda db: db.execute("UPDATE resources SET physical_identity=NULL WHERE resource_id=?", (ws,)))
        self.denied(self.worker(assigned), ("held",))

    def test_progress_failure_rolls_back_attempt_step_event_and_request(self):
        result = self.seed()
        consumer = self.assign("c")
        command = self.bind_command(consumer, (self.binding(result),))
        before = self.snapshot()
        with patch("adaptive_planning.plans._commit_projection", side_effect=RuntimeError("owned fault after mutation")):
            with self.assertRaisesRegex(RuntimeError, "owned fault"):
                self.execute(command)
        self.assertTrue(self.snapshot() == before, "Failed bind committed partial context or receipt")
        self.bind(consumer, (self.binding(result),))
        for kind in ("start_step", "report_activity"):
            with self.subTest(kind=kind):
                command = self.worker(consumer, kind)
                before = self.snapshot()
                with patch("adaptive_planning.plans._commit_projection", side_effect=RuntimeError("owned fault after mutation")):
                    with self.assertRaisesRegex(RuntimeError, "owned fault"):
                        self.execute(command)
                self.assertTrue(self.snapshot() == before, "Failed progress committed partial activity, event or receipt")

    def test_concurrent_workers_merge_current_plan_without_lost_updates(self):
        a, b = self.assign(), self.assign("b", assignee="worker-b")
        commands = (self.worker(a), self.worker(b, origin="worker-b"))
        barrier = threading.Barrier(2)
        def run(command):
            barrier.wait()
            return self.execute(command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(run, commands))
        self.assertEqual(sorted(r.committed_metadata.revision for r in results), [4, 5])
        self.assertEqual((self.attempt(a).version, self.attempt(b).version), (2, 2))
        self.assertEqual({a.state for a in self.f.service.read_plan(self.plan_id).coordination.attempts}, {"active"})

    def test_mcp_typed_tools_delegate_to_the_same_service_authority(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        result = self.seed()
        consumer = self.assign("c")
        async def exercise():
            names = {tool.name for tool in await server.list_tools()}
            self.assertTrue({"bind_availability", "start_step", "report_activity"} <= names)
            command = self.bind_command(consumer, (self.binding(result),))
            response = await server.call_tool(command.kind, {"command": plans.to_jsonable(command)})
            structured = response.structured_content or {}
            code = structured.get("error", {}).get("code", "unexposed")
            self.assertFalse(response.is_error, f"Typed bind tool rejected valid attestation: {code}")
            for kind in ("start_step", "report_activity"):
                response = await server.call_tool(kind, {"command": plans.to_jsonable(self.worker(consumer, kind))})
                self.assertFalse(response.is_error, "Typed worker tool rejected authenticated progress")
            before = self.snapshot()
            bad = plans.to_jsonable(self.worker(consumer, "report_activity"))
            bad["payload"]["unrelated_field"] = "must reject"
            response = await server.call_tool("report_activity", {"command": bad})
            self.assertTrue(response.is_error, "Typed worker tool accepted an unknown payload field")
            mismatch = await server.call_tool("start_step", {"command": plans.to_jsonable(self.worker(consumer, "report_activity"))})
            self.assertTrue(mismatch.is_error, "Semantic tool accepted another operation kind")
            self.assertTrue(self.snapshot() == before, "Rejected typed input mutated shared state")
        asyncio.run(exercise())
        self.assertEqual(self.attempt(consumer).version, 4)

    def test_real_http_tools_use_configured_inactivity_and_shared_state(self):
        import logging
        import os
        import socket
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from adaptive_planning.app import build_app
        from adaptive_planning.config import Config
        from tests import test_service_sdk as sdk
        from tests.test_service_http import CapturedLogs, SYNTHETIC_TOKEN

        result = self.seed()
        consumer = self.assign("c")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = Config(self.f.directory, base_url=base_url, stale_seconds=10)
        descriptor = os.open(config.data_dir / "service-token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as token_file:
            token_file.write(SYNTHETIC_TOKEN)
        captured = CapturedLogs()
        root = logging.getLogger()
        async def exercise(app):
            async with sdk.running_service(app, sock):
                async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"}) as http_client:
                    async with Client(streamable_http_client(base_url + "/mcp", http_client=http_client)) as client:
                        command = self.bind_command(consumer, (self.binding(result),))
                        response = await client.call_tool(command.kind, {"command": plans.to_jsonable(command)})
                        self.assertFalse(response.is_error, "HTTP bind rejected valid recorded inputs")
                        response = await client.call_tool("start_step", {"command": plans.to_jsonable(self.worker(consumer))})
                        self.assertFalse(response.is_error, "HTTP start rejected authenticated worker")
                        self.f.clock.advance(9)
                        response = await client.call_tool("report_activity", {"command": plans.to_jsonable(self.worker(consumer, "report_activity"))})
                        self.assertFalse(response.is_error, "HTTP activity rejected observation before configured threshold")
                        self.f.clock.advance(10)
                        before = self.snapshot()
                        response = await client.call_tool("report_activity", {"command": plans.to_jsonable(self.worker(consumer, "report_activity"))})
                        self.assertTrue(response.is_error, "HTTP composition ignored configured inactivity threshold")
                        self.assertEqual(response.structured_content["error"]["code"], "stale")
                        self.assertTrue(self.snapshot() == before, "HTTP stale rejection changed persisted state")
        with patch("adaptive_planning.app.datetime") as injected_clock:
            injected_clock.now.side_effect = lambda zone: self.f.clock()
            app = build_app(config)
            root.addHandler(captured)
            try:
                with sdk._private_sdk_client_logs(captured):
                    asyncio.run(exercise(app))
            finally:
                root.removeHandler(captured)
        self.assertEqual(self.attempt(consumer).version, 4)


if __name__ == "__main__":
    unittest.main()
