"""Test suite proving the pinned MCP SDK and client transport contract."""

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import errno
import json
import logging
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.types import TextContent
import uvicorn

from adaptive_planning import plans
from tests.service_fixtures import draft, make_service
from tests.test_service_http import CapturedLogs, SYNTHETIC_TOKEN


@dataclass(frozen=True)
class SdkProbe:
    version: str
    tool_names: tuple[str, ...]
    echoed: str
    listener_closed: bool
    path: str


@dataclass
class CapturedRequest:
    path: str
    method: str | None
    headers: dict[str, str]
    body: bytes
    json_data: dict[str, Any] | None


class RequestCapturingMiddleware:
    def __init__(self, app: Any):
        self.app = app
        self.captured_requests: list[CapturedRequest] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            path = scope.get("path", "")
            raw_headers = scope.get("headers", [])
            headers = {
                k.decode("latin1").lower(): v.decode("latin1")
                for k, v in raw_headers
            }

            body_chunks: list[bytes] = []

            async def wrapped_receive() -> dict[str, Any]:
                message = await receive()
                if message.get("type") == "http.request":
                    body_chunks.append(message.get("body", b""))
                return message

            await self.app(scope, wrapped_receive, send)

            full_body = b"".join(body_chunks)
            json_data = None
            method = None
            if full_body:
                try:
                    parsed = json.loads(full_body.decode("utf-8"))
                    if isinstance(parsed, dict):
                        json_data = parsed
                        method = parsed.get("method")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            self.captured_requests.append(
                CapturedRequest(
                    path=path,
                    method=method,
                    headers=headers,
                    body=full_body,
                    json_data=json_data,
                )
            )
            return

        await self.app(scope, receive, send)


class SyntheticTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token == "synthetic-test-token":
            return AccessToken(
                token=token,
                client_id="synthetic-client",
                scopes=["read", "write"],
            )
        return None


async def list_tool_names(server: MCPServer) -> tuple[str, ...]:
    tools = await server.list_tools()
    return tuple(tool.name for tool in tools)


def extract_request_protocol_version(req: CapturedRequest) -> str | None:
    # Observed list/call wire field in both pinned client modes.
    return req.headers.get("mcp-protocol-version")


async def shutdown_probe_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    try:
        # wait leaves the owned task running on timeout/caller cancellation.
        done, _ = await asyncio.wait({task}, timeout=5.0)
        if not done:
            raise asyncio.TimeoutError
        task.result()
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        caller_cancelled = bool(asyncio.current_task().cancelling())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() and not caller_cancelled:
                raise
        except Exception as drain_error:
            if caller_cancelled or asyncio.current_task().cancelling():
                raise asyncio.CancelledError from drain_error
            raise
        if caller_cancelled or asyncio.current_task().cancelling():
            raise asyncio.CancelledError from exc
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError("Server shutdown timed out") from exc
        raise RuntimeError("Server task cancelled during shutdown") from exc


