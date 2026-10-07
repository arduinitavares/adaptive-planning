"""Test suite proving the pinned MCP SDK and client transport contract."""

import asyncio
from dataclasses import dataclass
import errno
import json
import socket
import sys
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
        # Shield keeps timeout/caller cancellation distinct from server cancellation.
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        caller_cancelled = bool(asyncio.current_task().cancelling())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() and not caller_cancelled:
                raise
        if caller_cancelled:
            raise asyncio.CancelledError from exc
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError("Server shutdown timed out") from exc
        raise RuntimeError("Server task cancelled during shutdown") from exc


async def probe_protocols() -> tuple[SdkProbe, ...]:
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

            arguments = call_req.json_data.get("params", {}).get("arguments")
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

    finally:
        # Keep the body failure primary, with any shutdown failure attached as a note.
        body_error = sys.exception()
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


class TestServiceSdk(unittest.TestCase):
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
        async def short_wait(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=0.01)
        with patch.object(asyncio, "wait_for", short_wait):
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
        async def short_wait(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=0.01)
        with patch.object(asyncio, "wait_for", short_wait):
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


if __name__ == "__main__":
    unittest.main()
