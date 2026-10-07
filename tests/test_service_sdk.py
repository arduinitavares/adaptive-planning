"""Test suite proving the pinned MCP SDK and client transport contract."""

import asyncio
from dataclasses import dataclass
import errno
import json
import socket
from typing import Any
import unittest

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


class TestTokenVerifier:
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
    for h in ("mcp-protocol-version", "protocol-version"):
        if h in req.headers:
            return req.headers[h]
    if req.json_data and "params" in req.json_data:
        params = req.json_data["params"]
        if isinstance(params, dict):
            if "protocolVersion" in params:
                return str(params["protocolVersion"])
            if "_meta" in params and isinstance(params["_meta"], dict):
                if "protocolVersion" in params["_meta"]:
                    return str(params["_meta"]["protocolVersion"])
    if req.json_data and "_meta" in req.json_data and isinstance(req.json_data["_meta"], dict):
        if "protocolVersion" in req.json_data["_meta"]:
            return str(req.json_data["_meta"]["protocolVersion"])
    return None


async def probe_protocols() -> tuple[SdkProbe, ...]:
    # Reserve an ephemeral socket once and hand that socket to Uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    server_task: asyncio.Task[None] | None = None
    uv_server: uvicorn.Server | None = None
    probe_results: list[dict[str, Any]] = []

    try:
        server = MCPServer(
            name="synthetic-server",
            auth=AuthSettings(
                issuer_url="http://127.0.0.1",
                resource_server_url="http://127.0.0.1",
                validate_token_resource=False,
            ),
            token_verifier=TestTokenVerifier(),
        )

        @server.tool(name="echo", description="Synthetic echo tool")
        async def echo(message: str = "synthetic") -> str:
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
        uv_server.install_signal_handlers = lambda: None
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

        # Probe both modes: "auto" and "legacy"
        for mode in ("auto", "legacy"):
            req_start_idx = len(capturing_middleware.captured_requests)
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer synthetic-test-token"}) as http_client:
                transport = streamable_http_client(url, http_client=http_client)
                async with Client(transport, mode=mode) as client:
                    version = str(client.protocol_version)
                    tools_result = await client.list_tools()
                    tool_names = tuple(t.name for t in tools_result.tools)
                    call_result = await client.call_tool("echo", {"message": "synthetic"})
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

            # Verify protocol specifics
            if mode == "auto":
                if "server/discover" not in methods:
                    raise AssertionError("Mode 'auto' must send 'server/discover'")
                if "initialize" in methods:
                    raise AssertionError("Mode 'auto' must not send 'initialize'")
                if version != "2026-07-28":
                    raise AssertionError(f"Expected version '2026-07-28' for 'auto', got {version}")

                list_ver = extract_request_protocol_version(list_req)
                if list_ver != version:
                    raise AssertionError(f"Mode 'auto' tools/list version {list_ver!r} did not match client version {version}")

                call_ver = extract_request_protocol_version(call_req)
                if call_ver != version:
                    raise AssertionError(f"Mode 'auto' tools/call version {call_ver!r} did not match client version {version}")

            elif mode == "legacy":
                if "initialize" not in methods:
                    raise AssertionError("Mode 'legacy' must send 'initialize'")
                if version != "2025-11-25":
                    raise AssertionError(f"Expected version '2025-11-25' for 'legacy', got {version}")

                init_req = next((r for r in reqs_for_mode if r.method == "initialize"), None)
                if init_req is None:
                    raise AssertionError("Mode 'legacy' missing initialize request")
                init_ver = extract_request_protocol_version(init_req)
                if init_ver != version:
                    raise AssertionError(f"Initialize version {init_ver!r} did not match client version {version}")

            probe_results.append({
                "version": version,
                "tool_names": tool_names,
                "echoed": echoed,
                "path": list_req.path,
            })

    finally:
        # Always shut the server down and await its task
        if uv_server is not None:
            uv_server.should_exit = True
        if server_task is not None:
            shutdown_error: BaseException | None = None
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except asyncio.TimeoutError as exc:
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
                shutdown_error = RuntimeError(f"Server shutdown timed out: {exc}")
            except asyncio.CancelledError:
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as exc:
                shutdown_error = exc
            finally:
                sock.close()
            if shutdown_error is not None:
                raise shutdown_error
        else:
            sock.close()

    # listener_closed must reflect a real closed listener after server cleanup:
    # attempt a connection to the owned loopback port and require refusal.
    listener_closed = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            listener_closed = False
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED or exc.errno in (61, 111):
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
        async def echo(message: str = "synthetic") -> str:
            return message

        names = asyncio.run(list_tool_names(server))
        self.assertEqual(names, ("echo",))

    def test_streamable_http_two_protocols(self):
        probes = asyncio.run(probe_protocols())
        self.assertEqual({p.version for p in probes}, {"2026-07-28", "2025-11-25"})
        self.assertTrue(all("echo" in p.tool_names and p.echoed == "synthetic" and p.listener_closed for p in probes))
        self.assertEqual({p.path for p in probes}, {"/mcp"})


if __name__ == "__main__":
    unittest.main()
