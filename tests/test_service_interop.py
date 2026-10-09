"""Shared planning and worker checkpoints over real SDK HTTP sessions.

Synthetic SDK sessions and documented schemas are separate from installed product acceptance.
Credentials and private SDK client diagnostics stay in test-owned memory.
"""

import asyncio
import copy
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
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
from adaptive_planning.ownership import require_kind_capabilities
from adaptive_planning.types import (
    Availability, BOOTSTRAP_KINDS, DomainError, Event, Metadata, MutationReceipt,
    ORCHESTRATOR_KINDS, PrivateGrant, Review, WORKER_KINDS, parse_command,
)
from tests.service_fixtures import checkpoint, context, draft, make_service, step, workspace
from tests import test_service_sdk as sdk
from tests.test_service_http import CapturedLogs, SYNTHETIC_TOKEN


PLANNING_FACTS = ("Creator SDK planning fact", "Second SDK planning fact", "Third SDK planning fact")
PLANNING_TOOLS = {"read_service_info", "list_plans", "read_plan", "read_events", "list_ready",
                  "read_coordination", "create_plan", "import_plan", "revise_plan", "takeover"}
WORKER_TOOLS = PLANNING_TOOLS | {
    "register_resource", "reconcile_resources", "assign_step", "bind_availability",
    "start_step", "report_activity", "record_checkpoint", "resolve_reassessment",
    "invalidate_result", "resolve_invalidation", "record_review", "accept_step",
    "reserve_verification", "release_resource_use", "reopen_attempt",
    "sweep_stale", "resolve_attempt", "append_late_evidence",
}
DISABLED_FUTURE_TOOLS = {
    "resolve_store_hold",
}
WORKER_ORIGINS = ("codex-worker", "agy-worker", "copilot-worker")
SDK_PROTOCOLS = {"auto": "2026-07-28", "legacy": "2025-11-25"}


