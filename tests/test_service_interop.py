"""Shared planning over real SDK HTTP sessions and documented client schemas.

SDK sessions are an automated equivalent; installed product acceptance is separate.
All credentials and wire diagnostics stay in memory owned by these tests.
"""

import asyncio
import copy
from contextlib import contextmanager
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import re
import shlex
import socket
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from adaptive_planning import plans
from adaptive_planning.app import PlanningService, build_app
from adaptive_planning.config import Config
from adaptive_planning.types import Metadata, MutationReceipt, PrivateGrant, parse_command
from tests.service_fixtures import draft, make_service
from tests import test_service_sdk as sdk
from tests.test_service_http import CapturedLogs, SYNTHETIC_TOKEN


PLANNING_FACTS = ("Creator SDK planning fact", "Second SDK planning fact", "Third SDK planning fact")
PLANNING_TOOLS = {"read_service_info", "list_plans", "read_plan", "read_events", "list_ready",
                  "read_coordination", "create_plan", "revise_plan", "takeover"}


async def run_planning_interop(service: PlanningService) -> tuple[MutationReceipt, ...]:
    """Exercise three fresh HTTP connections against the production composition.

    The supplied fixture's database is reopened by build_app, as at production
    startup. The owned listener, synthetic token file and diagnostics are private
    and bounded to this call; no installed client configuration is read or written.
    """
    async def call(client, name, arguments):
        result = await client.call_tool(name, arguments)
        if result.is_error or not isinstance(result.structured_content, dict):
            raise AssertionError("The SDK planning call did not return a successful structured result")
        return result.structured_content

    def receipt(value):
        private = value["private"]
        return MutationReceipt(
            committed=value["committed"], committed_metadata=Metadata(**value["committed_metadata"]),
            current_metadata=Metadata(**value["current_metadata"]), replayed=value["replayed"],
            private=PrivateGrant(**private) if private is not None else None,
        )

    receipts = []
    infos = []
    grant = None
    authorization = None
    root = logging.getLogger()
    captured = CapturedLogs()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = Config(service._store.path.parent, base_url=base_url)
        token_path = config.data_dir / "service-token"
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as token_file:
                token_file.write(SYNTHETIC_TOKEN)
            app = build_app(config)
            root.addHandler(captured)
            try:
                with sdk._private_sdk_client_logs(captured):
                    async with sdk.running_service(app, sock):
                        async with httpx2.AsyncClient() as dashboard:
                            for index, fact in enumerate(PLANNING_FACTS):
                                # Exiting each block closes the creator before a fresh
                                # client resumes the canonical plan in the next block.
                                async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"}) as http_client:
                                    async with Client(streamable_http_client(base_url + "/mcp", http_client=http_client)) as client:
                                        assert client.protocol_version is not None, "The protocol was not negotiated"
                                        names = {tool.name for tool in (await client.list_tools()).tools}
                                        assert names == PLANNING_TOOLS, "The planning tool surface differs"
                                        info = await call(client, "read_service_info", {})
                                        infos.append(info)
                                        metadata = None
                                        if index == 0:
                                            proposed = plans.to_jsonable(replace(draft(mode="plan-only"), facts=(fact,)))
                                            kind = "create_plan"
                                            payload = {"creation_id": uuid.uuid4().hex, "draft": proposed,
                                                       "new_plan_intent": "Synthetic three-session planning check"}
                                        else:
                                            current = await call(client, "read_plan", {"plan_id": receipts[0].current_metadata.plan_id})
                                            metadata = current["metadata"]
                                            assert metadata["revision"] == index, "The fresh session did not resume the latest revision"
                                            proposed = copy.deepcopy(current["draft"])
                                            assert proposed["authorization"] == authorization, "Stored authorization changed"
                                            proposed["facts"].append(fact)
                                            kind = "revise_plan"
                                            payload = {"draft": proposed, "reason": fact, "authorization_change": None,
                                                       "retire_steps": []}
                                        command = {
                                            "kind": kind, "epoch": info["epoch"], "key": uuid.uuid4().hex,
                                            "actor": f"sdk-session-{index + 1}",
                                            "plan_id": metadata["plan_id"] if metadata else None,
                                            "generation": metadata["generation"] if metadata else None,
                                            "expected_revision": metadata["revision"] if metadata else None,
                                            "ownership_version": metadata["ownership_version"] if metadata else None,
                                            "orchestrator_token": grant if metadata else None,
                                            "attempt_id": None, "attempt_token": None, "attempt_version": None,
                                            "definition_version": None, "payload": payload,
                                        }
                                        value = await call(client, kind, {"command": plans.to_jsonable(parse_command(command))})
                                        committed = receipt(value)
                                        receipts.append(committed)
                                        if index == 0:
                                            assert committed.private is not None, "Creation did not return its private grant"
                                            grant = committed.private.orchestrator_token
                                            assert grant, "Creation did not return its orchestrator capability"
                                        plan_id = committed.current_metadata.plan_id
                                        public = await call(client, "read_plan", {"plan_id": plan_id})
                                        if index == 0:
                                            authorization = copy.deepcopy(public["draft"]["authorization"])
                                            assert authorization["interval"] and authorization["since_revision"] == 1, "Creation did not initialize authorization"
                                        assert public["metadata"] == value["current_metadata"], "The canonical metadata differs"
                                        assert public["draft"]["facts"] == list(PLANNING_FACTS[:index + 1]), "Planning facts were lost"
                                        assert public["draft"]["authorization"] == authorization, "Revision renewed authorization"
                                        events = await call(client, "read_events", {"plan_id": plan_id})
                                        observed = await dashboard.get(base_url + "/api/plans/" + plan_id)
                                        assert observed.status_code == 200, "The public dashboard plan read failed"
                                        assert observed.json() == public, "The dashboard and SDK plan differ"
                                        observed_events = await dashboard.get(base_url + "/api/plans/" + plan_id + "/events")
                                        assert observed_events.status_code == 200, "The public dashboard events read failed"
                                        assert observed_events.json() == events, "The dashboard and SDK events differ"
                                        assert [event["plan_revision"] for event in events["events"]] == list(range(1, index + 2)), "Committed events are missing"
                                        assert [event["actor"] for event in events["events"]] == [f"sdk-session-{number}" for number in range(1, index + 2)], "Session events differ"
                                        page = await dashboard.get(committed.current_metadata.dashboard_url)
                                        assert page.status_code == 200 and 'data-mode="service"' in page.text, "The dashboard plan page failed"
                                        assert f'data-plan-id="{plan_id}"' in page.text, "The dashboard targets a different plan"
                                        public_json = json.dumps({"plan": public, "events": events})
                                        assert grant not in public_json and "orchestrator_token" not in public_json, "Public planning reads exposed a private grant"
                            assert all(info == infos[0] for info in infos), "The sessions discovered different installations"
                            assert infos[0]["epoch"] == service.read_service_info().epoch, "The fixture and app installations differ"
                            assert infos[0]["dashboard_url"] == base_url + "/", "Installation discovery returned a different dashboard"
                            assert len({(item.current_metadata.epoch, item.current_metadata.plan_id,
                                         item.current_metadata.generation, item.current_metadata.dashboard_url)
                                        for item in receipts}) == 1, "The sessions did not share one plan identity"
            finally:
                root.removeHandler(captured)
        finally:
            token_path.unlink()
        assert sock.fileno() == -1, "The owned service listener was not closed"
    return tuple(receipts)


