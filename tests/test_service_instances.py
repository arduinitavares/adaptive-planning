"""Independent stores and credentials; aliases remain one service authority."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from adaptive_planning.app import build_app
from adaptive_planning.config import Config
from adaptive_planning.types import DomainError, ResourceClaim
from tests.service_fixtures import context, make_service
from tests.test_service_http import OTHER_TOKEN, SYNTHETIC_TOKEN, bound_ephemeral_socket
from tests.test_service_sdk import running_service


def _build_instance_app(directory: Path, token: str, base_url: str) -> Starlette:
    """Create one owned synthetic installation; never reuse existing data."""
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory / "service-token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as token_file:
        token_file.write(token)
    return build_app(Config(directory, base_url=base_url))


class TestInstances(unittest.TestCase):
    def setUp(self):
        self.a = self.enterContext(make_service())
        self.b = self.enterContext(make_service())

    def rows(self, fixture, tables=("plans", "events", "requests")):
        return fixture.store.read(lambda db: tuple(
            (name, tuple(tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")))
            for name in tables
        ))

    def test_independent_instances_keep_distinct_epochs_and_plan_state(self):
        a_info = self.a.service.read_service_info()
        b_info = self.b.service.read_service_info()
        self.assertEqual(a_info.host_id, b_info.host_id)
        self.assertEqual(a_info.host_id, "synthetic-host")
        self.assertNotEqual(a_info.epoch, b_info.epoch)
        plan = self.a.create()
        plan_id = plan.current_metadata.plan_id
        self.assertEqual(self.a.service.read_plan(plan_id).plan_id, plan_id)
        self.assertEqual(tuple(p.plan_id for p in self.a.service.list_plans()), (plan_id,))
        self.assertEqual(self.b.service.list_plans(), ())

    def test_foreign_instance_command_is_rejected_without_mutation(self):
        plan = self.a.create()
        command = self.a.command("assign_step", {
            "step_id": "a", "assignee": "worker-a", "context": context(None, writes=False)
        }, receipt=plan)
        receipt = self.a.service.execute(command)
        self.assertIsNotNone(receipt.committed["attempt_id"])
        before = self.rows(self.b)
        with self.assertRaises(DomainError) as raised:
            self.b.service.execute(command)
        self.assertEqual(raised.exception.code, "stale_epoch")
        self.assertEqual(self.rows(self.b), before)

    def test_foreign_plan_and_grant_are_rejected_after_epoch_check(self):
        a_plan, b_plan = self.a.create(), self.b.create()
        before = self.rows(self.b)
        with self.assertRaises(DomainError) as raised:
            self.b.service.read_plan(a_plan.current_metadata.plan_id)
        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(self.rows(self.b), before)
        valid = self.b.command("assign_step", {
            "step_id": "a", "assignee": "worker-a", "context": context(None, writes=False)
        }, receipt=b_plan)
        foreign_grant = replace(valid, orchestrator_token=a_plan.private.orchestrator_token)
        self.assertEqual(foreign_grant.epoch, self.b.service.read_service_info().epoch)
        self.assertEqual(foreign_grant.plan_id, b_plan.current_metadata.plan_id)
        with self.assertRaises(DomainError) as raised:
            self.b.service.execute(foreign_grant)
        self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(self.rows(self.b), before)
        # The same B mutation with its own grant succeeds: the envelope is otherwise valid.
        self.assertIsNotNone(self.b.service.execute(valid).committed["attempt_id"])

    def test_resource_reservations_are_scoped_to_each_instance(self):
        a_plan, b_plan = self.a.create(), self.b.create()
        claim = ResourceClaim("synthetic-host", "external", None, None, None, False,
                              None, "synthetic-external-shared")
        a_resource = self.a.register(claim, a_plan)
        b_resource = self.b.register(claim, b_plan)

        def assign_resource(fixture, plan, resource):
            return fixture.service.execute(fixture.command("assign_step", {
                "step_id": "a", "assignee": "worker-a",
                "context": context(None, external_ids=(resource,))
            }, receipt=plan))

        b_before = self.rows(self.b)
        assign_resource(self.a, a_plan, a_resource)
        a_uses = self.rows(self.a, ("resource_uses",))
        self.assertTrue(a_uses[0][1])
        self.assertEqual(self.rows(self.b, ("resource_uses", "holds")),
                         (("resource_uses", ()), ("holds", ())))
        self.assertEqual(self.rows(self.b), b_before)
        assign_resource(self.b, b_plan, b_resource)
        self.assertTrue(self.rows(self.b, ("resource_uses",))[0][1])
        self.assertEqual(self.rows(self.a, ("resource_uses",)), a_uses)

    def test_instance_credentials_are_not_interchangeable(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        installations = []
        for name, token, foreign in (("a", SYNTHETIC_TOKEN, OTHER_TOKEN),
                                      ("b", OTHER_TOKEN, SYNTHETIC_TOKEN)):
            sock = bound_ephemeral_socket()
            self.addCleanup(sock.close)
            base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            app = _build_instance_app(Path(temporary.name) / name, token, base_url)
            # unittest cleanup is LIFO: close each Store before removing its owned directory.
            self.addCleanup(app.state.close)
            installations.append((app, sock, base_url, token, foreign))

        async def exercise():
            identities = []
            for app, sock, url, token, foreign in installations:
                async with running_service(app, sock):
                    async with httpx2.AsyncClient(trust_env=False, timeout=10,
                            headers={"Authorization": f"Bearer {token}"}) as http:
                        denied = await http.post(url + "/mcp",
                            headers={"Authorization": f"Bearer {foreign}"}, json={})
                        self.assertEqual(denied.status_code, 401)
                        async with Client(streamable_http_client(url + "/mcp", http_client=http),
                                          mode="legacy") as client:
                            self.assertIn("read_service_info", {t.name for t in (await client.list_tools()).tools})
                            result = await client.call_tool("read_service_info", {})
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content["dashboard_url"], url + "/")
                            identities.append(result.structured_content["epoch"])
            self.assertNotEqual(identities[0], identities[1])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
