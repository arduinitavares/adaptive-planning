"""Useful lifecycle measurements require no extra client receipts."""
import json
import unittest

from adaptive_planning.types import Review
from tests.service_fixtures import checkpoint, make_service, step


class AutomaticMeasurements(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())

    def complete(self, reviewed=False):
        plan = self.f.create(steps=(step("a", review_required=reviewed),))
        attempt = self.f.start(plan, self.f.assign(plan))
        value = checkpoint()
        self.f.submit(plan, attempt, value)
        if reviewed:
            review = Review("other-reviewer", value.artifact, "passed", (), value.evidence, None)
            self.f.service.execute(self.f.command("record_review", {"step_id": "a", "review": review}, receipt=plan))
        self.f.accept(plan, attempt, value)
        return plan.current_metadata.plan_id

    def test_normal_calls_populate_lifecycle_without_receipts(self):
        pid = self.complete()
        before = self.f.store.read(lambda db: list(db.iterdump()))
        result = self.f.service.read_measurements(pid)
        item = result["plans"][0]
        self.assertEqual(item["observation_count"], 0)
        self.assertEqual(item["case_kind"], "unknown")
        self.assertEqual(item["lifecycle"]["accepted_steps"], 1)
        self.assertEqual(item["lifecycle"]["accepted_with_linked_review"], 0)
        self.assertEqual(item["lifecycle"]["checkpoints"], {"passed": 1, "failed": 0, "blocked": 0})
        self.assertEqual(item["lifecycle"]["reassessments"]["unchanged"], 1)
        self.assertEqual(item["lifecycle"]["attempts"], 1)
        checkpoint_event = next(e for e in item["lifecycle"]["trace"] if e["kind"] == "record_checkpoint")
        self.assertEqual(checkpoint_event["outcome"], "passed")
        self.assertEqual(checkpoint_event["disposition"], "unchanged")
        self.assertEqual(len(item["lifecycle"]["acceptance_elapsed_seconds"]), 1)
        self.assertIsNone(item["resources"]["input_tokens"]["known_sum"])
        self.assertEqual(before, self.f.store.read(lambda db: list(db.iterdump())))

    def test_exact_accepted_review_is_derived_without_new_attestation(self):
        pid = self.complete(reviewed=True)
        item = self.f.service.read_measurements(pid)["plans"][0]
        self.assertEqual(item["lifecycle"]["accepted_with_linked_review"], 1)
        self.assertEqual(item["outcomes"]["verified"], 0)
        self.assertIn("model family", item["lifecycle"]["review_basis"])

    def test_old_artifact_review_cannot_count_for_current_result(self):
        pid = self.complete(reviewed=True)
        # Deliberate corruption in this owned fixture proves the measurement fails closed.
        self.f.store.write(lambda db: db.execute("UPDATE reviews SET artifact=? WHERE plan_id=?", (json.dumps({"kind": "code", "identity": "wrong"}), pid)))
        life = self.f.service.read_measurements(pid)["plans"][0]["lifecycle"]
        self.assertEqual(life["accepted_with_linked_review"], 0)
        self.assertEqual(life["unresolved_review_bindings"], 1)

    def test_review_from_another_submission_cycle_cannot_count(self):
        pid = self.complete(reviewed=True)
        def damage(db):
            sequence, raw = db.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='record_review'", (pid,)).fetchone()
            data = json.loads(raw)
            data["cycle"]["checkpoint_sequence"] = -1
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(data), sequence))
        self.f.store.write(damage)
        life = self.f.service.read_measurements(pid)["plans"][0]["lifecycle"]
        self.assertEqual(life["accepted_with_linked_review"], 0)
        self.assertEqual(life["unresolved_review_bindings"], 1)

    def test_optional_review_dangling_pointer_is_visible(self):
        pid = self.complete()
        def damage(db):
            aid, raw = db.execute("SELECT attempt_id,payload FROM attempts WHERE plan_id=?", (pid,)).fetchone()
            data = json.loads(raw)
            data["review_id"] = "missing-review"
            db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?", (json.dumps(data), aid))
        self.f.store.write(damage)
        life = self.f.service.read_measurements(pid)["plans"][0]["lifecycle"]
        self.assertEqual(life["accepted_with_linked_review"], 0)
        self.assertEqual(life["unresolved_review_bindings"], 1)

    def test_remote_contract_explains_keys_auth_and_unknown_values(self):
        pid = self.complete()
        contract = self.f.service.read_measurements(pid)["receipt_contract"]
        self.assertEqual(contract["schema"], 1)
        self.assertIn("source_record_id", contract["envelope"])
        self.assertIn("context", contract["kinds"])
        self.assertIn("effort", contract["kinds"])
        self.assertIn("orchestrator_token", contract["authentication"])
        self.assertNotIn(self.f._cached_grants[pid].orchestrator_token, json.dumps(contract))

    def test_client_versions_can_be_recorded_without_guessing_usage(self):
        pid = self.complete()
        observation = dict(source_record_id="codex:fixture:context:v1", epoch=self.f.epoch, plan_id=pid,
                           kind="context", observed_at=self.f.clock().isoformat(), step_id=None, attempt_id=None,
                           checkpoint_sequence=None, data={"client_name": "Codex", "client_version": "fixture-version",
                           "client_os": "windows", "collector_version": "agent-receipts-v1", "case_kind": "fixture"})
        self.f.service.record_measurement(observation)
        self.assertTrue(self.f.service.record_measurement(observation)["replayed"])
        item = self.f.service.read_measurements(pid)["plans"][0]
        self.assertEqual(item["versions"][0]["client_os"], "windows")
        self.assertIsNone(item["resources"]["input_tokens"]["known_sum"])

    def test_failed_check_is_not_automatically_a_defect_or_rework(self):
        plan = self.f.create(steps=(step("a"),))
        attempt = self.f.start(plan, self.f.assign(plan))
        self.f.submit(plan, attempt, checkpoint("failed"))
        item = self.f.service.read_measurements(plan.current_metadata.plan_id)["plans"][0]
        self.assertEqual(item["lifecycle"]["checkpoints"]["failed"], 1)
        self.assertIsNone(item["regressions"]["caught"])
        self.assertIsNone(item["rework"]["worker_seconds"]["known_sum"])
        self.assertEqual(item["lifecycle"]["accepted_steps"], 0)


if __name__ == "__main__":
    unittest.main()