class _WorkerWire:
    """Test-local role handling; every mutation goes through an actual SDK client."""

    def __init__(self, clients, info):
        self.clients = clients
        self.info = info
        self.plan_id = None
        self.identity = None
        self.owner_token = None
        self.attempt_tokens = {}
        self.private_tokens = [SYNTHETIC_TOKEN]

    def public(self, value):
        encoded = json.dumps(value, sort_keys=True)
        assert all(token not in encoded for token in self.private_tokens), "A public projection exposed a private credential"
        assert "orchestrator_token" not in encoded and "attempt_token" not in encoded, "A public projection exposed grant fields"
        return value

    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        value = result.structured_content
        assert not result.is_error and isinstance(value, dict), f"The synthetic SDK {name} call failed"
        if name in {"read_service_info", "list_plans", "read_plan", "read_events", "list_ready", "read_coordination"}:
            self.public(value)
        return value

    async def send(self, client, command):
        value = await self.call(client, command.kind, {"command": plans.to_jsonable(command)})
        private = value["private"]
        if private is not None:
            self.private_tokens.extend(token for token in private.values() if token)
        self.public(value["committed"])
        return value

    def envelope(self, kind, payload, metadata=None, target=None, *, actor="sdk-owner", token=None, attempt_token=None):
        return parse_command({
            "kind": kind, "epoch": self.info["epoch"], "key": uuid.uuid4().hex, "actor": actor,
            "plan_id": self.identity["plan_id"] if self.identity else None,
            "generation": self.identity["generation"] if self.identity else None,
            "expected_revision": metadata["revision"] if metadata else None,
            "ownership_version": metadata["ownership_version"] if metadata else None,
            "orchestrator_token": token, "attempt_id": target["attempt_id"] if target else None,
            "attempt_token": attempt_token, "attempt_version": target["version"] if target else None,
            "definition_version": target["definition_version"] if target else None, "payload": payload,
        })

    async def create(self, proposed, creation_id):
        command = self.envelope("create_plan", {
            "creation_id": creation_id, "draft": proposed,
            "new_plan_intent": "Separate synthetic SDK interoperability scenario",
        })
        value = await self.send(self.clients[0], command)
        self.identity = value["committed_metadata"]
        self.plan_id = self.identity["plan_id"]
        self.owner_token = value["private"]["orchestrator_token"]
        assert self.owner_token, "Creation omitted its private owner grant"
        return value

    async def coordination(self):
        items, cursor = [], None
        while True:
            page = await self.call(self.clients[0], "read_coordination", {
                "plan_id": self.plan_id, "after": cursor, "limit": 2,
            })
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return items

    async def target(self, attempt_id):
        targets = [item["value"] for item in await self.coordination()
                   if item["kind"] == "attempt" and item["value"]["attempt_id"] == attempt_id]
        assert len(targets) == 1, "The exact public attempt target is missing or ambiguous"
        return targets[0]

    async def owner_command(self, kind, payload, attempt_id=None):
        current = await self.call(self.clients[0], "read_plan", {"plan_id": self.plan_id})
        target = await self.target(attempt_id) if attempt_id else None
        return self.envelope(kind, payload, current["metadata"], target, token=self.owner_token)

    async def owner(self, kind, payload, attempt_id=None):
        return await self.send(self.clients[0], await self.owner_command(kind, payload, attempt_id))

    async def assign(self, step_id, worker, execution):
        value = await self.owner("assign_step", {"step_id": step_id, "assignee": worker, "context": execution})
        attempt_id = value["committed"]["attempt_id"]
        self.attempt_tokens[attempt_id] = value["private"]["attempt_token"]
        assert self.attempt_tokens[attempt_id], "Assignment omitted its private attempt grant"
        return attempt_id

    async def worker_command(self, kind, payload, attempt_id, worker):
        # Direct workers carry their own current target, never global owner CAS.
        target = await self.target(attempt_id)
        return self.envelope(kind, payload, target=target, actor=worker,
                             attempt_token=self.attempt_tokens[attempt_id])

    async def worker(self, client_index, kind, payload, attempt_id, worker):
        return await self.send(self.clients[client_index], await self.worker_command(kind, payload, attempt_id, worker))

    async def events(self):
        events, after = [], 0
        while True:
            page = await self.call(self.clients[0], "read_events", {"plan_id": self.plan_id, "after": after, "limit": 2})
            if not page["events"]:
                return tuple(Event(**event) for event in events)
            assert all(event["sequence"] > after for event in page["events"]), "Event pagination repeated an old page"
            events.extend(page["events"])
            after = events[-1]["sequence"]

    async def state(self):
        plan = await self.call(self.clients[0], "read_plan", {"plan_id": self.plan_id})
        return plans.to_jsonable((plan, await self.coordination(), await self.events()))

    async def refuse(self, client, command, code):
        before = await self.state()
        result = await client.call_tool(command.kind, {"command": plans.to_jsonable(command)})
        value = result.structured_content
        assert result.is_error and isinstance(value, dict), "The forbidden wire mutation succeeded"
        self.public(value)
        assert value["error"]["code"] == code, "The wire refusal has a different domain code"
        assert await self.state() == before, "A refused wire mutation changed public state"