def _logging_state(*additional_loggers):
    """Snapshot existing loggers using public APIs, without formatting records."""
    pending = [logging.getLogger(), *additional_loggers]
    fallback = logging.lastResort
    state = {"record_factory": logging.getLogRecordFactory(),
             "last_resort": (fallback, tuple(fallback.filters) if fallback is not None else ())}
    while pending:
        logger = pending.pop()
        if logger in state:
            continue
        state[logger] = (logger.level, logger.disabled, logger.propagate,
                         tuple(logger.handlers), tuple(logger.filters),
                         tuple((handler, tuple(handler.filters)) for handler in logger.handlers))
        pending.extend(logger.getChildren())
    return state


@contextmanager
def _private_emission_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        handler.close()
        stream.close()


class TestService(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = make_service(Path(directory.name))

    def test_three_sessions_share_planning_events(self):
        sessions = []
        actions = []
        protocols = []
        original_call = Client.call_tool
        original_exit = Client.__aexit__

        async def observe(client, name, arguments):
            if client not in sessions:
                sessions.append(client)
                protocols.append(str(client.protocol_version))
            actions.append((client, name))
            return await original_call(client, name, arguments)

        async def closed(client, *arguments):
            result = await original_exit(client, *arguments)
            actions.append((client, "closed"))
            return result

        with patch.object(Client, "call_tool", observe), patch.object(Client, "__aexit__", closed):
            receipts = asyncio.run(run_planning_interop(self.f.service))
        self.assertEqual(len(receipts), 3)
        self.assertTrue(all(isinstance(receipt, MutationReceipt) for receipt in receipts))
        self.assertEqual(len({receipt.current_metadata.plan_id for receipt in receipts}), 1)
        self.assertEqual([receipt.current_metadata.revision for receipt in receipts], [1, 2, 3])
        self.assertEqual(len(sessions), 3)
        self.assertEqual(len(set(protocols)), 1)
        self.assertNotIn(protocols[0], ("None", ""))
        for index, client in enumerate(sessions):
            names = [name for observed, name in actions if observed is client]
            self.assertEqual(names[0], "read_service_info")
            self.assertEqual(names[-1], "closed")
            self.assertIn("create_plan" if index == 0 else "revise_plan", names)
            if index:
                self.assertLess(actions.index((sessions[index - 1], "closed")),
                                actions.index((client, "read_service_info")))
                self.assertLess(names.index("read_plan"), names.index("revise_plan"))
        plan_id = receipts[0].current_metadata.plan_id
        canonical = self.f.service.read_plan(plan_id)
        self.assertEqual(canonical.draft.facts, PLANNING_FACTS)
        self.assertEqual(canonical.metadata.revision, 3)
        self.assertEqual(canonical.draft.authorization.mode, "plan-only")
        self.assertTrue(canonical.draft.authorization.interval)
        self.assertEqual(canonical.draft.authorization.since_revision, 1)
        events = self.f.service.read_events(plan_id)
        self.assertEqual([event.kind for event in events], ["create_plan", "revise_plan", "revise_plan"])
        self.assertEqual([event.plan_revision for event in events], [1, 2, 3])
        self.assertEqual([event.actor for event in events], ["sdk-session-1", "sdk-session-2", "sdk-session-3"])

    def _examples(self, client, language):
        path = Path(__file__).resolve().parents[1] / "docs" / "clients" / f"{client}.md"
        self.assertTrue(path.is_file(), f"The {client} client schema documentation is missing")
        return re.findall(r"```" + language + r"\n(.*?)\n```", path.read_text(), re.DOTALL)

    def _endpoint(self, url):
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.port, parsed.path),
                         ("http", "127.0.0.1", 8790, "/mcp"))
        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)
        self.assertEqual((parsed.query, parsed.fragment), ("", ""))

    def test_codex_examples_use_cli_environment_or_private_toml_header(self):
        snippets = self._examples("codex", "toml")
        self.assertEqual(len(snippets), 2)
        environment, private = [tomllib.loads(snippet)["mcp_servers"]["adaptivePlanning"]
                                for snippet in snippets]
        for entry in (environment, private):
            self._endpoint(entry["url"])
        self.assertEqual(set(environment), {"url", "bearer_token_env_var"})
        self.assertEqual(environment["bearer_token_env_var"], "ADAPTIVE_PLANNING_TOKEN")
        self.assertEqual(set(private), {"url", "http_headers"})
        self.assertEqual(private["http_headers"], {"Authorization": "Bearer SYNTHETIC_ONLY"})
        commands = self._examples("codex", "sh")
        self.assertEqual(shlex.split(commands[0]), [
            "codex", "mcp", "add", "adaptivePlanning", "--url", "http://127.0.0.1:8790/mcp",
            "--bearer-token-env-var", "ADAPTIVE_PLANNING_TOKEN",
        ])

    def test_antigravity_example_uses_literal_private_authorization(self):
        snippets = self._examples("antigravity", "json")
        self.assertEqual(len(snippets), 1)
        entry = json.loads(snippets[0])["mcpServers"]["adaptivePlanning"]
        self.assertEqual(set(entry), {"serverUrl", "headers"})
        self._endpoint(entry["serverUrl"])
        self.assertEqual(entry["headers"], {"Authorization": "Bearer SYNTHETIC_ONLY"})

    def test_copilot_cli_and_vscode_examples_use_their_distinct_secret_schemas(self):
        snippets = self._examples("copilot", "json")
        self.assertEqual(len(snippets), 2)
        cli, vscode = [json.loads(snippet) for snippet in snippets]
        entry = cli["mcpServers"]["adaptivePlanning"]
        self.assertEqual(set(entry), {"type", "url", "headers", "tools"})
        self.assertEqual(entry["type"], "http")
        self._endpoint(entry["url"])
        self.assertEqual(entry["headers"], {"Authorization": "Bearer ${ADAPTIVE_PLANNING_TOKEN}"})
        self.assertEqual(entry["tools"], ["*"])
        self.assertEqual(set(vscode), {"servers", "inputs"})
        extension = vscode["servers"]["adaptivePlanning"]
        self.assertEqual(extension["type"], "http")
        self._endpoint(extension["url"])
        self.assertEqual(extension["headers"], {"Authorization": "Bearer ${input:adaptive-planning-token}"})
        self.assertEqual(vscode["inputs"], [{"type": "promptString", "id": "adaptive-planning-token",
                                             "description": "Installation token", "password": True}])

    def test_inherited_wire_scenario_keeps_private_grants_inside_memory_capture(self):
        grants = []
        original = Client.call_tool

        async def observe(client, name, arguments):
            result = await original(client, name, arguments)
            value = result.structured_content
            if isinstance(value, dict) and isinstance(value.get("private"), dict):
                grant = value["private"].get("orchestrator_token")
                if grant:
                    grants.append(grant)
            return result

        before = _logging_state()
        with _private_emission_stream() as stream:
            result = unittest.TestResult()
            case = sdk.TestService("test_shared_app_two_protocols_wire_errors_and_lifecycle")
            with patch.object(Client, "call_tool", observe):
                case.run(result)
            self.assertTrue(result.wasSuccessful(), "The inherited SDK wire scenario failed")
            self.assertEqual(len(grants), 4)
            emitted_private_grant = any(grant in stream.getvalue() for grant in grants)
        self.assertTrue(all(_logging_state()[logger] == state for logger, state in before.items()),
                        "The wire scenario did not restore existing logging state")
        self.assertFalse(emitted_private_grant, "SDK private wire diagnostics escaped the memory capture")

    def test_private_sdk_logging_boundary_restores_state_after_failure(self):
        boundary = getattr(sdk, "_private_sdk_client_logs", None)
        self.assertIsNotNone(boundary, "The private SDK logging boundary is missing")
        captured = CapturedLogs()
        root = logging.getLogger()
        root.addHandler(captured)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        probe = logging.getLogger("mcp.client.interop_probe")
        probe_state = (probe.level, probe.disabled, probe.propagate)
        marker = logging.Filter()
        child_stream = io.StringIO()
        child_handler = logging.StreamHandler(child_stream)
        child_handler.addFilter(marker)
        late_stream = io.StringIO()
        late_handler = logging.StreamHandler(late_stream)
        late_handler.addFilter(marker)
        fallback_stream = io.StringIO()
        fallback_handler = logging.StreamHandler(fallback_stream)
        fallback_handler.addFilter(marker)
        previous_fallback = logging.lastResort
        fallback_probe = logging.getLogger("mcp.client.unmaterialized.fallback_probe")
        fallback_state = (fallback_probe.level, fallback_probe.disabled, fallback_probe.propagate)
        fallback_probe.setLevel(logging.DEBUG)
        fallback_probe.disabled = False
        fallback_probe.propagate = False
        logging.lastResort = fallback_handler
        probe.addFilter(marker)
        probe.addHandler(child_handler)
        probe.setLevel(logging.DEBUG)
        probe.disabled = False
        probe.propagate = True
        previous_factory = logging.getLogRecordFactory()
        factory_records = []

        def existing_factory(*args, **kwargs):
            record = previous_factory(*args, **kwargs)
            factory_records.append(record.name)
            return record

        logging.setLogRecordFactory(existing_factory)
        try:
            with _private_emission_stream() as stream:
                before = _logging_state(probe, fallback_probe)
                with self.assertRaisesRegex(RuntimeError, "synthetic boundary failure"):
                    with boundary(captured):
                        probe.addHandler(late_handler)
                        try:
                            probe.debug("synthetic private wire marker")
                            fallback_probe.warning("synthetic fallback private marker")
                            logging.getLogger("adaptive_planning.interop_probe").warning("synthetic service marker")
                        finally:
                            probe.removeHandler(late_handler)
                        raise RuntimeError("synthetic boundary failure")
                self.assertTrue(all(_logging_state(probe, fallback_probe)[logger] == state for logger, state in before.items()),
                                "The failed boundary did not restore existing logging state")
                self.assertNotIn("synthetic private wire marker", stream.getvalue())
                self.assertEqual(child_stream.getvalue(), "")
                self.assertEqual(late_stream.getvalue(), "")
                self.assertEqual(tuple(late_handler.filters), (marker,))
                self.assertEqual(fallback_stream.getvalue(), "")
                self.assertIn("synthetic service marker", stream.getvalue())
            self.assertTrue(any("synthetic private wire marker" in line for line in captured.lines))
            self.assertIn(probe.name, factory_records)
            self.assertIn("adaptive_planning.interop_probe", factory_records)
        finally:
            logging.setLogRecordFactory(previous_factory)
            logging.lastResort = previous_fallback
            fallback_probe.setLevel(fallback_state[0])
            fallback_probe.disabled, fallback_probe.propagate = fallback_state[1:]
            probe.removeHandler(child_handler)
            probe.removeFilter(marker)
            probe.setLevel(probe_state[0])
            probe.disabled, probe.propagate = probe_state[1:]
            child_handler.close()
            child_stream.close()
            late_handler.close()
            late_stream.close()
            fallback_handler.close()
            fallback_stream.close()
            root.removeHandler(captured)
            root.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
