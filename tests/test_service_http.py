"""Service configuration, explicit credential initialization and HTTP request boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import jsonschema
import uvicorn
from pydantic import BaseModel, ValidationError

from adaptive_planning.admin import initialize_installation, load_service_token
from adaptive_planning.config import Config, RequestBoundary, http_allowlist, load_config
from adaptive_planning.types import DomainError

SYNTHETIC_TOKEN = "synthetic-token-" + "A1b2C3d4" * 6
OTHER_TOKEN = "synthetic-other-" + "Z9y8X7w6" * 6
POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX owner and mode semantics")


class Body(BaseModel):
    count: int


class Recorder:
    """Synthetic downstream ASGI app: answers 200 and records that it was reached."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            self.calls.append({"type": "websocket"})
            await send({"type": "websocket.close", "code": 1000})
            return
        self.calls.append({"method": scope["method"], "path": scope["path"]})
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"downstream"})


def bound_ephemeral_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def make_scope(method="GET", path="/mcp", headers=(), query=b"", scope_type="http"):
    return {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": [(name.encode() if isinstance(name, str) else name, bytes(value)) for name, value in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8790),
    }


def call(app, method="GET", path="/mcp", headers=(), query=b"", body=b""):
    messages: list[dict] = []

    async def run():
        pending = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(make_scope(method, path, headers, query), receive, send)

    asyncio.run(run())
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    response_headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in start["headers"]]
    return start["status"], response_headers, payload


def error_code(payload: bytes) -> str:
    return json.loads(payload)["error"]["code"]


class CapturedLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record)
        if record.exc_info:
            text += "\n" + "".join(logging.Formatter().formatException(record.exc_info))
        self.lines.append(text + " " + repr(record.args))


