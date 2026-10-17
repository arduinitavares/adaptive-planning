"""Decisive tests for observation accounting, independent of the service runtime."""
import tempfile
import unittest
from pathlib import Path

from adaptive_planning.measurements import Ledger, validate_observation, report, union_seconds
from adaptive_planning.types import DomainError


def observation(key="r1", kind="context", data=None, **scope):
    return {**dict(source_record_id=key, epoch="epoch-test", plan_id="p1", kind=kind,
                observed_at="2026-10-18T00:00:00Z", step_id=None, attempt_id=None,
                checkpoint_sequence=None, data=data or {"case_kind": "operational"}), **scope}


class Measurements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / "measurements.sqlite")

    def record(self, value):
        validate_observation(value)
        return self.ledger.record(value, "orchestrator", "2026-10-18T00:01:00Z", {"code": "test"})

    def test_exact_replay_conflicting_reuse_and_restart(self):
        value = observation()
        first = self.record(value)
        self.assertFalse(first["replayed"])
        self.assertTrue(self.record(value)["replayed"])
        with self.assertRaises(DomainError) as raised:
            self.record(observation(data={"case_kind": "fixture"}))
        self.assertEqual(raised.exception.code, "conflict")
        reopened = Ledger(self.ledger.path)
        self.assertEqual(len(reopened.rows("epoch-test", "p1")), 1)

    def test_rejects_unknown_negative_nonfinite_and_cumulative_usage(self):
        for data in ({"metrics": {"input_tokens": -1}}, {"metrics": {"input_tokens": float("nan")}},
                     {"metrics": {"magic_cost": 3}}, {"metrics": {"input_tokens": 4}, "cumulative": True}):
            with self.subTest(data=data), self.assertRaises(DomainError):
                validate_observation(observation(kind="effort", data=data))

    def test_unknown_partial_complete_and_real_zero(self):
        cases = [{"plan_id": "p1", "status": "running", "created_at": "2026-10-17T00:00:00Z", "steps": []}]
        empty = report(cases, [], {})["plans"][0]
        self.assertIsNone(empty["resources"]["input_tokens"]["known_sum"])
        self.record(observation(kind="effort", data={"activity_kind": "implement", "corrective_rework": False,
                    "metrics": {"input_tokens": 10}, "source": "provider_receipt", "evidence": "receipt-1"}))
        partial = report(cases, self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertEqual(partial["resources"]["input_tokens"], {"known_sum": 10, "coverage": "partial"})
        self.record(observation("coverage1", "coverage", {"metrics": ["input_tokens", "human_seconds"],
                    "evidence": "all receipts for p1 reconciled"}))
        complete = report(cases, self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertEqual(complete["resources"]["human_seconds"], {"known_sum": 0, "coverage": "client_attested_complete"})

    def test_rework_is_subset_and_requires_cause(self):
        data = {"activity_kind": "debug", "corrective_rework": True, "cause_id": "defect-1",
                "metrics": {"worker_seconds": 20}, "source": "client_measured", "evidence": "span-1"}
        with self.assertRaises(DomainError):
            self.record(observation(kind="effort", data=data))
        self.record(observation("d1", "defect", {"defect_id": "defect-1", "stage": "caught", "status": "open",
                    "cause": "introduced by patch abc", "evidence": "regression test fails on abc"}))
        self.record(observation("u1", "effort", data))
        self.record(observation("u2", "effort", {**data, "corrective_rework": False, "cause_id": None}))
        case = {"plan_id": "p1", "status": "running", "created_at": "2026-10-17T00:00:00Z", "steps": []}
        result = report([case], self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertEqual(result["resources"]["worker_seconds"]["known_sum"], 40)
        self.assertEqual(result["rework"]["worker_seconds"]["known_sum"], 20)
        self.assertEqual(result["regressions"]["caught"], 1)

    def test_overlapping_active_intervals_use_union(self):
        self.assertEqual(union_seconds([("2026-10-18T00:00:00Z", "2026-10-18T00:01:00Z"),
                                        ("2026-10-18T00:00:30Z", "2026-10-18T00:01:30Z")]), 90)

    def test_fixtures_and_unknown_are_not_operational(self):
        cases = [{"plan_id": "p1", "status": "running", "created_at": "2026-10-17T00:00:00Z", "steps": []}]
        self.assertEqual(report(cases, [], {})["cohort"]["operational_plans"], 0)
        self.record(observation(data={"case_kind": "fixture"}))
        self.assertEqual(report(cases, self.ledger.rows("epoch-test", "p1"), {})["cohort"]["fixture_plans"], 1)

    def test_estimates_are_visible_separately_from_measured_usage(self):
        self.record(observation(kind="effort", data={"activity_kind": "plan", "corrective_rework": False,
                    "metrics": {"input_tokens": 100}, "source": "estimate", "evidence": "estimated budget"}))
        case = {"plan_id": "p1", "status": "running", "created_at": "2026-10-17T00:00:00Z", "steps": []}
        result = report([case], self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertIsNone(result["resources"]["input_tokens"]["known_sum"])
        self.assertEqual(result["estimated_resources"]["input_tokens"], 100)

    def test_rejects_estimated_actual_billing_and_huge_integer(self):
        base = {"activity_kind": "plan", "corrective_rework": False, "metrics": {}, "source": "estimate", "evidence": "budget"}
        for data in ({**base, "billing_amount": 2, "currency": "USD", "billing_scope": "isolated"},
                     {**base, "metrics": {"input_tokens": 10 ** 400}}):
            with self.subTest(data=str(data)[:100]), self.assertRaises(DomainError):
                validate_observation(observation(kind="effort", data=data))

    def test_explicit_later_failure_does_not_hide_behind_acceptance(self):
        self.record(observation("failed", "outcome", {"status": "failed", "evidence": "post-acceptance replay fails"}, step_id="a"))
        case = {"plan_id": "p1", "status": "complete", "created_at": "2026-10-17T00:00:00Z",
                "steps": [{"step_id": "a", "state": "complete", "accepted_at": "2026-10-17T12:00:00Z"}]}
        self.assertEqual(report([case], self.ledger.rows("epoch-test", "p1"), {})["plans"][0]["outcomes"]["failed"], 1)

    def test_late_old_attempt_and_worker_failure_preserve_current_owner_verification(self):
        verified = observation("verified", "outcome", {"status": "verified", "independent": True,
                    "verifier": "reviewer", "result_version": "result-2", "evidence": "independent receipt"},
                    step_id="a", attempt_id="attempt-2", checkpoint_sequence=20, observed_at="2026-10-18T12:01:00Z")
        self.record(verified)
        case = {"plan_id": "p1", "status": "complete", "created_at": "2026-10-17T00:00:00Z",
                "steps": [{"step_id": "a", "state": "complete", "accepted_at": "2026-10-18T12:00:00Z",
                           "attempt_id": "attempt-2", "result_version": "result-2"}]}
        late = observation("late", "outcome", {"status": "failed", "evidence": "delayed attempt-1 receipt"},
                           step_id="a", attempt_id="attempt-1", observed_at="2026-10-18T09:00:00Z")
        self.record(late)
        result = report([case], self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertEqual(result["outcomes"]["verified"], 1)
        worker = {**late, "source_record_id": "worker-late", "attempt_id": "attempt-2", "observed_at": "2026-10-18T13:00:00Z"}
        self.ledger.record(worker, {"role": "attempt"}, "2026-10-18T13:01:00Z", {})
        result = report([case], self.ledger.rows("epoch-test", "p1"), {})["plans"][0]
        self.assertEqual(result["outcomes"]["verified"], 1)
        self.assertEqual(len(result["time"]["creation_to_accepted_seconds"]), 1)


if __name__ == "__main__":
    unittest.main()