@asynccontextmanager
async def _worker_wire(service, mode, client_count=3):
    """Own and settle one production HTTP listener and one serial logging boundary."""
    captured = CapturedLogs()
    root = logging.getLogger()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        config = Config(service._store.path.parent, base_url=f"http://127.0.0.1:{sock.getsockname()[1]}")
        token_path = config.data_dir / "service-token"
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as token_file:
                token_file.write(SYNTHETIC_TOKEN)
            app = build_app(config)  # Real app clock; all live state is created below over HTTP.
            root.addHandler(captured)
            try:
                with sdk._private_sdk_client_logs(captured):
                    async with sdk.running_service(app, sock):
                        app.middleware_stack = sdk.RequestCapturingMiddleware(app.middleware_stack)
                        async with AsyncExitStack() as stack:
                            clients, infos = [], []
                            for _ in range(client_count):
                                http = await stack.enter_async_context(httpx2.AsyncClient(
                                    headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"}, follow_redirects=False))
                                client = await stack.enter_async_context(Client(
                                    streamable_http_client(config.base_url + "/mcp", http_client=http), mode=mode))
                                assert str(client.protocol_version) == SDK_PROTOCOLS[mode], "The worker protocol negotiation differs"
                                names = {tool.name for tool in (await client.list_tools()).tools}
                                assert names == WORKER_TOOLS and not names & DISABLED_FUTURE_TOOLS, "The tested worker catalog differs"
                                result = await client.call_tool("read_service_info", {})
                                assert not result.is_error and isinstance(result.structured_content, dict), "Installation discovery failed"
                                clients.append(client)
                                infos.append(result.structured_content)
                            assert len({id(client) for client in clients}) == client_count, "SDK clients were reused"
                            assert all(info == infos[0] for info in infos), "The sessions found different installations"
                            assert infos[0]["epoch"] == service.read_service_info().epoch, "The app reopened a different installation"
                            wire = _WorkerWire(tuple(clients), infos[0])
                            wire.public(infos[0])
                            yield wire
                            requests = [request for request in app.middleware_stack.captured_requests
                                        if request.method in {"tools/list", "tools/call"}]
                            assert requests, "The scenario produced no actual MCP wire requests"
                            assert all(request.path == "/mcp" and
                                       request.headers.get("authorization") == f"Bearer {SYNTHETIC_TOKEN}" and
                                       request.headers.get("mcp-protocol-version") == SDK_PROTOCOLS[mode]
                                       for request in requests), "The authenticated wire protocol/header boundary differs"
            finally:
                root.removeHandler(captured)
        finally:
            token_path.unlink()
        assert sock.fileno() == -1, "The worker listener was not settled"


def _finished_checkpoint(worker, step_id, *, workspace_id=None, affected=()):
    value = checkpoint(affected=affected, artifact_version=f"{step_id}-artifact-v1")
    artifact = replace(value.artifact, identity=f"synthetic/{step_id}", summary=f"Synthetic finished {step_id} output")
    check = f"Check for step {step_id}"
    evidence = replace(value.evidence[0], artifact=artifact, workspace_id=workspace_id, check=check,
                       summary=f"Synthetic {worker} finished output passed its bounded check",
                       limitations=("SDK/domain proof only; no installed client, model or host liveness certification",))
    reassessment = replace(value.reassessment, summary=f"Reassessed {step_id} after its check",
                          facts=(f"Observed synthetic {step_id} finished artifact",),
                          assumptions=("Synthetic resource declarations remain bounded to this test",))
    return replace(value, origin_worker=worker, artifact=artifact, check=check,
                   evidence=(evidence,), reassessment=reassessment)


async def run_worker_interop(service: PlanningService) -> tuple[Event, ...]:
    """Return this auto-mode run's three actual canonical finished checkpoints."""
    return await _run_worker_interop(service, "auto")


async def _run_worker_interop(service: PlanningService, mode: str) -> tuple[Event, ...]:
    async with _worker_wire(service, mode) as wire:
        definitions = (step("codex"), step("agy"), step("copilot", review_required=True, final_gate=True))
        await wire.create(draft(steps=definitions), f"worker-interop-{mode}-{uuid.uuid4().hex}")
        attempts, resources, values, submissions, originals, accepts = {}, {}, {}, {}, {}, {}
        for index, worker in enumerate(WORKER_ORIGINS):
            step_id = definitions[index].step_id
            registered = await wire.owner("register_resource", {"claim": workspace(f"/synthetic/{mode}/{step_id}", 900 + index)})
            resources[step_id] = registered["committed"]["resource_id"]
            attempts[step_id] = await wire.assign(step_id, worker, context(resources[step_id]))
        for index, worker in enumerate(WORKER_ORIGINS):
            await wire.call(wire.clients[index], "read_plan", {"plan_id": wire.plan_id})
            await wire.worker(index, "start_step", {"origin_worker": worker, "relay": None,
                                                    "observed_activity": f"Started bounded synthetic {definitions[index].step_id}"},
                              attempts[definitions[index].step_id], worker)
        # Owner acceptance/reassessment/review operations interleave worker origins.
        for index in (1, 0, 2):
            step_id, worker = definitions[index].step_id, WORKER_ORIGINS[index]
            value = _finished_checkpoint(worker, step_id, workspace_id=resources[step_id],
                                         affected=(step_id,) if index == 0 else ())
            values[step_id] = value
            original = await wire.worker_command("record_checkpoint", {"checkpoint": value}, attempts[step_id], worker)
            originals[step_id] = original
            submissions[step_id] = await wire.send(wire.clients[index], original)
            assert submissions[step_id]["private"] is None, "Submission returned a new grant"
            acceptance = {"step_id": step_id, "artifact": value.artifact, "stopped_resource_ids": (), "stopped_reason": None}
            if index == 0:
                await wire.refuse(wire.clients[0], await wire.owner_command("accept_step", acceptance, attempts[step_id]), "held")
                await wire.owner("resolve_reassessment", {
                    "checkpoint_id": submissions[step_id]["committed"]["checkpoint_id"], "decision": "reject",
                    "reason": "Owner explicitly classified the synthetic provisional effect as resolved",
                    "draft": None, "affected_steps": (), "unbounded": False,
                })
            if definitions[index].review_required:
                await wire.refuse(wire.clients[0], await wire.owner_command("accept_step", acceptance, attempts[step_id]), "invalid")
                await wire.owner("record_review", {"step_id": step_id,
                    "review": Review("synthetic-independent-reviewer", value.artifact, "passed", (), value.evidence, None)})
            accepts[step_id] = await wire.owner("accept_step", acceptance, attempts[step_id])
            assert accepts[step_id]["private"] is None, "Acceptance returned a new grant"
        public = await wire.call(wire.clients[0], "read_plan", {"plan_id": wire.plan_id})
        assert public["status"] != "complete" and public["coordination"]["total_counts"]["owned_nonreleased_uses"] == 3, "Acceptance silently released physical obligations"
        reservations = [item["value"] for item in await wire.coordination() if item["kind"] == "reservation"]
        assert {reservation["attempt_id"] for reservation in reservations} == set(attempts.values()), "Original resource obligations were lost"
        assert all(reservation["writer_active"] for reservation in reservations), "Acceptance silently stopped synthetic writers"
        for step_id, worker in zip(("codex", "agy", "copilot"), WORKER_ORIGINS):
            original = originals[step_id]
            retry = await wire.send(wire.clients[WORKER_ORIGINS.index(worker)], original)
            assert retry["replayed"] and retry["committed"] == submissions[step_id]["committed"], "Accepted direct retry lost its immutable receipt"
            await wire.refuse(wire.clients[WORKER_ORIGINS.index(worker)], await wire.worker_command(
                "report_activity", {"origin_worker": worker, "relay": None, "observed_activity": "New terminal progress is refused"},
                attempts[step_id], worker), "invalid")
            # These are explicit synthetic stop facts, never real host certification.
            use = next(reservation for reservation in reservations if reservation["attempt_id"] == attempts[step_id])
            await wire.owner("release_resource_use", {"use_id": use["use_id"], "resource_ids": (resources[step_id],),
                "reason": "Synthetic test writer explicitly declared stopped after finished checkpoint", "stopped": True})
        final = await wire.call(wire.clients[0], "read_plan", {"plan_id": wire.plan_id})
        assert final["status"] == "complete" and not final["coordination"]["has_unresolved_uses"], "Explicit resource release did not complete the bounded plan"
        events = await wire.events()
        checkpoints = tuple(event for event in events if event.kind == "record_checkpoint")
        assert len(checkpoints) == 3 and {event.origin_worker for event in checkpoints} == set(WORKER_ORIGINS), "Finished worker origins differ"
        for event in checkpoints:
            step_id = event.payload["step_id"]
            assert event.actor == event.origin_worker and event.relay is None, "Direct worker provenance changed"
            assert event.payload["attempt_id"] == attempts[step_id], "Checkpoint targets a different attempt"
            assert str(event.sequence) == submissions[step_id]["committed"]["checkpoint_id"], "Checkpoint ID is not its actual event sequence"
            assert event.payload["checkpoint"] == plans.to_jsonable(values[step_id]), "Checkpoint evidence/reassessment changed"
            accepted = next(item for item in events if item.kind == "accept_step" and item.payload["step_id"] == step_id)
            result_id = accepts[step_id]["committed"]["result_version"]
            assert str(uuid.UUID(result_id)) == result_id and result_id != values[step_id].artifact.version, "Accepted result is not its own durable UUID"
            assert accepted.payload["result_version"] == result_id and accepted.payload["checkpoint_sequence"] == event.sequence, "Acceptance lost its original checkpoint proof"
            assert accepted.payload["artifact"] == plans.to_jsonable(values[step_id].artifact), "Acceptance changed the full artifact"
            if step_id == "copilot":
                review = next(item for item in events if item.kind == "record_review")
                assert accepted.payload["review_id"] == review.payload["review_id"] and accepted.payload["review_sequence"] == review.sequence, "Acceptance lost its applicable original review"
                assert result_id != review.payload["review_id"], "Result UUID was confused with review UUID"
        assert len({event.payload["attempt_id"] for event in checkpoints}) == 3, "Workers shared an attempt"
        return checkpoints


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
                                        assert PLANNING_TOOLS <= names, "The required planning tool surface differs"
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

    def test_three_clients_full_worker_checkpoint_cycle(self):
        sessions, actions, grants, creations = [], [], [], []
        original = Client.call_tool

        async def observe(client, name, arguments):
            if not any(observed is client for observed, _ in sessions):
                sessions.append((client, str(client.protocol_version)))
            command = arguments.get("command", {})
            actions.append((client, name, command.get("actor")))
            if name == "create_plan":
                creations.append(command["payload"]["creation_id"])
            result = await original(client, name, arguments)
            value = result.structured_content
            if isinstance(value, dict) and isinstance(value.get("private"), dict):
                grants.extend(token for token in value["private"].values() if token)
            return result

        async def exercise():
            # Fixed public signature returns its own auto run; legacy is separate.
            auto = await run_worker_interop(self.f.service)
            legacy = await _run_worker_interop(self.f.service, "legacy")
            return auto, legacy

        before = _logging_state()
        with _private_emission_stream() as stream:
            with patch.object(Client, "call_tool", observe):
                runs = asyncio.run(exercise())
            self.assertFalse(any(token in stream.getvalue() for token in grants), "Private SDK diagnostics escaped memory")
        self.assertTrue(all(_logging_state()[logger] == state for logger, state in before.items()),
                        "Worker HTTP scenarios did not restore logging state")
        self.assertEqual(len(sessions), 6)
        self.assertEqual(len(creations), 2)
        self.assertEqual(len(set(creations)), 2)
        self.assertEqual(len({events[0].plan_id for events in runs}), 2)
        for (mode, version), checkpoints in zip(SDK_PROTOCOLS.items(), runs):
            with self.subTest(mode=mode):
                self.assertIsInstance(checkpoints, tuple)
                self.assertEqual(len(checkpoints), 3)
                self.assertTrue(all(isinstance(event, Event) for event in checkpoints))
                self.assertEqual({event.origin_worker for event in checkpoints}, set(WORKER_ORIGINS))
                self.assertEqual([event.origin_worker for event in checkpoints], ["agy-worker", "codex-worker", "copilot-worker"])
                self.assertEqual(len({event.sequence for event in checkpoints}), 3)
                self.assertEqual(len({event.plan_revision for event in checkpoints}), 3)
                clients = [client for client, protocol in sessions if protocol == version]
                self.assertEqual(len(clients), 3)
                observed_protocol = next(protocol for client, protocol in sessions if client is clients[0])
                for client, worker in zip(clients, WORKER_ORIGINS):
                    kinds = [(name, actor) for observed, name, actor in actions if observed is client]
                    self.assertIn(("start_step", worker), kinds)
                    self.assertIn(("record_checkpoint", worker), kinds)
                plan_id = checkpoints[0].plan_id
                canonical, after = [], 0
                while True:
                    page = self.f.service.read_events(plan_id, after=after, limit=2)
                    if not page:
                        break
                    canonical.extend(page)
                    after = page[-1].sequence
                self.assertEqual(tuple(event for event in canonical if event.kind == "record_checkpoint"), checkpoints)
                accepted = [event for event in canonical if event.kind == "accept_step"]
                self.assertEqual(len(accepted), 3)
                self.assertTrue(all(event.actor == "sdk-owner" and event.origin_worker is None for event in accepted))
                proof = {"scope": "synthetic official SDK only", "mode": mode, "protocol": observed_protocol,
                         "sdk_clients": len(clients), "plan_id": plan_id, "creation_id": creations[list(SDK_PROTOCOLS).index(mode)],
                         "checkpoints": [{"event_sequence": event.sequence, "checkpoint_id": str(event.sequence),
                             "plan_revision": event.plan_revision, "origin_worker": event.origin_worker,
                             "actor": event.actor, "relay": event.relay, "attempt_id": event.payload["attempt_id"],
                             "attempt_version": event.payload["version"], "definition_version": event.payload["definition_version"],
                             "artifact": event.payload["checkpoint"]["artifact"]} for event in checkpoints],
                         "acceptance": [{"event_sequence": event.sequence, **event.payload} for event in accepted]}
                # Intentional public, synthetic proof goes into the private test capture.
                print("TASK20_SYNTHETIC_WORKER_PROOF " + json.dumps(proof, sort_keys=True))

    def test_kind_capability_matrix(self):
        owner, attempt = "synthetic-owner-token", "synthetic-attempt-token"

        def rejects(kind, relay, owner_token, attempt_token, code):
            with self.subTest(kind=kind, relay=relay, owner_present=owner_token is not None,
                              attempt_present=attempt_token is not None), self.assertRaises(DomainError) as error:
                require_kind_capabilities(kind, relay, owner_token, attempt_token)
            self.assertEqual(error.exception.code, code)

        for kind in ORCHESTRATOR_KINDS:
            require_kind_capabilities(kind, None, owner, None)
            rejects(kind, None, None, None, "forbidden")
            rejects(kind, None, None, attempt, "invalid")
            rejects(kind, None, owner, attempt, "invalid")
        for kind in BOOTSTRAP_KINDS:
            require_kind_capabilities(kind, None, None, None)
            rejects(kind, None, owner, None, "invalid")
            rejects(kind, None, None, attempt, "invalid")
            rejects(kind, None, owner, attempt, "invalid")
        for kind in WORKER_KINDS:
            require_kind_capabilities(kind, None, None, attempt)
            rejects(kind, None, None, None, "forbidden")
            rejects(kind, None, owner, None, "invalid")
            rejects(kind, None, owner, attempt, "invalid")
            require_kind_capabilities(kind, "synthetic-owner", owner, attempt)
            rejects(kind, "synthetic-owner", None, None, "forbidden")
            rejects(kind, "synthetic-owner", owner, None, "forbidden")
            rejects(kind, "synthetic-owner", None, attempt, "forbidden")
        rejects("unknown-kind", None, None, None, "invalid")

    def test_direct_worker_after_takeover_and_old_relay_fenced(self):
        p = self.f.create(steps=(step("a"),))
        assigned = self.f.assign(p)
        active = self.f.start(p, assigned)
        owner_actor = "synthetic-orchestrator"
        relay = replace(self.f.command("report_activity", {
            "origin_worker": "worker-a", "relay": owner_actor, "observed_activity": "Initially valid owner relay",
        }, receipt=p, attempt=active), actor=owner_actor)
        self.f.service.execute(relay)
        value = checkpoint()
        old_checkpoint = replace(self.f.command("record_checkpoint", {
            "checkpoint": replace(value, relay=owner_actor),
        }, receipt=p, attempt=active), actor=owner_actor)
        current = self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": p.current_metadata.ownership_version, "reason": "Explicit synthetic takeover",
        }, receipt=p))
        for old in (relay, old_checkpoint):
            with self.subTest(kind=old.kind), self.assertRaises(DomainError) as error:
                self.f.service.execute(old)
            self.assertEqual(error.exception.code, "fenced")
        direct = replace(self.f.command("record_checkpoint", {"checkpoint": value}, receipt=current, attempt=active,
                                        key="direct-after-takeover"), expected_revision=None, ownership_version=None)
        self.assertIsNone(direct.orchestrator_token)
        self.assertIsNone(direct.expected_revision)
        self.assertIsNone(direct.ownership_version)
        invalid = (
            (replace(direct, orchestrator_token=current.private.orchestrator_token), "invalid"),
            (replace(direct, expected_revision=current.current_metadata.revision), "invalid"),
            (replace(direct, actor="another-worker"), "forbidden"),
            (replace(direct, payload={"checkpoint": replace(value, origin_worker="another-worker")}), "forbidden"),
        )
        for command, code in invalid:
            with self.subTest(code=code), self.assertRaises(DomainError) as error:
                self.f.service.execute(command)
            self.assertEqual(error.exception.code, code)
        submitted = self.f.service.execute(direct)
        accepted = self.f.accept(current, submitted, value)
        self.assertNotEqual(accepted.committed["result_version"], value.artifact.version)
        events = self.f.service.read_events(current.current_metadata.plan_id)
        replayed = self.f.service.execute(direct)  # Exact original envelope/key, not reconstructed current CAS.
        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.committed, submitted.committed)
        self.assertEqual(self.f.service.read_events(current.current_metadata.plan_id), events)
        for command in (
            replace(direct, key="new-terminal-checkpoint", attempt_version=accepted.committed["version"]),
            replace(self.f.command("report_activity", {"origin_worker": "worker-a", "relay": None,
                "observed_activity": "New activity after acceptance"}, receipt=current, attempt=submitted),
                expected_revision=None, ownership_version=None),
        ):
            with self.subTest(kind=command.kind), self.assertRaises(DomainError) as error:
                self.f.service.execute(command)
            self.assertEqual(error.exception.code, "invalid")
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(replace(direct, attempt_token="incorrect-synthetic-token"))
        self.assertEqual(error.exception.code, "forbidden")
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(relay)
        self.assertEqual(error.exception.code, "fenced")

    def test_sdk_accept_availability_start_invalidate_and_explicit_remediation(self):
        async def exercise(mode):
            async with _worker_wire(self.f.service, mode, client_count=2) as wire:
                await wire.create(draft(steps=(step("producer", review_required=True),
                    step("consumer", ("producer",), review_required=True), step("waiting", ("producer",)))),
                    f"availability-interop-{mode}-{uuid.uuid4().hex}")
                producer = await wire.assign("producer", "availability-producer", context(None, writes=False))
                await wire.worker(1, "start_step", {"origin_worker": "availability-producer", "relay": None,
                    "observed_activity": "Bounded synthetic producer started"}, producer, "availability-producer")
                produced = _finished_checkpoint("availability-producer", "producer")
                checkpoint_receipt = await wire.worker(1, "record_checkpoint", {"checkpoint": produced}, producer, "availability-producer")
                await wire.owner("record_review", {"step_id": "producer",
                    "review": Review("availability-reviewer", produced.artifact, "passed", (), produced.evidence, None)})
                accepted = await wire.owner("accept_step", {"step_id": "producer", "artifact": produced.artifact,
                    "stopped_resource_ids": (), "stopped_reason": None}, producer)
                result_id = accepted["committed"]["result_version"]
                assert str(uuid.UUID(result_id)) == result_id and result_id != produced.artifact.version, "Availability substituted artifact version for accepted UUID"
                consumer = await wire.assign("consumer", "availability-consumer", context(None, writes=False))
                start_payload = {"origin_worker": "availability-consumer", "relay": None,
                                 "observed_activity": "Start only after final-base availability"}
                await wire.refuse(wire.clients[1], await wire.worker_command("start_step", start_payload, consumer, "availability-consumer"), "invalid")
                checked_base = f"synthetic-final-consumer-base-{mode}"
                binding = Availability("producer", result_id, "ancestor", checked_base, None, produced.evidence[0])
                wrong = replace(binding, evidence=replace(binding.evidence,
                                artifact=replace(produced.artifact, summary="A different full artifact")))
                await wire.refuse(wire.clients[0], await wire.owner_command("bind_availability", {
                    "bindings": (wrong,), "checked_base": checked_base}, consumer), "invalid")
                bound = await wire.owner("bind_availability", {"bindings": (binding,), "checked_base": checked_base}, consumer)
                started = await wire.worker(1, "start_step", start_payload, consumer, "availability-consumer")
                assert started["committed"]["version"] > bound["committed"]["version"], "Start did not consume its current own target"
                consumed = _finished_checkpoint("availability-consumer", "consumer")
                await wire.worker(1, "record_checkpoint", {"checkpoint": consumed}, consumer, "availability-consumer")
                await wire.owner("record_review", {"step_id": "consumer",
                    "review": Review("availability-reviewer", consumed.artifact, "passed", (), consumed.evidence, None)})
                consumer_accept = await wire.owner("accept_step", {"step_id": "consumer", "artifact": consumed.artifact,
                    "stopped_resource_ids": (), "stopped_reason": None}, consumer)
                waiting = await wire.assign("waiting", "waiting-consumer", context(None, writes=False))
                await wire.owner("bind_availability", {"bindings": (binding,), "checked_base": checked_base}, waiting)
                invalid = await wire.owner("invalidate_result", {"step_id": "producer", "result_version": result_id,
                    "reason": "Synthetic later observation invalidated the original producer assumption", "evidence": produced.evidence})
                invalidation_id = invalid["committed"]["invalidation_id"]

                async def held_steps():
                    return {item["value"]["step_id"] for item in await wire.coordination() if item["kind"] == "hold"}

                assert await held_steps() == {"producer", "consumer", "waiting"}, "Invalidation lost its exact dependency closure"
                # A producer hold blocks availability; an affected consumer hold blocks execution.
                await wire.refuse(wire.clients[0], await wire.owner_command("bind_availability", {
                    "bindings": (binding,), "checked_base": checked_base}, waiting), "held")
                waiting_start = {"origin_worker": "waiting-consumer", "relay": None, "observed_activity": "Held consumer cannot start"}
                await wire.refuse(wire.clients[1], await wire.worker_command("start_step", waiting_start, waiting, "waiting-consumer"), "held")
                resolution = {"invalidation_id": invalidation_id, "affected_step_id": "producer", "disposition": "revalidated",
                    "corrective_step_id": None, "corrected_result_version": result_id, "evidence": produced.evidence,
                    "reason": "Explicit revalidation of this exact original accepted artifact"}
                await wire.refuse(wire.clients[0], await wire.owner_command("resolve_invalidation", resolution), "invalid")
                post_review = await wire.owner("record_review", {"step_id": "producer",
                    "review": Review("post-invalidation-reviewer", produced.artifact, "passed", (), produced.evidence, None)})
                await wire.owner("resolve_invalidation", resolution)
                assert await held_steps() == {"consumer", "waiting"}, "Producer resolution automatically cleared consumer holds"
                # Producer resolution never overrides the consumer's own hold.
                await wire.refuse(wire.clients[0], await wire.owner_command("bind_availability", {
                    "bindings": (binding,), "checked_base": checked_base}, waiting), "held")
                await wire.refuse(wire.clients[1], await wire.worker_command("start_step", waiting_start, waiting, "waiting-consumer"), "held")
                await wire.owner("record_review", {"step_id": "consumer",
                    "review": Review("post-invalidation-reviewer", consumed.artifact, "passed", (), consumed.evidence, None)})
                await wire.owner("resolve_invalidation", {**resolution, "affected_step_id": "consumer",
                    "corrected_result_version": consumer_accept["committed"]["result_version"], "evidence": consumed.evidence})
                assert await held_steps() == {"waiting"}, "Named consumer resolution cleared an unremediated sibling"
                public = await wire.call(wire.clients[0], "read_plan", {"plan_id": wire.plan_id})
                assert public["coordination"]["result_versions"] == {
                    "producer": result_id, "consumer": consumer_accept["committed"]["result_version"]}, "Revalidation changed accepted UUIDs"
                events = await wire.events()
                producer_event = next(event for event in events if event.kind == "record_checkpoint" and event.payload["step_id"] == "producer")
                assert str(producer_event.sequence) == checkpoint_receipt["committed"]["checkpoint_id"], "Availability proof lost its real producer checkpoint"
                invalidation_event = next(event for event in events if event.kind == "invalidate_result")
                current_review = next(event for event in events if event.kind == "record_review" and event.payload["review_id"] == post_review["committed"]["review_id"])
                assert current_review.sequence > invalidation_event.sequence, "Revalidation reused a pre-invalidation review"
                assert current_review.payload["artifact"] == plans.to_jsonable(produced.artifact), "Revalidation reviewed another full artifact"
                print("TASK20_SYNTHETIC_AVAILABILITY_PROOF " + json.dumps({
                    "scope": "synthetic official SDK only", "mode": mode, "protocol": str(wire.clients[0].protocol_version),
                    "sdk_clients": 2, "plan_id": wire.plan_id, "producer_checkpoint_id": str(producer_event.sequence),
                    "producer_result_uuid": result_id, "consumer_result_uuid": consumer_accept["committed"]["result_version"],
                    "invalidation_id": invalidation_id, "invalidation_sequence": invalidation_event.sequence,
                    "post_invalidation_review_id": post_review["committed"]["review_id"], "post_invalidation_review_sequence": current_review.sequence,
                    "remaining_explicit_hold": "waiting", "checked_base": checked_base}, sort_keys=True))

        for mode in SDK_PROTOCOLS:
            with self.subTest(mode=mode):
                asyncio.run(exercise(mode))

    def test_sdk_plan_only_denies_execution_despite_worker_tool_visibility(self):
        async def exercise(mode):
            async with _worker_wire(self.f.service, mode, client_count=1) as wire:
                await wire.create(draft(mode="plan-only", steps=(step("a"),)), f"plan-only-worker-{mode}-{uuid.uuid4().hex}")
                public = await wire.call(wire.clients[0], "read_plan", {"plan_id": wire.plan_id})
                authorization = public["draft"]["authorization"]
                assert authorization["mode"] == "plan-only", "The test did not retain planning-only scope"
                await wire.refuse(wire.clients[0], await wire.owner_command("assign_step", {
                    "step_id": "a", "assignee": "scope-worker", "context": context(None, writes=False)}), "forbidden")
                assert not await wire.coordination(), "Planning-only refusal created an attempt or resource use"
                assert [event.kind for event in await wire.events()] == ["create_plan"], "Tool visibility executed planning-only work"
                final = await wire.call(wire.clients[0], "read_plan", {"plan_id": wire.plan_id})
                assert final["draft"]["authorization"] == authorization, "Refusal renewed execution authorization"

        for mode in SDK_PROTOCOLS:
            with self.subTest(mode=mode):
                asyncio.run(exercise(mode))

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