class TestService(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data_dir = Path(self._directory.name)
        self.config = Config(self.data_dir)
        self.host, self.origin = http_allowlist(self.config)
        self.downstream = Recorder()
        self.app = RequestBoundary(self.downstream, self.config, SYNTHETIC_TOKEN)
        self.bearer = ("authorization", f"Bearer {SYNTHETIC_TOKEN}".encode())

    def request(self, method="GET", path="/mcp", *, host="default", origin=None, auth=True, extra=(), query=b""):
        headers = []
        if host == "default":
            headers.append(("host", self.host.encode()))
        elif host is not None:
            headers.append(("host", host.encode()))
        if origin is not None:
            headers.append(("origin", origin.encode()))
        if auth:
            headers.append(self.bearer)
        headers.extend(extra)
        return call(self.app, method, path, headers, query)

    def assertRejected(self, response, status, code, reached_before):
        got_status, headers, payload = response
        self.assertEqual((got_status, error_code(payload)), (status, code))
        self.assertEqual(len(self.downstream.calls), reached_before, "downstream handled a rejected request")
        self.assertFalse([name for name, _ in headers if name.startswith("access-control-")])

    # --- explicit credential initialization -------------------------------------------------

    def test_explicit_init_private_token(self):
        with TemporaryDirectory() as directory:
            config = Config(Path(directory))
            token_path = config.data_dir/"service-token"
            self.assertFalse(token_path.exists())
            initialize_installation(config)
            original = token_path.read_bytes()
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            initialize_installation(config)
            self.assertEqual(token_path.read_bytes(), original)

    def test_import_read_and_start_never_generate_token(self):
        config = Config(self.data_dir)
        load_config({"ADAPTIVE_PLANNING_DATA_DIR": str(self.data_dir)})
        http_allowlist(config)
        RequestBoundary(self.downstream, config, SYNTHETIC_TOKEN)
        with self.assertRaises(DomainError) as raised:
            load_service_token(config)
        self.assertEqual(raised.exception.code, "not_initialized")
        self.assertEqual(list(self.data_dir.iterdir()), [])
        missing = Config(self.data_dir/"not-created")
        with self.assertRaises(DomainError):
            load_service_token(missing)
        self.assertFalse(missing.data_dir.exists())

    def test_initialized_token_is_random_loadable_and_secret_free_on_failure(self):
        with TemporaryDirectory() as second_directory:
            other = Config(Path(second_directory))
            initialize_installation(self.config)
            initialize_installation(other)
            token = load_service_token(self.config)
            self.assertEqual(token.encode(), (self.data_dir/"service-token").read_bytes())
            self.assertGreaterEqual(len(token), 32)
            self.assertNotEqual(token, load_service_token(other))
        self.assertEqual([entry.name for entry in self.data_dir.iterdir()], ["service-token"])

    @POSIX_ONLY
    def test_load_requires_private_regular_owner_controlled_token(self):
        path = self.data_dir/"service-token"
        initialize_installation(self.config)
        token = path.read_text()
        for mode in (0o640, 0o604, 0o644, 0o666, 0o700):
            os.chmod(path, mode)
            with self.assertRaises(DomainError) as raised:
                load_service_token(self.config)
            self.assertEqual(raised.exception.code, "insecure_credential")
            self.assertNotIn(token, raised.exception.message)
            self.assertNotIn(str(self.data_dir), raised.exception.message)
            with self.assertRaises(DomainError):
                initialize_installation(self.config)
            self.assertEqual(path.read_text(), token)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        os.chmod(path, 0o600)
        self.assertEqual(load_service_token(self.config), token)

        real = self.data_dir/"real-token"
        path.rename(real)
        path.symlink_to(real)
        for operation in (load_service_token, initialize_installation):
            with self.assertRaises(DomainError) as raised:
                operation(self.config)
            self.assertIn(raised.exception.code, {"insecure_credential", "invalid_credential"})
        path.unlink()
        path.mkdir()
        with self.assertRaises(DomainError):
            load_service_token(self.config)

    @POSIX_ONLY
    def test_load_rejects_foreign_owner_and_malformed_content(self):
        path = self.data_dir/"service-token"
        initialize_installation(self.config)
        real_geteuid = os.geteuid
        os.geteuid = lambda: real_geteuid() + 1
        try:
            with self.assertRaises(DomainError) as raised:
                load_service_token(self.config)
            self.assertEqual(raised.exception.code, "insecure_credential")
        finally:
            os.geteuid = real_geteuid
        for content in (b"", b"short", b"has space " + b"x" * 40, b"\xff" * 40, b"x" * 5000):
            path.write_bytes(content)
            with self.assertRaises(DomainError) as raised:
                load_service_token(self.config)
            self.assertEqual(raised.exception.code, "invalid_credential")
            if content:
                self.assertNotIn(content[:16].decode("latin-1"), raised.exception.message)
            with self.assertRaises(DomainError):
                initialize_installation(self.config)
            self.assertEqual(path.read_bytes(), content)

    def test_concurrent_init_is_race_safe(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def initialize():
            barrier.wait()
            initialize_installation(self.config)
            return load_service_token(self.config)

        with ThreadPoolExecutor(workers) as pool:
            tokens = list(pool.map(lambda _: initialize(), range(workers)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual([entry.name for entry in self.data_dir.iterdir()], ["service-token"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.data_dir/"service-token").stat().st_mode), 0o600)

    # --- configuration ------------------------------------------------------------------------

    def test_config_defaults_and_environment(self):
        config = Config(self.data_dir)
        self.assertEqual((config.base_url, config.stale_seconds), ("http://127.0.0.1:8790", 900))
        self.assertEqual(http_allowlist(config), ("127.0.0.1:8790", "http://127.0.0.1:8790"))
        loaded = load_config({})
        self.assertEqual((loaded.data_dir, loaded.base_url, loaded.stale_seconds), (Path("/data"), "http://127.0.0.1:8790", 900))
        loaded = load_config({
            "ADAPTIVE_PLANNING_DATA_DIR": str(self.data_dir),
            "ADAPTIVE_PLANNING_BASE_URL": "http://localhost:9001",
            "ADAPTIVE_PLANNING_STALE_SECONDS": "30",
        })
        self.assertEqual(
            (loaded.data_dir, loaded.base_url, loaded.stale_seconds), (self.data_dir, "http://localhost:9001", 30)
        )
        self.assertEqual(http_allowlist(loaded), ("localhost:9001", "http://localhost:9001"))
        self.assertEqual(list(self.data_dir.iterdir()), [])

    def test_base_url_must_be_one_bound_loopback_http_authority(self):
        invalid = [
            "https://127.0.0.1:8790", "http://127.0.0.1", "http://localhost", "http://127.0.0.1:0",
            "http://127.0.0.1:65536", "http://127.0.0.1:08790", "http://127.0.0.1:-1", "http://127.0.0.1:",
            "http://0.0.0.0:8790", "http://[::1]:8790", "http://127.0.0.2:8790", "http://127.1:8790",
            "http://localhost.:8790", "http://LOCALHOST:8790", "http://example.com:8790", "http://*:8790",
            "http://user@127.0.0.1:8790", "http://user:%s@127.0.0.1:8790" % SYNTHETIC_TOKEN,
            "http://127.0.0.1:8790/", "http://127.0.0.1:8790/mcp", "http://127.0.0.1:8790?token=x",
            "http://127.0.0.1:8790#frag", "http://127.0.0.1:8790\n", " http://127.0.0.1:8790",
            "http://127.0.0.1:8790,http://localhost:8790", "http://127.0.0.1:８７９０", "", "127.0.0.1:8790",
        ]
        with self.assertRaises(DomainError) as baseline:
            Config(self.data_dir, base_url="https://example.invalid")
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(DomainError) as raised:
                    Config(self.data_dir, base_url=url)
                self.assertEqual(raised.exception.code, "invalid_config")
                # A constant message cannot echo the rejected value.
                self.assertEqual(raised.exception.message, baseline.exception.message)
                self.assertNotIn(SYNTHETIC_TOKEN, raised.exception.message)
        with self.assertRaises(DomainError):
            load_config({"ADAPTIVE_PLANNING_BASE_URL": "http://0.0.0.0:8790"})
        with self.assertRaises(DomainError):
            Config(self.data_dir, base_url=None)
        for seconds in (0, -1, True, 1.5, "900"):
            with self.subTest(seconds=seconds), self.assertRaises(DomainError):
                Config(self.data_dir, stale_seconds=seconds)
        for value in ("", "abc", "0", "-5", "1.5", " 9"):
            with self.subTest(env=value), self.assertRaises(DomainError):
                load_config({"ADAPTIVE_PLANNING_STALE_SECONDS": value})

    # --- request boundary ---------------------------------------------------------------------

    def test_all_route_host_origin_and_mcp_auth(self):
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method=method):
                before = len(self.downstream.calls)
                # Missing Origin (CLI client) with the right bearer is accepted.
                status, headers, payload = self.request(method)
                self.assertEqual((status, payload), (200, b"downstream"))
                self.assertEqual(len(self.downstream.calls), before + 1)
                self.assertEqual(self.downstream.calls[-1], {"method": method, "path": "/mcp"})
                self.assertFalse([name for name, _ in headers if name.startswith("access-control-")])
                # The validated Origin is accepted too.
                self.assertEqual(self.request(method, origin=self.origin)[0], 200)
                before = len(self.downstream.calls)
                # Missing bearer / wrong bearer / other scheme / empty bearer / trailing space.
                for value in (
                    None,
                    f"Bearer {OTHER_TOKEN}",
                    SYNTHETIC_TOKEN,
                    f"Basic {SYNTHETIC_TOKEN}",
                    "Bearer ",
                    f"Bearer {SYNTHETIC_TOKEN} ",
                ):
                    extra = () if value is None else (("authorization", value.encode()),)
                    response = self.request(method, extra=extra, auth=False)
                    self.assertRejected(response, 401, "unauthorized", before)
                    self.assertIn(("www-authenticate", "Bearer"), response[1])
                # Invalid Host, even with a correct bearer.
                for host in (None, "", "example.com", "127.0.0.1", "127.0.0.1:1", "localhost:8790", "evil.test:8790"):
                    self.assertRejected(self.request(method, host=host), 403, "invalid_host", before)
                # Supplied Origin that is not the validated origin.
                for origin in (
                    "null", "", "*", "http://example.com", "http://127.0.0.1", "http://127.0.0.1:1",
                    "http://localhost:8790", "https://127.0.0.1:8790", self.origin + "/", self.origin + ".evil.test",
                    self.origin.upper(),
                ):
                    self.assertRejected(self.request(method, origin=origin), 403, "invalid_origin", before)
        # A valid dashboard GET needs no bearer; with or without a valid Origin.
        before = len(self.downstream.calls)
        self.assertEqual(self.request("GET", "/", auth=False)[0], 200)
        self.assertEqual(self.request("GET", "/api/plans", auth=False, origin=self.origin)[0], 200)
        self.assertEqual(self.downstream.calls[before:][0], {"method": "GET", "path": "/"})
        # ...but still needs a valid Host and Origin.
        before = len(self.downstream.calls)
        self.assertRejected(self.request("GET", "/", host="example.com", auth=False), 403, "invalid_host", before)
        self.assertRejected(
            self.request("GET", "/", origin="http://example.com", auth=False), 403, "invalid_origin", before
        )

    def test_header_case_duplicates_and_forwarding(self):
        before = len(self.downstream.calls)
        host, origin, bearer = self.host.encode(), self.origin.encode(), f"Bearer {SYNTHETIC_TOKEN}".encode()
        # Raw header-name case is not significant; scheme case is not significant for Bearer.
        mixed = [(b"HoSt", host), (b"ORIGIN", origin), (b"AuthoriZation", b"bEaReR " + SYNTHETIC_TOKEN.encode())]
        self.assertEqual(call(self.app, "POST", "/mcp", mixed)[0], 200)
        self.assertEqual(call(self.app, "GET", "/mcp", [(b"Host", self.host.upper().encode()), (b"authorization", bearer)])[0], 200)
        before = len(self.downstream.calls)
        # Duplicate Host/Origin/Authorization are rejected whatever the order or whether values agree.
        duplicates = [
            ([(b"host", host), (b"host", host), (b"authorization", bearer)], 403, "invalid_host"),
            ([(b"host", host), (b"Host", b"example.com"), (b"authorization", bearer)], 403, "invalid_host"),
            ([(b"host", b"example.com"), (b"HOST", host), (b"authorization", bearer)], 403, "invalid_host"),
            ([(b"host", host), (b"origin", origin), (b"Origin", origin), (b"authorization", bearer)], 403, "invalid_origin"),
            ([(b"host", host), (b"origin", origin), (b"origin", b"http://example.com"), (b"authorization", bearer)], 403, "invalid_origin"),
            ([(b"host", host), (b"origin", b"http://example.com"), (b"origin", origin), (b"authorization", bearer)], 403, "invalid_origin"),
            ([(b"host", host), (b"authorization", bearer), (b"Authorization", bearer)], 401, "unauthorized"),
            ([(b"host", host), (b"authorization", b"Bearer " + OTHER_TOKEN.encode()), (b"authorization", bearer)], 401, "unauthorized"),
            ([(b"host", host), (b"authorization", bearer), (b"authorization", b"Bearer " + OTHER_TOKEN.encode())], 401, "unauthorized"),
        ]
        for headers, status, code in duplicates:
            with self.subTest(headers=[name for name, _ in headers]):
                got = call(self.app, "POST", "/mcp", headers)
                self.assertEqual((got[0], error_code(got[2])), (status, code))
        # Malformed authority values never match.
        for malformed in (
            self.host + " ", " " + self.host, self.host + ",example.com", "example.com," + self.host,
            self.host + "\t", self.host + "@example.com", "user@" + self.host, self.host + ":8790", self.host + "/",
            "127.0.0.1:8790.", "127.0.0.1.:8790", "[::1]:8790", "127.1:8790", "2130706433:8790", "0x7f.0.0.1:8790",
            "127.0.0.1:08790", "127.0.0.1%00:8790", "127.0.0.1:8790\r\nX: y", "ｌｏｃａｌｈｏｓｔ:8790",
        ):
            got = call(self.app, "GET", "/mcp", [(b"host", malformed.encode("utf-8")), (b"authorization", bearer)])
            with self.subTest(host=malformed):
                self.assertEqual((got[0], error_code(got[2])), (403, "invalid_host"))
        # Forwarded-host/forwarding headers cannot bypass or accompany the allowlist.
        for name in (
            b"forwarded", b"x-forwarded-host", b"X-Forwarded-For", b"x-forwarded-proto", b"x-forwarded-port",
            b"x-forwarded-server", b"x-original-host", b"x-host",
        ):
            for value in (host, b"example.com", b"for=1.2.3.4;host=example.com"):
                got = call(self.app, "GET", "/mcp", [(b"host", host), (name, value), (b"authorization", bearer)])
                with self.subTest(header=name, value=value):
                    self.assertEqual((got[0], error_code(got[2])), (403, "forwarded_rejected"))
            got = call(self.app, "GET", "/", [(b"host", host), (name, host)])
            self.assertEqual((got[0], error_code(got[2])), (403, "forwarded_rejected"))
        got = call(self.app, "GET", "/mcp", [(b"host", b"example.com"), (b"x-forwarded-host", host), (b"authorization", bearer)])
        self.assertEqual((got[0], error_code(got[2])), (403, "invalid_host"))
        self.assertEqual(len(self.downstream.calls), before)

    def test_alternate_allowed_host_spellings_are_rejected(self):
        for base_url, allowed, alternates in (
            ("http://127.0.0.1:8790", "127.0.0.1:8790", ["localhost:8790", "[::1]:8790", "127.0.0.1:8791", "127.0.0.1"]),
            ("http://localhost:8790", "localhost:8790", ["127.0.0.1:8790", "localhost:8791", "localhost", "localhost.:8790"]),
        ):
            config = Config(self.data_dir, base_url=base_url)
            app = RequestBoundary(self.downstream, config, SYNTHETIC_TOKEN)
            auth = ("authorization", f"Bearer {SYNTHETIC_TOKEN}".encode())
            self.assertEqual(call(app, "GET", "/mcp", [("host".encode(), allowed.encode()), auth])[0], 200)
            for alternate in alternates:
                with self.subTest(base_url=base_url, host=alternate):
                    status, _, payload = call(app, "GET", "/mcp", [(b"host", alternate.encode()), auth])
                    self.assertEqual((status, error_code(payload)), (403, "invalid_host"))
                    status, _, payload = call(
                        app, "GET", "/mcp", [(b"host", allowed.encode()), (b"origin", f"http://{alternate}".encode()), auth]
                    )
                    self.assertEqual((status, error_code(payload)), (403, "invalid_origin"))

    def test_non_dashboard_methods_and_mcp_path_variants_need_bearer(self):
        before = len(self.downstream.calls)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertRejected(self.request(method, "/", auth=False), 401, "unauthorized", before)
        for path in ("/mcp", "/mcp/", "/mcp/sub", "/MCP", "//mcp", "/./mcp", "/static/../mcp", "/Mcp/x"):
            for method in ("GET", "HEAD", "POST", "DELETE", "OPTIONS", "PUT"):
                with self.subTest(path=path, method=method):
                    self.assertRejected(self.request(method, path, auth=False), 401, "unauthorized", before)
        self.assertEqual(self.request("HEAD", "/", auth=False)[0], 200)
        self.assertEqual(self.request("GET", "/mcpx", auth=False)[0], 200)
        # A CORS preflight is not a grant: no Access-Control headers, bearer still required.
        before = len(self.downstream.calls)
        preflight = self.request(
            "OPTIONS", "/mcp", origin=self.origin, auth=False,
            extra=(("access-control-request-method", b"POST"),),
        )
        self.assertRejected(preflight, 401, "unauthorized", before)

    def test_non_http_scopes(self):
        # Lifespan passes through to the wrapped app untouched.
        messages = []

        async def drive_lifespan():
            queue = asyncio.Queue()
            await queue.put({"type": "lifespan.startup"})
            await queue.put({"type": "lifespan.shutdown"})

            async def send(message):
                messages.append(message["type"])

            await self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, queue.get, send)

        asyncio.run(drive_lifespan())
        self.assertEqual(messages, ["lifespan.startup.complete", "lifespan.shutdown.complete"])

        # WebSocket handshakes get the same boundary: unauthenticated or bad Host never reaches the app.
        async def websocket(headers):
            sent = []

            async def receive():
                return {"type": "websocket.connect"}

            async def send(message):
                sent.append(message)

            await self.app(make_scope("GET", "/mcp", headers, scope_type="websocket"), receive, send)
            return sent

        before = len(self.downstream.calls)
        host = (b"host", self.host.encode())
        for headers in ([host], [(b"host", b"example.com"), self.bearer]):
            sent = asyncio.run(websocket(headers))
            self.assertEqual([m["type"] for m in sent], ["websocket.close"])
            self.assertEqual(sent[0]["code"], 1008)
        self.assertEqual(len(self.downstream.calls), before)
        asyncio.run(websocket([host, self.bearer]))
        self.assertEqual(len(self.downstream.calls), before + 1)

    def test_constant_time_comparison_is_used(self):
        import hmac
        from unittest import mock

        with mock.patch("adaptive_planning.config.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            self.request("POST")
            self.request("POST", extra=((("authorization", b"Bearer " + OTHER_TOKEN.encode())),), auth=False)
        self.assertGreaterEqual(compare.call_count, 2)

    def test_boundary_construction_validates_token_and_config(self):
        for token in ("", None, b"bytes", 5):
            with self.subTest(token=token), self.assertRaises(DomainError):
                RequestBoundary(self.downstream, self.config, token)
        self.assertNotIn(SYNTHETIC_TOKEN, repr(self.app))
        self.assertNotIn(SYNTHETIC_TOKEN, repr(vars(self.app)))

    # --- real socket evidence -----------------------------------------------------------------

    def serve(self, app, sock):
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on", access_log=False))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(timeout=15)
            sock.close()

        self.addCleanup(stop)
        return server

    @staticmethod
    def wire(port, raw: bytes) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", port), timeout=15) as client:
            client.sendall(raw)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        data = b"".join(chunks)
        status = int(data.split(b" ", 2)[1])
        return status, data.partition(b"\r\n\r\n")[2]

    def test_bound_ephemeral_port_boundary_over_uvicorn(self):
        sock = bound_ephemeral_socket()
        port = sock.getsockname()[1]
        self.assertNotEqual(port, 8790)
        config = Config(self.data_dir, base_url=f"http://127.0.0.1:{port}")
        host, origin = http_allowlist(config)
        self.assertEqual((host, origin), (f"127.0.0.1:{port}", f"http://127.0.0.1:{port}"))
        downstream = Recorder()
        self.serve(RequestBoundary(downstream, config, SYNTHETIC_TOKEN), sock)
        auth = f"Authorization: Bearer {SYNTHETIC_TOKEN}"

        def raw(method, path, *headers):
            lines = "".join(f"{header}\r\n" for header in headers)
            return f"{method} {path} HTTP/1.1\r\n{lines}Connection: close\r\n\r\n".encode()

        for method in ("GET", "POST", "DELETE"):
            length = ("Content-Length: 0",) if method == "POST" else ()
            self.assertEqual(self.wire(port, raw(method, "/mcp", f"Host: {host}", *length, auth))[0], 200)
            self.assertEqual(self.wire(port, raw(method, "/mcp", f"Host: {host}"))[0], 401)
            self.assertEqual(self.wire(port, raw(method, "/mcp", f"Host: {host}", f"Authorization: Bearer {OTHER_TOKEN}"))[0], 401)
            self.assertEqual(self.wire(port, raw(method, "/mcp", "Host: localhost:%d" % port, auth))[0], 403)
            self.assertEqual(self.wire(port, raw(method, "/mcp", f"Host: {host}", "Origin: http://example.com", auth))[0], 403)
            self.assertEqual(self.wire(port, raw(method, "/mcp", f"Host: {host}", f"Origin: {origin}", auth))[0], 200)
        self.assertEqual(self.wire(port, raw("GET", "/", f"Host: {host}"))[0], 200)
        self.assertEqual(self.wire(port, raw("GET", "/", f"Host: {host}", f"Origin: {origin}"))[0], 200)
        # Mixed-case names on the wire.
        self.assertEqual(self.wire(port, raw("GET", "/mcp", f"hOsT: {host}", f"AUTHORIZATION: Bearer {SYNTHETIC_TOKEN}", f"oRiGiN: {origin}"))[0], 200)
        # Port is part of the authority: the loopback port 8790 spelling is not.
        self.assertEqual(self.wire(port, raw("GET", "/", "Host: 127.0.0.1:8790"))[0], 403)
        # Forwarding headers never bypass.
        status, payload = self.wire(port, raw("GET", "/", f"Host: {host}", "X-Forwarded-Host: example.com"))
        self.assertEqual((status, error_code(payload)), (403, "forwarded_rejected"))
        # Duplicate Host is never forwarded as valid, whether the server or boundary rejects it.
        before = len(downstream.calls)
        status, _ = self.wire(port, raw("GET", "/mcp", f"Host: {host}", "Host: example.com", auth))
        self.assertIn(status, {400, 403})
        status, _ = self.wire(port, raw("GET", "/mcp", "Host: example.com", f"Host: {host}", auth))
        self.assertIn(status, {400, 403})
        status, _ = self.wire(port, raw("GET", "/mcp", f"Host: {host}", f"Origin: {origin}", "Origin: http://example.com", auth))
        self.assertEqual(status, 403)
        status, _ = self.wire(port, raw("GET", "/mcp", f"Host: {host}", auth, auth))
        self.assertEqual(status, 401)
        self.assertEqual(len(downstream.calls), before)

    # --- secret-free errors, URLs and logs ----------------------------------------------------

    def test_logs_and_urls_are_secret_free(self):
        captured = CapturedLogs()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(captured)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(root.removeHandler, captured)

        schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}

        async def validating_app(scope, receive, send):
            if scope["type"] != "http":
                return
            request = await receive()
            payload = json.loads(request["body"] or b"{}")
            mode = payload.get("mode")
            if mode == "pydantic":
                Body.model_validate({"count": payload["count"]})
            elif mode == "schema":
                jsonschema.validate(payload["instance"], schema)
            elif mode == "exception":
                raise RuntimeError(f"failure carrying {payload['count']} and {request['body']!r}")
            elif mode == "after-start":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                raise RuntimeError(f"late failure {payload['count']}")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = RequestBoundary(validating_app, self.config, SYNTHETIC_TOKEN)
        secrets = (SYNTHETIC_TOKEN, OTHER_TOKEN)
        bearer = ("authorization", f"Bearer {SYNTHETIC_TOKEN}".encode())
        host = ("host", self.host.encode())
        outputs: list[bytes] = []
        urls: list[str] = [self.config.base_url, *http_allowlist(self.config)]

        def body(**fields):
            return json.dumps(fields).encode()

        # Successful request.
        outputs.append(call(app, "POST", "/mcp", [host, bearer], b"", body(mode="ok"))[2])
        # Secrets submitted as the bad input of Pydantic / JSON-schema validation and as exception text.
        for fields in (
            {"mode": "pydantic", "count": SYNTHETIC_TOKEN},
            {"mode": "pydantic", "count": OTHER_TOKEN},
            {"mode": "schema", "instance": {"count": SYNTHETIC_TOKEN}},
            {"mode": "schema", "instance": {"count": [OTHER_TOKEN]}},
            {"mode": "exception", "count": SYNTHETIC_TOKEN},
        ):
            status, headers, payload = call(app, "POST", "/mcp", [host, bearer], b"", body(**fields))
            self.assertEqual((status, error_code(payload)), (500, "internal_error"), fields)
            outputs.append(payload)
            outputs.extend(value.encode() for _, value in headers)
        # A failure after the response started must not escape with its text either.
        status, _, payload = call(app, "POST", "/mcp", [host, bearer], b"", body(mode="after-start", count=SYNTHETIC_TOKEN))
        outputs.append(payload)
        # The credential in the URL or in rejected requests.
        for query in (f"token={SYNTHETIC_TOKEN}".encode(), f"access_token={OTHER_TOKEN}".encode()):
            for headers in ([host], [host, ("authorization", b"Bearer " + OTHER_TOKEN.encode())], [("host", SYNTHETIC_TOKEN.encode()), bearer]):
                status, response_headers, payload = call(app, "POST", f"/mcp/{OTHER_TOKEN}", headers, query, body(mode="ok"))
                self.assertIn(status, {401, 403})
                outputs.append(payload)
                outputs.extend(value.encode() for _, value in response_headers)
        for origin in (SYNTHETIC_TOKEN, f"http://{OTHER_TOKEN}"):
            status, response_headers, payload = call(app, "GET", "/", [host, ("origin", origin.encode())])
            self.assertEqual(status, 403)
            outputs.append(payload)
            outputs.extend(value.encode() for _, value in response_headers)
        # Credential failures and invalid configuration.
        for url in (f"http://user:{SYNTHETIC_TOKEN}@127.0.0.1:8790", f"http://127.0.0.1:8790/{OTHER_TOKEN}"):
            with self.assertRaises(DomainError) as raised:
                Config(self.data_dir, base_url=url)
            outputs.append(raised.exception.message.encode())
            outputs.append(repr(raised.exception).encode())
        (self.data_dir/"service-token").write_text(SYNTHETIC_TOKEN + " bad")
        if os.name != "nt":
            os.chmod(self.data_dir/"service-token", 0o600)
        for operation in (load_service_token, initialize_installation):
            with self.assertRaises(DomainError) as raised:
                operation(self.config)
            outputs.append(raised.exception.message.encode())
            outputs.append(repr(raised.exception).encode())
        # Direct Pydantic error text proves the scenario carries (a truncated rendering of) the secret.
        with self.assertRaises(ValidationError) as raised:
            Body.model_validate({"count": SYNTHETIC_TOKEN})
        self.assertIn(SYNTHETIC_TOKEN[:24], str(raised.exception))

        for secret in secrets:
            for fragment in (secret, secret[:24]):
                for output in outputs:
                    self.assertNotIn(fragment.encode(), output)
                for url in urls:
                    self.assertNotIn(fragment, url)
                for line in captured.lines:
                    self.assertNotIn(fragment, line)
        self.assertTrue(captured.lines, "expected the boundary to log rejected and failed requests")
        self.assertTrue(any("internal_error" in line for line in captured.lines))
        self.assertTrue(any("unauthorized" in line for line in captured.lines))

    def test_secret_free_over_uvicorn_wire_and_server_logs(self):
        captured = CapturedLogs()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(captured)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, old_level)
        self.addCleanup(root.removeHandler, captured)
        sock = bound_ephemeral_socket()
        port = sock.getsockname()[1]
        config = Config(self.data_dir, base_url=f"http://127.0.0.1:{port}")

        async def failing(scope, receive, send):
            if scope["type"] == "http":
                request = await receive()
                Body.model_validate_json(request["body"])

        self.serve(RequestBoundary(failing, config, SYNTHETIC_TOKEN), sock)
        payload = json.dumps({"count": SYNTHETIC_TOKEN}).encode()
        request = (
            f"POST /mcp?token={OTHER_TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {SYNTHETIC_TOKEN}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode() + payload
        status, response = self.wire(port, request)
        self.assertEqual((status, error_code(response)), (500, "internal_error"))
        status, response = self.wire(
            port, f"GET /mcp?token={SYNTHETIC_TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
        )
        self.assertEqual(status, 401)
        self.assertNotIn(SYNTHETIC_TOKEN.encode(), response)
        for line in captured.lines:
            self.assertNotIn(OTHER_TOKEN, line)
            self.assertNotIn(SYNTHETIC_TOKEN, line)


if __name__ == "__main__":
    unittest.main()