async def probe_protocols() -> tuple[SdkProbe, ...]:
    body_error: BaseException | None = None
    # Reserve an ephemeral socket once and hand that socket to Uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server_task: asyncio.Task[None] | None = None
        uv_server: uvicorn.Server | None = None
        probe_results: list[dict[str, Any]] = []
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]

        server = MCPServer(
            name="synthetic-server",
            auth=AuthSettings(
                issuer_url="http://127.0.0.1",
                resource_server_url="http://127.0.0.1",
                validate_token_resource=False,
            ),
            token_verifier=SyntheticTokenVerifier(),
        )

        @server.tool(name="echo", description="Synthetic echo tool")
        async def echo(message: str) -> str:
            return message

        app = server.streamable_http_app(streamable_http_path="/mcp")
        capturing_middleware = RequestCapturingMiddleware(app)

        config = uvicorn.Config(
            app=capturing_middleware,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
        uv_server = uvicorn.Server(config=config)
        server_task = asyncio.create_task(uv_server.serve(sockets=[sock]))

        # Bounded readiness check
        for _ in range(100):
            if uv_server.started:
                break
            if server_task.done():
                exc = server_task.exception() if not server_task.cancelled() else None
                raise RuntimeError(f"Server task exited prematurely: {exc}")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Server failed to start within timeout")

        url = f"http://127.0.0.1:{port}/mcp"

        # Verify real authentication: unauthenticated request is rejected with 401
        async with httpx2.AsyncClient() as unauth_client:
            resp = await unauth_client.post(
                url,
                json={"jsonrpc": "2.0", "id": "probe-unauth", "method": "tools/list"},
            )
            if resp.status_code != 401:
                raise AssertionError(f"Expected HTTP 401 for unauthenticated request, got {resp.status_code}")

        async with httpx2.AsyncClient(headers={"Authorization": "Bearer synthetic-wrong-token"}) as wrong_client:
            resp = await wrong_client.post(
                url, json={"jsonrpc": "2.0", "id": "probe-wrong", "method": "tools/list"},
            )
            if resp.status_code != 401:
                raise AssertionError(f"Expected HTTP 401 for wrong token, got {resp.status_code}")

        # Probe both modes: "auto" and "legacy"
        for mode in ("auto", "legacy"):
            req_start_idx = len(capturing_middleware.captured_requests)
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer synthetic-test-token"}) as http_client:
                transport = streamable_http_client(url, http_client=http_client)
                async with Client(transport, mode=mode) as client:
                    version = str(client.protocol_version)
                    tools_result = await client.list_tools()
                    tool_names = tuple(t.name for t in tools_result.tools)
                    message = f"synthetic-{mode}"
                    call_result = await client.call_tool("echo", {"message": message})
                    if call_result.is_error:
                        raise RuntimeError(f"Tool call failed in mode {mode}")
                    echoed = "".join(b.text for b in call_result.content if isinstance(b, TextContent))

            reqs_for_mode = capturing_middleware.captured_requests[req_start_idx:]
            methods = [r.method for r in reqs_for_mode if r.method]

            # Require actual captured /mcp tools/list and tools/call requests
            list_req = next((r for r in reqs_for_mode if r.method == "tools/list"), None)
            call_req = next((r for r in reqs_for_mode if r.method == "tools/call"), None)
            if list_req is None:
                raise AssertionError(f"Mode '{mode}' did not capture a 'tools/list' request")
            if call_req is None:
                raise AssertionError(f"Mode '{mode}' did not capture a 'tools/call' request")

            if list_req.path != "/mcp":
                raise AssertionError(f"Mode '{mode}' tools/list path was '{list_req.path}', expected '/mcp'")
            if call_req.path != "/mcp":
                raise AssertionError(f"Mode '{mode}' tools/call path was '{call_req.path}', expected '/mcp'")

            # Require that all captured MCP requests carried the synthetic bearer token
            for req in reqs_for_mode:
                if req.path == "/mcp":
                    auth = req.headers.get("authorization")
                    if auth != "Bearer synthetic-test-token":
                        raise AssertionError(f"Expected synthetic bearer token, got {auth}")

            call_params = call_req.json_data.get("params")
            if not isinstance(call_params, dict):
                raise AssertionError(f"Mode '{mode}' recorded arguments require object params, got {call_params!r}")
            arguments = call_params.get("arguments")
            if arguments != {"message": message}:
                raise AssertionError(f"Mode '{mode}' recorded arguments {arguments!r}, expected message {message!r}")
            if echoed != message:
                raise AssertionError(f"Mode '{mode}' echoed {echoed!r}, expected {message!r}")

            for req in (list_req, call_req):
                recorded_version = extract_request_protocol_version(req)
                if recorded_version != version:
                    raise AssertionError(f"Mode '{mode}' {req.method} version {recorded_version!r} did not match client version {version}")
                if mode == "auto":
                    metadata_version = req.json_data.get("params", {}).get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
                    if metadata_version != version:
                        raise AssertionError(f"Mode 'auto' {req.method} metadata version {metadata_version!r} did not match client version {version}")

            # Verify protocol specifics
            if mode == "auto":
                if "server/discover" not in methods:
                    raise AssertionError("Mode 'auto' must send 'server/discover'")
                if "initialize" in methods:
                    raise AssertionError("Mode 'auto' must not send 'initialize'")
                if version != "2026-07-28":
                    raise AssertionError(f"Expected version '2026-07-28' for 'auto', got {version}")

            elif mode == "legacy":
                if "initialize" not in methods:
                    raise AssertionError("Mode 'legacy' must send 'initialize'")
                if version != "2025-11-25":
                    raise AssertionError(f"Expected version '2025-11-25' for 'legacy', got {version}")

                init_req = next((r for r in reqs_for_mode if r.method == "initialize"), None)
                if init_req is None:
                    raise AssertionError("Mode 'legacy' missing initialize request")
                init_ver = init_req.json_data.get("params", {}).get("protocolVersion")
                if init_ver != version:
                    raise AssertionError(f"Initialize version {init_ver!r} did not match client version {version}")

            probe_results.append({
                "version": version,
                "tool_names": tool_names,
                "echoed": echoed,
                "path": list_req.path,
            })

    except BaseException as exc:
        body_error = exc
        raise
    finally:
        # Keep only this probe's body failure primary, with shutdown failure as a note.
        try:
            if uv_server is not None and server_task is not None:
                try:
                    await shutdown_probe_server(uv_server, server_task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if body_error is None:
                        raise
                    body_error.add_note(f"Server shutdown also failed: {exc!r}")
        finally:
            sock.close()

    # listener_closed must reflect a real closed listener after server cleanup:
    # attempt a connection to the owned loopback port and require refusal.
    listener_closed = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            listener_closed = False
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            listener_closed = True
        else:
            raise

    if not listener_closed:
        raise RuntimeError(f"Port 127.0.0.1:{port} remained open after server cleanup")

    # Construct final probes only after listener closure verification
    probes = tuple(
        SdkProbe(
            version=r["version"],
            tool_names=r["tool_names"],
            echoed=r["echoed"],
            listener_closed=listener_closed,
            path=r["path"],
        )
        for r in probe_results
    )
    return probes


@asynccontextmanager
async def running_service(app, sock):
    """Run and settle only the supplied ephemeral listener, including failure paths."""
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async def ready():
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError("Owned server exited before readiness")
                await asyncio.sleep(0)
        await asyncio.wait_for(ready(), 5)
        yield server
    finally:
        try:
            await shutdown_probe_server(server, task)
        finally:
            sock.close()


class TestService(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = make_service(Path(directory.name))

    def command_input(self, kind="create_plan", payload=None, **fields):
        if payload is None:
            payload = {"creation_id": "sdk-create", "draft": draft(), "new_plan_intent": None}
        return plans.to_jsonable(self.f.command(kind, payload, **fields))

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    @staticmethod
    def result_json(result):
        return json.dumps(result.model_dump(mode="json", by_alias=True))

    def test_tools_use_service_and_private_mutation_result(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        async def exercise():
            created = await server.call_tool("create_plan", {"command": self.command_input()})
            self.assertFalse(created.is_error, created.content)
            receipt = created.structured_content
            metadata = receipt["committed_metadata"]
            plan_id = metadata["plan_id"]
            token = receipt["private"]["orchestrator_token"]
            self.assertTrue(token)
            self.assertEqual(metadata["epoch"], self.f.epoch)
            self.assertEqual(metadata["revision"], 1)
            raw = self.command_input()
            raw.update(kind="revise_plan", key="sdk-revise", plan_id=plan_id,
                       generation=metadata["generation"], expected_revision=1,
                       ownership_version=1, orchestrator_token=token,
                       payload=plans.to_jsonable({"draft": replace(self.f.service.read_plan(plan_id).draft, next_check="Revised SDK next check"),
                                                "reason": "SDK revision", "authorization_change": None}))
            revised = await server.call_tool("revise_plan", {"command": raw})
            self.assertFalse(revised.is_error, revised.content)
            self.assertEqual(revised.structured_content["current_metadata"]["revision"], 2)
            for name, arguments in (
                ("read_service_info", {}), ("list_plans", {}), ("read_plan", {"plan_id": plan_id}),
                ("read_events", {"plan_id": plan_id}), ("list_ready", {"plan_id": plan_id}),
                ("read_coordination", {"plan_id": plan_id}),
            ):
                result = await server.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.content)
                encoded = self.result_json(result)
                self.assertNotIn(token, encoded)
                self.assertNotIn("orchestrator_token", encoded)
                self.assertNotIn("attempt_token", encoded)
                self.assertNotIn('"private"', encoded)
            public = (await server.call_tool("read_plan", {"plan_id": plan_id})).structured_content
            self.assertEqual(public["metadata"], revised.structured_content["current_metadata"])
            self.assertEqual(public["draft"]["next_check"], "Revised SDK next check")
            takeover = copy.deepcopy(raw)
            takeover.update(kind="takeover", key="sdk-takeover", expected_revision=2,
                            orchestrator_token=None, payload={"expected_ownership_version": 1, "reason": "New owner"})
            taken = await server.call_tool("takeover", {"command": takeover})
            self.assertFalse(taken.is_error, taken.content)
            self.assertEqual(taken.structured_content["current_metadata"]["ownership_version"], 2)
            self.assertNotEqual(taken.structured_content["private"]["orchestrator_token"], token)
        asyncio.run(exercise())

    def test_no_assignment_tool_in_planning_milestone(self):
        from adaptive_planning.transport import build_mcp
        names = asyncio.run(list_tool_names(build_mcp(self.f.service)))
        self.assertEqual(set(names), {"read_service_info", "list_plans", "read_plan", "read_events",
                                      "list_ready", "read_coordination", "create_plan", "revise_plan", "takeover"})
        self.assertIn("create_plan", names)
        self.assertNotIn("assign_step", names)

    def invalid_commands(self):
        base = self.command_input()
        variants = []
        for edit in (
            {"kind": SYNTHETIC_TOKEN}, {"expected_revision": SYNTHETIC_TOKEN},
            {"orchestrator_token": {SYNTHETIC_TOKEN: SYNTHETIC_TOKEN}},
            {SYNTHETIC_TOKEN: "unknown"},
            {"payload": {**base["payload"], SYNTHETIC_TOKEN: SYNTHETIC_TOKEN}},
            {"payload": {**base["payload"], "draft": {**base["payload"]["draft"], SYNTHETIC_TOKEN: "unknown"}}},
            {"payload": {**base["payload"], "draft": {**base["payload"]["draft"], "objective": [SYNTHETIC_TOKEN]}}},
        ):
            variants.append({"command": {**copy.deepcopy(base), **edit}})
        variants.extend(({"command": SYNTHETIC_TOKEN}, {"command": base, SYNTHETIC_TOKEN: "unknown"}))
        variants.append({"command": {**copy.deepcopy(base), "kind": "takeover",
                                      "payload": {"expected_ownership_version": 1, "reason": SYNTHETIC_TOKEN}}})
        return variants

    def test_invalid_arguments_and_sdk_failures_are_secret_free_and_atomic(self):
        from adaptive_planning.transport import build_mcp
        captured = CapturedLogs()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(captured)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(root.removeHandler, captured)
        server = build_mcp(self.f.service)
        before = self.snapshot()
        async def exercise():
            for arguments in self.invalid_commands():
                result = await server.call_tool("create_plan", arguments)
                self.assertTrue(result.is_error)
                text = self.result_json(result)
                self.assertNotIn(SYNTHETIC_TOKEN, text)
                self.assertNotIn(SYNTHETIC_TOKEN[:24], text)
                self.assertEqual(self.snapshot(), before)
            result = await server.call_tool(SYNTHETIC_TOKEN, {})
            self.assertTrue(result.is_error)
            self.assertNotIn(SYNTHETIC_TOKEN, self.result_json(result))
            with patch.object(self.f.service, "execute", side_effect=RuntimeError(SYNTHETIC_TOKEN)):
                failed = await server.call_tool("create_plan", {"command": self.command_input()})
            self.assertTrue(failed.is_error)
            self.assertEqual(failed.structured_content["error"]["code"], "internal_error")
            self.assertNotIn(SYNTHETIC_TOKEN, self.result_json(failed))
            self.assertEqual(self.snapshot(), before)
        asyncio.run(exercise())
        for line in captured.lines:
            self.assertNotIn(SYNTHETIC_TOKEN, line)
            self.assertNotIn(SYNTHETIC_TOKEN[:24], line)

    def test_list_plans_aggregate_is_bounded_without_truncation(self):
        from adaptive_planning.transport import build_mcp
        for number in range(2):
            self.f.service.execute(self.f.command("create_plan", {
                "creation_id": f"aggregate-{number}", "draft": replace(draft(), objective=f"Plan {number}"),
                "new_plan_intent": "Separate bounded aggregate plan" if number else None}))
        server = build_mcp(self.f.service)
        full = asyncio.run(server.call_tool("list_plans", {}))
        self.assertFalse(full.is_error)
        self.assertEqual(len(full.structured_content["plans"]), 2)
        size = len("".join(item.text for item in full.content if isinstance(item, TextContent)).encode())
        individual = max(len(plans.canonical_bytes(p)) for p in self.f.service.list_plans())
        self.assertGreater(size, individual)
        with patch.object(plans, "MAX_PAGE_BYTES", size):
            at_limit = asyncio.run(server.call_tool("list_plans", {}))
            self.assertFalse(at_limit.is_error)
            self.assertEqual(len(at_limit.structured_content["plans"]), 2)
        with patch.object(plans, "MAX_PAGE_BYTES", size - 1):
            rejected = asyncio.run(server.call_tool("list_plans", {}))
            self.assertTrue(rejected.is_error)
            self.assertEqual(rejected.structured_content["error"]["code"], "limit_exceeded")

    def test_duplicate_plan_error_keeps_public_code_and_does_not_mutate(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        created = asyncio.run(server.call_tool("create_plan", {"command": self.command_input()}))
        self.assertFalse(created.is_error)
        before = self.snapshot()
        duplicate = self.command_input(payload={"creation_id": SYNTHETIC_TOKEN, "draft": draft(), "new_plan_intent": None})
        result = asyncio.run(server.call_tool("create_plan", {"command": duplicate}))
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "duplicate_candidate")
        self.assertNotIn(SYNTHETIC_TOKEN, self.result_json(result))
        self.assertEqual(self.snapshot(), before)

    def test_schema_error_diagnostics_stay_bounded_and_secret_free(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        command = self.command_input()
        command.update({f"{SYNTHETIC_TOKEN}-{number}": SYNTHETIC_TOKEN for number in range(200)})
        before = self.snapshot()
        with patch.object(plans, "MAX_PAGE_BYTES", 4096):
            result = asyncio.run(server.call_tool("create_plan", {"command": command}))
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "invalid")
        self.assertEqual(result.structured_content["error"]["validation_error_count"], 200)
        text = "".join(block.text for block in result.content if isinstance(block, TextContent))
        self.assertLessEqual(len(text.encode()), 4096)
        self.assertNotIn(SYNTHETIC_TOKEN[:24], text)
        self.assertEqual(self.snapshot(), before)

    def test_shared_app_two_protocols_wire_errors_and_lifecycle(self):
        from adaptive_planning.app import build_app
        from adaptive_planning.config import Config
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        self.assertNotEqual(port, 8790)
        config = Config(self.f.directory, base_url=f"http://127.0.0.1:{port}")
        token_path = config.data_dir / "service-token"
        token_path.write_text(SYNTHETIC_TOKEN)
        token_path.chmod(0o600)
        app = build_app(config)
        captured = CapturedLogs()
        root = logging.getLogger()
        root.addHandler(captured)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(root.removeHandler, captured)
        private_tokens = []
        async def exercise():
            async with running_service(app, sock):
                # Preserve actual wire observations while using the production stack.
                app.middleware_stack = RequestCapturingMiddleware(app.middleware_stack)
                for mode, version in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
                    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"},
                                                 follow_redirects=False) as http_client:
                        async with Client(streamable_http_client(config.base_url + "/mcp", http_client=http_client), mode=mode) as client:
                            self.assertEqual(str(client.protocol_version), version)
                            names = {tool.name for tool in (await client.list_tools()).tools}
                            self.assertIn("create_plan", names)
                            self.assertNotIn("assign_step", names)
                            info = await client.call_tool("read_service_info", {})
                            self.assertFalse(info.is_error)
                            self.assertEqual(info.structured_content["dashboard_url"], config.base_url + "/")
                            creation = self.command_input(payload={"creation_id": f"wire-{mode}", "draft": draft(),
                                                                 "new_plan_intent": "Separate protocol fixture" if mode == "legacy" else None})
                            created = await client.call_tool("create_plan", {"command": creation})
                            self.assertFalse(created.is_error, created.content)
                            receipt = created.structured_content
                            metadata = receipt["committed_metadata"]
                            plan_id = metadata["plan_id"]
                            grant = receipt["private"]["orchestrator_token"]
                            private_tokens.append(grant)
                            revision = {**creation, "kind": "revise_plan", "key": f"wire-revise-{mode}",
                                        "plan_id": plan_id, "generation": metadata["generation"], "expected_revision": 1,
                                        "ownership_version": 1, "orchestrator_token": grant,
                                        "payload": plans.to_jsonable({"draft": replace(self.f.service.read_plan(plan_id).draft,
                                                                                      next_check="Wire next check"),
                                                                     "reason": "Protocol revision", "authorization_change": None})}
                            revised = await client.call_tool("revise_plan", {"command": revision})
                            self.assertFalse(revised.is_error, revised.content)
                            self.assertEqual(revised.structured_content["current_metadata"]["revision"], 2)
                            for name, arguments in (("list_plans", {}), ("read_plan", {"plan_id": plan_id}),
                                                    ("read_events", {"plan_id": plan_id}), ("list_ready", {"plan_id": plan_id}),
                                                    ("read_coordination", {"plan_id": plan_id})):
                                public = await client.call_tool(name, arguments)
                                self.assertFalse(public.is_error, public.content)
                                self.assertNotIn(grant, self.result_json(public))
                                self.assertNotIn("orchestrator_token", self.result_json(public))
                            takeover = {**revision, "kind": "takeover", "key": f"wire-takeover-{mode}",
                                        "expected_revision": 2, "orchestrator_token": None,
                                        "payload": {"expected_ownership_version": 1, "reason": "Wire takeover"}}
                            taken = await client.call_tool("takeover", {"command": takeover})
                            self.assertFalse(taken.is_error, taken.content)
                            self.assertEqual(taken.structured_content["current_metadata"]["ownership_version"], 2)
                            private_tokens.append(taken.structured_content["private"]["orchestrator_token"])
                            before = self.snapshot()
                            for arguments in self.invalid_commands():
                                result = await client.call_tool("create_plan", arguments)
                                self.assertTrue(result.is_error)
                                self.assertNotIn(SYNTHETIC_TOKEN[:24], self.result_json(result))
                                self.assertEqual(self.snapshot(), before)
                            unknown = await client.call_tool(SYNTHETIC_TOKEN, {})
                            self.assertTrue(unknown.is_error)
                            self.assertNotIn(SYNTHETIC_TOKEN, self.result_json(unknown))
                    # Force the actual SDK malformed-envelope response beyond a tiny
                    # diagnostic budget, then prove a healthy subsequent request still works.
                    from adaptive_planning.transport import MCPHttpErrors
                    projection = next(route.app for route in app.routes if isinstance(getattr(route, "app", None), MCPHttpErrors))
                    with patch.object(projection, "max_error_body_bytes", 1):
                        async with httpx2.AsyncClient() as http_client:
                            response = await http_client.post(config.base_url + "/mcp",
                                headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}",
                                         "Accept": "application/json, text/event-stream"},
                                json={"jsonrpc": SYNTHETIC_TOKEN, "id": 1, "method": "tools/list"})
                            self.assertEqual(response.status_code, 500)
                            self.assertNotIn(SYNTHETIC_TOKEN[:24], response.text)
                            self.assertTrue(response.headers["content-type"].startswith("application/json"))
                            self.assertEqual(response.json()["error"]["code"], "internal_error")
                            self.assertEqual(response.headers["content-length"], str(len(response.content)))
                    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"}) as http_client:
                        async with Client(streamable_http_client(config.base_url + "/mcp", http_client=http_client), mode=mode) as client:
                            healthy = await client.call_tool("read_service_info", {})
                            self.assertFalse(healthy.is_error)
                    self.assertEqual(self.snapshot(), before)
                    requests = app.middleware_stack.captured_requests
                    selected = [r for r in requests if r.method in ("tools/list", "tools/call")
                                and r.headers.get("mcp-protocol-version") == version]
                    self.assertTrue(selected)
                    self.assertEqual({r.path for r in selected}, {"/mcp"})
                    self.assertTrue(all(r.headers.get("authorization") == f"Bearer {SYNTHETIC_TOKEN}" for r in selected))
                self.assertEqual(len(self.f.service.list_plans()), 2)
        asyncio.run(exercise())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)
        for line in captured.lines:
            # The test's SDK client has its own DEBUG wire logger. The service owns
            # server diagnostics; client-side logging is outside this server contract.
            if line.startswith("mcp.client."):
                continue
            self.assertNotIn(SYNTHETIC_TOKEN, line)
            self.assertNotIn(SYNTHETIC_TOKEN[:24], line)
            for grant in private_tokens:
                self.assertNotIn(grant, line)
        self.assertTrue(any(line.startswith("adaptive_planning.transport:") for line in captured.lines))

    def test_list_tool_names(self):
        server = MCPServer(name="synthetic-server")

        @server.tool(name="echo", description="Synthetic echo tool")
        async def echo(message: str) -> str:
            return message

        names = asyncio.run(list_tool_names(server))
        self.assertEqual(names, ("echo",))

    def test_streamable_http_two_protocols(self):
        probes = asyncio.run(probe_protocols())
        self.assertEqual({p.version for p in probes}, {"2026-07-28", "2025-11-25"})
        self.assertEqual({p.echoed for p in probes}, {"synthetic-auto", "synthetic-legacy"})
        self.assertTrue(all("echo" in p.tool_names and p.listener_closed for p in probes))
        self.assertEqual({p.path for p in probes}, {"/mcp"})


