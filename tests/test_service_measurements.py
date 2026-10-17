"""Measurement collection must not acquire planning authority."""
import asyncio
import json
import unittest

from adaptive_planning.types import DomainError
from tests.service_fixtures import make_service


class ServiceMeasurements(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id
        self.obs = dict(source_record_id="fixture-context", epoch=self.f.epoch, plan_id=self.plan_id,
                        kind="context", step_id=None, attempt_id=None, checkpoint_sequence=None,
                        observed_at=self.f.clock().isoformat(), data={"case_kind": "fixture", "skill_version": "sha256:test"})

    def test_context_and_usage_do_not_change_canonical_state(self):
        before = self.f.store.read(lambda db: list(db.iterdump()))
        self.f.service.record_measurement(self.obs)
        result = self.f.service.read_measurements(self.plan_id)
        self.assertEqual(result["cohort"]["fixture_plans"], 1)
        after = self.f.store.read(lambda db: list(db.iterdump()))
        self.assertEqual(before, after)
        self.assertEqual(result["plans"][0]["resources"]["input_tokens"]["coverage"], "none")

    def test_cost_requires_capability_and_invalid_tokens_are_not_downgraded(self):
        value = {**self.obs, "kind": "effort", "data": {"activity_kind": "implement", "corrective_rework": False,
                 "metrics": {"input_tokens": 5}, "source": "provider_receipt", "evidence": "fixture receipt"}}
        with self.assertRaises(DomainError) as failure:
            self.f.service.record_measurement(value)
        self.assertEqual(failure.exception.code, "forbidden")
        token = self.p.private.orchestrator_token
        self.assertFalse(self.f.service.record_measurement(value, orchestrator_token=token)["replayed"])
        self.assertTrue(self.f.service.record_measurement(value, orchestrator_token=token)["replayed"])
        with self.assertRaises(DomainError):
            self.f.service.record_measurement(self.obs, orchestrator_token="wrong")

    def test_epoch_and_scope_are_checked(self):
        with self.assertRaises(DomainError) as failure:
            self.f.service.record_measurement({**self.obs, "epoch": "old"})
        self.assertEqual(failure.exception.code, "stale_epoch")
        with self.assertRaises(DomainError):
            self.f.service.record_measurement({**self.obs, "step_id": "missing"})

    def test_sdk_registry_and_trace(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        result = asyncio.run(server.call_tool("read_measurements", {"plan_id": self.plan_id}))
        self.assertFalse(result.is_error)
        bad = asyncio.run(server.call_tool("record_measurement", {"observation": {**self.obs, "orchestrator_token": "SECRET_WRONG"}}))
        self.assertTrue(bad.is_error)
        report = self.f.service.read_measurements(self.plan_id)
        self.assertGreaterEqual(report["mcp_calls"]["count"], 2)
        self.assertNotIn("SECRET_WRONG", json.dumps(report))

    def test_read_page_escapes_observations_and_rejects_duplicate_queries(self):
        from adaptive_planning.admin import initialize_installation
        from adaptive_planning.app import build_app
        from adaptive_planning.config import Config
        from tests.test_service_http import call
        self.f.service.record_measurement({**self.obs, "data": {"skill_version": "<script>alert(1)</script>"}})
        config = Config(self.f.directory)
        initialize_installation(config)
        app = build_app(config)
        self.addCleanup(app.state.close)
        headers = [("host", b"127.0.0.1:8790")]
        status, response_headers, body = call(app, "GET", "/measurements", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<script>alert", body)
        self.assertIn("content-security-policy", dict(response_headers))
        status, _, _ = call(app, "GET", "/api/measurements", headers=headers, query=b"limit=1&limit=2")
        self.assertEqual(status, 400)

    def test_worker_cost_is_bound_to_own_attempt_and_owner_can_report_late(self):
        assigned = self.f.assign(self.p)
        value = {**self.obs, "source_record_id": "worker-cost", "kind": "effort", "step_id": "a",
                 "attempt_id": assigned.committed["attempt_id"], "data": {"activity_kind": "verify",
                 "corrective_rework": False, "metrics": {"worker_seconds": 7}, "source": "client_measured", "evidence": "fixture span"}}
        token = assigned.private.attempt_token
        self.f.service.record_measurement(value, attempt_token=token)
        with self.assertRaises(DomainError):
            self.f.service.record_measurement({**value, "source_record_id": "other-step", "step_id": "b"}, attempt_token=token)

    def test_verified_attestation_matches_accepted_version_and_independent_reviewer(self):
        from tests.service_fixtures import seed_accepted_result, checkpoint
        seeded = seed_accepted_result(self.f, self.plan_id, "a", checkpoint().artifact)
        row = self.f.store.read(lambda db: db.execute("SELECT attempt_id,payload FROM attempts WHERE plan_id=? AND step_id='a'", (self.plan_id,)).fetchone())
        data = json.loads(row[1])
        value = {**self.obs, "source_record_id": "verified", "kind": "outcome", "step_id": "a", "attempt_id": row[0],
                 "checkpoint_sequence": data["checkpoint_sequence"], "data": {"status": "verified", "independent": True,
                 "verifier": "independent-checker", "result_version": seeded.result_version, "evidence": "fixture test receipt"}}
        token = self.p.private.orchestrator_token
        self.f.service.record_measurement(value, orchestrator_token=token)
        self.assertEqual(self.f.service.read_measurements(self.plan_id)["plans"][0]["outcomes"]["verified"], 1)
        with self.assertRaises(DomainError):
            self.f.service.record_measurement({**value, "source_record_id": "wrong-result", "data": {**value["data"], "result_version": "wrong"}}, orchestrator_token=token)


if __name__ == "__main__":
    unittest.main()
