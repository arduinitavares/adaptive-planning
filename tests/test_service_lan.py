"""Optional LAN identity: strict opt-in, shared authentication and real SDK transport."""
from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from adaptive_planning.app import build_app
from adaptive_planning.config import Config, RequestBoundary, http_allowlist, load_config
from adaptive_planning.types import DomainError, parse_command
from tests.service_fixtures import draft
from tests.test_service_http import (
    OTHER_TOKEN, SYNTHETIC_TOKEN, Recorder, bound_ephemeral_socket, call, error_code, make_scope,
)
from tests.test_service_sdk import RequestCapturingMiddleware, running_service


class TestLan(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.local_host = "127.0.0.1:8790"
        self.local_origin = "http://127.0.0.1:8790"
        # Synthetic RFC1918 fixture; only owned loopback sockets are contacted.
        self.lan_host = "192.168.1.10:8790"
        self.lan_origin = "http://192.168.1.10:8790"
        self.bearer = (b"authorization", f"Bearer {SYNTHETIC_TOKEN}".encode())

    def config(self, lan=None, local="http://127.0.0.1:8790"):
        environment = {
            "ADAPTIVE_PLANNING_DATA_DIR": str(self.data_dir),
            "ADAPTIVE_PLANNING_BASE_URL": local,
        }
        if lan is not None:
            environment["ADAPTIVE_PLANNING_LAN_BASE_URL"] = lan
        return load_config(environment)

    def boundary(self, config):
        downstream = Recorder()
        return RequestBoundary(downstream, config, SYNTHETIC_TOKEN), downstream

    def test_default_denies_lan_and_keeps_positional_config_and_local_allowlist(self):
        config = Config(self.data_dir, self.local_origin, 23)
        self.assertEqual(config.stale_seconds, 23)
        self.assertEqual(http_allowlist(config), (self.local_host, self.local_origin))
        app, downstream = self.boundary(self.config())
        for path in ("/", "/mcp"):
            response = call(app, "GET", path, [(b"host", self.lan_host.encode()), self.bearer])
            self.assertEqual((response[0], error_code(response[2])), (403, "invalid_host"))
        self.assertEqual(downstream.calls, [])
        self.assertEqual(call(app, "GET", "/", [(b"host", self.local_host.encode())])[0], 200)

    def test_enabled_exact_local_and_lan_pairs_accept_reads_and_authenticated_mcp(self):
        config = self.config(self.lan_origin)
        self.assertEqual(http_allowlist(config), (self.local_host, self.local_origin))
        app, _ = self.boundary(config)
        for host, origin in ((self.local_host, self.local_origin), (self.lan_host, self.lan_origin)):
            for supplied_origin in (None, origin):
                headers = [(b"host", host.encode())]
                if supplied_origin is not None:
                    headers.append((b"origin", supplied_origin.encode()))
                for method, path, auth in (("GET", "/", False), ("HEAD", "/", False),
                                           ("GET", "/mcp", True), ("POST", "/mcp", True),
                                           ("PATCH", "/api/plans", True)):
                    with self.subTest(host=host, origin=supplied_origin, method=method, path=path):
                        status, response_headers, _ = call(app, method, path, headers + ([self.bearer] if auth else []))
                        self.assertEqual(status, 200)
                        self.assertFalse([name for name, _ in response_headers if name.startswith("access-control-")])

    def test_rfc1918_range_boundaries_and_valid_port_boundaries(self):
        for address in ("10.0.0.0", "10.255.255.255", "172.16.0.0", "172.31.255.255",
                        "192.168.0.0", "192.168.255.255"):
            for port in (1, 8790, 65535):
                with self.subTest(address=address, port=port):
                    origin = f"http://{address}:{port}"
                    app, _ = self.boundary(self.config(origin, f"http://127.0.0.1:{port}"))
                    response = call(app, "GET", "/", [(b"host", f"{address}:{port}".encode()),
                                                      (b"origin", origin.encode())])
                    self.assertEqual(response[0], 200)

    def test_invalid_lan_opt_in_is_rejected_without_echoing_input(self):
        invalid = (
            "", "http://192.168.1.10", "https://192.168.1.10:8790",
            "HTTP://192.168.1.10:8790", "http://192.168.1.10:8790/",
            "http://192.168.1.10:8790/path", "http://192.168.1.10:8790?token=SECRET",
            "http://192.168.1.10:8790#SECRET", "http://user:SECRET@192.168.1.10:8790",
            " http://192.168.1.10:8790", "http://192.168.1.10:8790 ",
            "http://192.168.1.10:8790\n", "http://192.168.1.10:\t8790",
            "http://192.168.001.10:8790", "http://192.168.1:8790",
            "http://3232235786:8790", "http://0xc0a8010a:8790", "http://192.168.1.256:8790",
            "http://localhost:8790", "http://studio.local:8790", "http://*.local:8790",
            "http://0.0.0.0:8790", "http://127.0.0.1:8790", "http://127.1:8790",
            "http://8.8.8.8:8790", "http://9.255.255.255:8790", "http://11.0.0.0:8790",
            "http://172.15.255.255:8790", "http://172.32.0.0:8790",
            "http://192.167.255.255:8790", "http://192.169.0.0:8790",
            "http://169.254.1.1:8790", "http://100.64.0.1:8790", "http://192.0.0.1:8790",
            "http://198.18.0.1:8790", "http://224.0.0.1:8790", "http://255.255.255.255:8790",
            "http://[::1]:8790", "http://[fd00::1]:8790", "http://[::ffff:192.168.1.10]:8790",
            "http://192.168.1.10:0", "http://192.168.1.10:65536",
            "http://192.168.1.10:08790", "http://192.168.1.10:+8790",
            "http://192.168.1.10:-8790", "http://192.168.1.10:*",
            "http://192.168.1.10:8790:8790", "http://192.168.1.10:8791",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(DomainError) as raised:
                    self.config(url)
                self.assertEqual(raised.exception.code, "invalid_config")
                self.assertNotIn("SECRET", str(raised.exception))
                if url:
                    self.assertNotIn(url, str(raised.exception))

    def test_lan_opt_in_does_not_relax_the_local_base_url_parser(self):
        for local in (self.lan_origin, "http://0.0.0.0:8790", "http://localhost:8790/", "http://[::1]:8790"):
            with self.subTest(local=local), self.assertRaises(DomainError):
                self.config(self.lan_origin, local)

    def test_only_the_configured_loopback_spelling_is_accepted_with_lan(self):
        app, _ = self.boundary(self.config(self.lan_origin, "http://localhost:8790"))
        self.assertEqual(call(app, "GET", "/", [(b"host", b"LOCALHOST:8790"),
                                                 (b"origin", b"http://localhost:8790")])[0], 200)
        self.assertEqual(call(app, "GET", "/", [(b"host", self.lan_host.encode())])[0], 200)
        response = call(app, "GET", "/", [(b"host", self.local_host.encode())])
        self.assertEqual((response[0], error_code(response[2])), (403, "invalid_host"))

    def test_unsafe_unlisted_and_duplicate_hosts_are_rejected(self):
        app, downstream = self.boundary(self.config(self.lan_origin))
        for value in (b"0.0.0.0:8790", b"localhost:8790", b"192.168.1.11:8790",
                      b"192.168.1.10:8791", b"192.168.1.10:*", b"192.168.1.10",
                      b"192.168.1.10:08790", b"192.168.1.10:8790 ", b"evil.example:8790",
                      b"[::1]:8790", b"\xff:8790"):
            response = call(app, "GET", "/", [(b"host", value)])
            self.assertEqual((response[0], error_code(response[2])), (403, "invalid_host"))
        for headers in ([], [(b"host", self.lan_host.encode())] * 2,
                        [(b"host", self.local_host.encode()), (b"Host", self.lan_host.encode())]):
            response = call(app, "GET", "/", headers)
            self.assertEqual((response[0], error_code(response[2])), (403, "invalid_host"))
        self.assertEqual(downstream.calls, [])

    def test_origins_must_match_the_selected_host_without_duplicates(self):
        app, downstream = self.boundary(self.config(self.lan_origin))
        for host, origin, opposite in ((self.local_host, self.local_origin, self.lan_origin),
                                       (self.lan_host, self.lan_origin, self.local_origin)):
            host_header = (b"host", host.encode())
            for value in (opposite, "null", "http://evil.example:8790", "https://" + host,
                          origin + "/", origin + " ", origin.replace(":8790", ":8791")):
                response = call(app, "GET", "/", [host_header, (b"origin", value.encode())])
                self.assertEqual((response[0], error_code(response[2])), (403, "invalid_origin"))
            for values in ((origin, origin), (origin, opposite), (opposite, origin)):
                response = call(app, "GET", "/", [host_header, (b"origin", values[0].encode()),
                                                   (b"Origin", values[1].encode())])
                self.assertEqual((response[0], error_code(response[2])), (403, "invalid_origin"))
        self.assertEqual(downstream.calls, [])

    def test_bearer_is_required_over_both_identities_for_all_protected_paths_and_methods(self):
        app, downstream = self.boundary(self.config(self.lan_origin))
        protected = (("GET", "/mcp"), ("HEAD", "/mcp/"), ("GET", "/MCP/nested"),
                     ("GET", "/x/../mcp"), ("POST", "/"), ("PUT", "/api/plans"),
                     ("PATCH", "/api/plans"), ("DELETE", "/"), ("OPTIONS", "/"),
                     ("TRACE", "/"), ("CONNECT", "/"), ("CUSTOM", "/"))
        for host in (self.local_host, self.lan_host):
            for method, path in protected:
                for authorization in ([], [(b"authorization", f"Bearer {OTHER_TOKEN}".encode())],
                                      [self.bearer, self.bearer]):
                    with self.subTest(host=host, method=method, path=path, auth_count=len(authorization)):
                        response = call(app, method, path, [(b"host", host.encode()), *authorization])
                        self.assertEqual((response[0], error_code(response[2])), (401, "unauthorized"))
                        self.assertIn(("www-authenticate", "Bearer"), response[1])
        self.assertEqual(downstream.calls, [])

    def test_forwarding_headers_are_rejected_for_both_identities(self):
        app, downstream = self.boundary(self.config(self.lan_origin))
        for host in (self.local_host, self.lan_host):
            for name in (b"forwarded", b"x-forwarded-host", b"X-Forwarded-For", b"x-forwarded-proto",
                         b"x-forwarded-port", b"x-forwarded-server", b"x-original-host", b"x-host"):
                response = call(app, "POST", "/mcp", [(b"host", host.encode()), self.bearer,
                                                       (name, self.lan_host.encode())])
                self.assertEqual((response[0], error_code(response[2])), (403, "forwarded_rejected"))
        self.assertEqual(downstream.calls, [])

    def test_websocket_handshakes_require_bearer_and_matching_identity(self):
        app, downstream = self.boundary(self.config(self.lan_origin))

        async def handshake(headers):
            sent = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(message):
                sent.append(message)
            await app(make_scope("GET", "/", headers, scope_type="websocket"), receive, send)
            return sent

        for host, origin, opposite in ((self.local_host, self.local_origin, self.lan_origin),
                                       (self.lan_host, self.lan_origin, self.local_origin)):
            headers = [(b"host", host.encode()), (b"origin", origin.encode())]
            for auth in ([], [(b"authorization", f"Bearer {OTHER_TOKEN}".encode())], [self.bearer] * 2):
                self.assertEqual(asyncio.run(handshake(headers + auth)), [{"type": "websocket.close", "code": 1008}])
            self.assertEqual(asyncio.run(handshake([(b"host", host.encode()),
                                                    (b"origin", opposite.encode()), self.bearer])),
                             [{"type": "websocket.close", "code": 1008}])
            self.assertEqual(asyncio.run(handshake(headers + [self.bearer])),
                             [{"type": "websocket.close", "code": 1000}])
        self.assertEqual(len(downstream.calls), 2)

    def real_app(self, config):
        descriptor = os.open(self.data_dir / "service-token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as token_file:
            token_file.write(SYNTHETIC_TOKEN)
        app = build_app(config)
        self.addCleanup(app.state.close)
        epoch = app.state.service.read_service_info().epoch
        command = parse_command({"kind": "create_plan", "epoch": epoch, "key": "synthetic-lan-plan",
                                 "actor": "synthetic-orchestrator", "payload": {
                                     "creation_id": "synthetic-lan-plan", "draft": draft(),
                                     "new_plan_intent": "Synthetic plan for advertised LAN links"}})
        receipt = app.state.service.execute(command)
        return app, receipt.current_metadata.plan_id

    def test_default_service_and_plan_links_stay_local(self):
        app, plan_id = self.real_app(self.config())
        self.assertEqual(app.state.service.read_service_info().dashboard_url, self.local_origin + "/")
        self.assertEqual(app.state.service.read_plan(plan_id).metadata.dashboard_url,
                         self.local_origin + "/plans/" + plan_id)

    def test_real_http_sdk_initializes_discovers_tools_and_reads_same_lan_advertised_service(self):
        sock = bound_ephemeral_socket()
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        self.assertNotEqual(port, 8790)
        local_url = f"http://127.0.0.1:{port}"
        lan_url = f"http://192.168.1.10:{port}"
        app, plan_id = self.real_app(self.config(lan_url, local_url))
        capture = RequestCapturingMiddleware(app.middleware_stack)
        app.middleware_stack = capture

        async def exercise():
            infos = []
            async with running_service(app, sock):
                for identity in (lan_url, local_url):
                    headers = {"Host": identity.removeprefix("http://"), "Origin": identity,
                               "Authorization": f"Bearer {SYNTHETIC_TOKEN}"}
                    # Only the test's loopback socket is contacted. LAN identity is carried in headers.
                    async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=10) as http:
                        dashboard = await http.get(local_url + "/")
                        self.assertEqual(dashboard.status_code, 200)
                        self.assertFalse(any(name.startswith("access-control-") for name in dashboard.headers))
                        index = await http.get(local_url + "/api/plan-index")
                        self.assertEqual(index.status_code, 200)
                        self.assertIn(lan_url + "/plans/" + plan_id, index.text)
                        for bearer in ("", f"Bearer {OTHER_TOKEN}"):
                            denied = await http.post(local_url + "/mcp", headers={"Authorization": bearer}, json={})
                            self.assertEqual(denied.status_code, 401)
                        async with Client(streamable_http_client(local_url + "/mcp", http_client=http),
                                          mode="legacy") as client:
                            self.assertTrue(client.protocol_version)
                            names = {tool.name for tool in (await client.list_tools()).tools}
                            self.assertIn("read_service_info", names)
                            info = await client.call_tool("read_service_info", {})
                            self.assertFalse(info.is_error)
                            self.assertEqual(info.structured_content["dashboard_url"], lan_url + "/")
                            infos.append(info.structured_content)
                self.assertEqual(infos[0], infos[1])
            for identity in (lan_url, local_url):
                methods = {request.method for request in capture.captured_requests
                           if request.path == "/mcp" and request.headers.get("origin") == identity}
                self.assertTrue({"initialize", "tools/list", "tools/call"}.issubset(methods))

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