class TestSdkProbeFailures(unittest.IsolatedAsyncioTestCase):
    async def test_socket_setup_failures_close_owned_socket(self):
        for operation in ("setsockopt", "bind", "listen", "getsockname"):
            with self.subTest(operation=operation):
                owned = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                proxy = Mock(wraps=owned)
                getattr(proxy, operation).side_effect = OSError("synthetic setup failure")
                try:
                    with patch(__name__ + ".socket.socket", return_value=proxy):
                        with self.assertRaisesRegex(OSError, "synthetic setup failure"):
                            await probe_protocols()
                    self.assertEqual(owned.fileno(), -1, "setup failure leaked the owned socket")
                finally:
                    owned.close()

    async def test_legacy_list_and_call_require_recorded_protocol_header(self):
        original = RequestCapturingMiddleware.__call__
        for method in ("tools/list", "tools/call"):
            with self.subTest(method=method):
                async def omit_header(middleware, scope, receive, send):
                    await original(middleware, scope, receive, send)
                    if scope["type"] == "http":
                        for request in middleware.captured_requests:
                            if request.method == method and request.headers.get("mcp-protocol-version") == "2025-11-25":
                                request.headers.pop("mcp-protocol-version")
                with patch.object(RequestCapturingMiddleware, "__call__", omit_header):
                    with self.assertRaisesRegex(AssertionError, "legacy.*version"):
                        await probe_protocols()

    async def test_call_requires_recorded_arguments(self):
        original = RequestCapturingMiddleware.__call__
        async def omit_arguments(middleware, scope, receive, send):
            await original(middleware, scope, receive, send)
            for request in middleware.captured_requests:
                if request.method == "tools/call":
                    request.json_data["params"].pop("arguments", None)
        with patch.object(RequestCapturingMiddleware, "__call__", omit_arguments):
            with self.assertRaisesRegex(AssertionError, "arguments"):
                await probe_protocols()

    async def test_echo_rejects_arguments_dropped_before_transport(self):
        original = Client.call_tool
        async def drop_arguments(client, name, arguments):
            return await original(client, name, {})
        with patch.object(Client, "call_tool", drop_arguments):
            with self.assertRaises(Exception) as raised:
                await probe_protocols()
        pending = [raised.exception]
        failures = []
        while pending:
            exc = pending.pop()
            if isinstance(exc, BaseExceptionGroup):
                pending.extend(exc.exceptions)
            else:
                failures.append(exc)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertIn("Tool call failed", str(failures[0]))

    async def test_body_failure_survives_shutdown_failure(self):
        body_error = ValueError("synthetic body failure")
        shutdown_error = RuntimeError("synthetic shutdown failure")
        class FailingShutdownServer:
            started = True
            should_exit = False
            def __init__(self, config):
                pass
            async def serve(self, sockets):
                while not self.should_exit:
                    await asyncio.sleep(0)
                raise shutdown_error
        with patch.object(uvicorn, "Server", FailingShutdownServer), patch.object(
            httpx2.AsyncClient, "post", AsyncMock(side_effect=body_error)
        ):
            observed = None
            try:
                await probe_protocols()
            except BaseException as exc:
                observed = exc
        self.assertIs(observed, body_error)
        self.assertTrue(any("synthetic shutdown failure" in note for note in observed.__notes__))

    async def test_server_cancellation_during_cleanup_preserves_body_failure(self):
        body_error = ValueError("synthetic body failure")
        class CancelledServer:
            started = True
            should_exit = False
            def __init__(self, config):
                pass
            async def serve(self, sockets):
                while not self.should_exit:
                    await asyncio.sleep(0)
                raise asyncio.CancelledError
        with patch.object(uvicorn, "Server", CancelledServer), patch.object(
            httpx2.AsyncClient, "post", AsyncMock(side_effect=body_error)
        ):
            observed = None
            try:
                await probe_protocols()
            except BaseException as exc:
                observed = exc
        self.assertIs(observed, body_error)
        self.assertTrue(any("cancelled" in note for note in observed.__notes__))

    async def test_caller_cancellation_during_cleanup_is_propagated(self):
        shutdown_started = asyncio.Event()
        server_stopped = asyncio.Event()
        body_error = ValueError("synthetic body failure")
        class WaitingServer:
            started = True
            should_exit = False
            def __init__(self, config):
                pass
            async def serve(self, sockets):
                try:
                    while not self.should_exit:
                        await asyncio.sleep(0)
                    shutdown_started.set()
                    await asyncio.Event().wait()
                finally:
                    server_stopped.set()
        with patch.object(uvicorn, "Server", WaitingServer), patch.object(
            httpx2.AsyncClient, "post", AsyncMock(side_effect=body_error)
        ):
            probe = asyncio.create_task(probe_protocols())
            await asyncio.wait_for(shutdown_started.wait(), timeout=1)
            probe.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await probe
        self.assertTrue(server_stopped.is_set(), "caller cancellation left the server task running")

    async def test_shutdown_timeout_cancels_and_settles_server_task(self):
        stopped = asyncio.Event()
        async def serve():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(serve())
        server = Mock()
        original_wait_for = asyncio.wait_for
        original_wait = asyncio.wait
        async def short_wait(tasks, *, timeout):
            return await original_wait(tasks, timeout=0.01)
        with patch.object(asyncio, "wait", short_wait):
            with self.assertRaisesRegex(RuntimeError, "shutdown timed out"):
                await shutdown_probe_server(server, task)
        self.assertTrue(task.done())
        self.assertTrue(stopped.is_set())
        self.assertTrue(server.should_exit)

    async def test_caller_cancelled_during_timeout_drain_is_propagated(self):
        draining = asyncio.Event()
        stopped = asyncio.Event()
        async def serve():
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    draining.set()
                    await asyncio.Event().wait()
            finally:
                stopped.set()
        server_task = asyncio.create_task(serve())
        original_wait_for = asyncio.wait_for
        original_wait = asyncio.wait
        async def short_wait(tasks, *, timeout):
            return await original_wait(tasks, timeout=0.01)
        with patch.object(asyncio, "wait", short_wait):
            cleanup = asyncio.create_task(shutdown_probe_server(Mock(), server_task))
            await original_wait_for(draining.wait(), timeout=1)
            cleanup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup
        self.assertTrue(server_task.done())
        self.assertTrue(stopped.is_set())

    async def test_server_cancellation_without_caller_is_shutdown_failure(self):
        async def cancelled_server():
            raise asyncio.CancelledError
        server_task = asyncio.create_task(cancelled_server())
        with self.assertRaisesRegex(RuntimeError, "Server task cancelled during shutdown"):
            await shutdown_probe_server(Mock(), server_task)
        self.assertEqual(asyncio.current_task().cancelling(), 0)
        self.assertTrue(server_task.done())

    async def test_successful_body_inside_except_still_reports_shutdown_failure(self):
        original_shutdown = shutdown_probe_server
        settled = []
        async def fail_after_actual_shutdown(server, task):
            await original_shutdown(server, task)
            settled.append(task.done())
            raise RuntimeError("synthetic post-shutdown failure")
        unrelated = ValueError("synthetic unrelated caller error")
        try:
            raise unrelated
        except ValueError:
            with patch(__name__ + ".shutdown_probe_server", fail_after_actual_shutdown):
                with self.assertRaisesRegex(RuntimeError, "synthetic post-shutdown failure"):
                    await probe_protocols()
        self.assertEqual(settled, [True], "actual server did not settle before injected shutdown failure")
        self.assertFalse(hasattr(unrelated, "__notes__"), "unrelated caller exception was mutated")

    async def test_caller_cancellation_survives_server_error_during_drain(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def serve():
            try:
                started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("synthetic server error on cancellation")
            finally:
                stopped.set()
        server_task = asyncio.create_task(serve())
        await started.wait()
        cleanup = asyncio.create_task(shutdown_probe_server(Mock(), server_task))
        await asyncio.sleep(0)
        cleanup.cancel()
        observed = None
        try:
            await cleanup
        except BaseException as exc:
            observed = exc
        self.assertIsInstance(observed, asyncio.CancelledError)
        self.assertTrue(server_task.done())
        self.assertTrue(stopped.is_set())

    async def test_cancellation_arriving_during_timeout_drain_survives_server_error(self):
        draining = asyncio.Event()
        stopped = asyncio.Event()
        async def serve():
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    draining.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        raise RuntimeError("synthetic server error during timeout drain")
            finally:
                stopped.set()
        server_task = asyncio.create_task(serve())
        original_wait_for = asyncio.wait_for
        original_wait = asyncio.wait
        async def short_wait(tasks, *, timeout):
            return await original_wait(tasks, timeout=0.01)
        with patch.object(asyncio, "wait", short_wait):
            cleanup = asyncio.create_task(shutdown_probe_server(Mock(), server_task))
            await original_wait_for(draining.wait(), timeout=1)
            cleanup.cancel()
            observed = None
            try:
                await cleanup
            except BaseException as exc:
                observed = exc
        self.assertIsInstance(observed, asyncio.CancelledError)
        self.assertTrue(server_task.done())
        self.assertTrue(stopped.is_set())

    async def test_caller_cancellation_retrieves_server_failure_without_loop_diagnostic(self):
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        diagnostics = []
        started = asyncio.Event()
        async def serve():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("synthetic retrieved server failure")
        loop.set_exception_handler(lambda _, context: diagnostics.append(context))
        try:
            server_task = asyncio.create_task(serve())
            await started.wait()
            cleanup = asyncio.create_task(shutdown_probe_server(Mock(), server_task))
            await asyncio.sleep(0)
            cleanup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup
            self.assertTrue(server_task.done())
            self.assertEqual(str(server_task.exception()), "synthetic retrieved server failure")
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
        self.assertEqual(
            [context.get("message") for context in diagnostics], [],
            "owned cleanup leaked a diagnostic despite retrieving the server failure",
        )


if __name__ == "__main__":
    unittest.main()
