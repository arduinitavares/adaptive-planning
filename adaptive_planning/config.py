"""Service configuration and the HTTP request boundary (Host, Origin and bearer checks)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Mapping

from starlette.types import ASGIApp, Receive, Scope, Send

from adaptive_planning.types import DomainError

DEFAULT_BASE_URL = "http://127.0.0.1:8790"
DEFAULT_DATA_DIR = "/data"
DEFAULT_STALE_SECONDS = 900

ENV_DATA_DIR = "ADAPTIVE_PLANNING_DATA_DIR"
ENV_BASE_URL = "ADAPTIVE_PLANNING_BASE_URL"
ENV_LAN_BASE_URL = "ADAPTIVE_PLANNING_LAN_BASE_URL"
ENV_STALE_SECONDS = "ADAPTIVE_PLANNING_STALE_SECONDS"

_LOG = logging.getLogger("adaptive_planning.http")
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})
_FORWARDING_HEADERS = frozenset({b"forwarded", b"x-original-host", b"x-host"})
_PUBLIC_MESSAGES = {
    "invalid_host": "Host header is not allowed.",
    "invalid_origin": "Origin header is not allowed.",
    "forwarded_rejected": "Forwarding headers are not accepted.",
    "unauthorized": "A valid bearer credential is required.",
    "internal_error": "The request could not be completed.",
}
_BASE_URL = re.compile(r"http://(127\.0\.0\.1|localhost):([1-9][0-9]{0,4})")
_LAN_BASE_URL = re.compile(r"http://([0-9]{1,3}(?:\.[0-9]{1,3}){3}):([1-9][0-9]{0,4})")
_LAN_NETWORKS = tuple(IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


def _invalid_config(message: str) -> DomainError:
    # Messages never echo configuration values: a URL may embed credentials.
    return DomainError("invalid_config", message)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    base_url: str = DEFAULT_BASE_URL
    stale_seconds: int = DEFAULT_STALE_SECONDS
    lan_base_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, Path):
            raise _invalid_config("data_dir must be a path.")
        _, local_port = _parse_base_url(self.base_url)
        if self.lan_base_url is not None:
            _parse_lan_base_url(self.lan_base_url, local_port)
        if isinstance(self.stale_seconds, bool) or not isinstance(self.stale_seconds, int) or self.stale_seconds < 1:
            raise _invalid_config("stale_seconds must be a positive integer.")


def _parse_base_url(base_url: object) -> tuple[str, int]:
    match = _BASE_URL.fullmatch(base_url) if isinstance(base_url, str) else None
    if match is None or int(match.group(2)) > 65535:
        raise _invalid_config(
            "base_url must be http://127.0.0.1:<port> or http://localhost:<port> with no userinfo, path, query or fragment."
        )
    return match.group(1), int(match.group(2))


def load_config(environment: Mapping[str, str]) -> Config:
    """Build a Config from an environment mapping. Never touches the filesystem."""
    data_dir = environment.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)
    if not data_dir or "\0" in data_dir:
        raise _invalid_config(f"{ENV_DATA_DIR} must be a non-empty path.")
    stale = environment.get(ENV_STALE_SECONDS)
    if stale is not None and _POSITIVE_INTEGER.fullmatch(stale) is None:
        raise _invalid_config(f"{ENV_STALE_SECONDS} must be a positive integer.")
    return Config(
        Path(data_dir),
        environment.get(ENV_BASE_URL, DEFAULT_BASE_URL),
        DEFAULT_STALE_SECONDS if stale is None else int(stale),
        environment.get(ENV_LAN_BASE_URL),
    )


def http_allowlist(config: Config) -> tuple[str, str]:
    """Return the configured loopback Host/Origin pair (the single-pair API)."""
    hostname, port = _parse_base_url(config.base_url)
    return f"{hostname}:{port}", f"http://{hostname}:{port}"


def _parse_lan_base_url(base_url: object, local_port: int) -> tuple[str, int]:
    message = (
        "lan_base_url must be plain HTTP with a canonical RFC1918 IPv4 address and the local port, "
        "with no userinfo, path, query or fragment."
    )
    match = _LAN_BASE_URL.fullmatch(base_url) if isinstance(base_url, str) else None
    if match is None or int(match.group(2)) > 65535 or int(match.group(2)) != local_port:
        raise _invalid_config(message)
    try:
        address = IPv4Address(match.group(1))
    except ValueError:
        raise _invalid_config(message) from None
    if not any(address in network for network in _LAN_NETWORKS):
        raise _invalid_config(message)
    return str(address), int(match.group(2))


def http_allowlists(config: Config) -> tuple[tuple[str, str], ...]:
    """Return exact matched Host/Origin pairs for local and optional LAN access."""
    local = http_allowlist(config)
    if config.lan_base_url is None:
        return (local,)
    _, local_port = _parse_base_url(config.base_url)
    hostname, port = _parse_lan_base_url(config.lan_base_url, local_port)
    return local, (f"{hostname}:{port}", f"http://{hostname}:{port}")


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _segments(path: str) -> list[str]:
    resolved: list[str] = []
    for segment in path.lower().replace("\\", "/").split("/"):
        if segment == "..":
            if resolved:
                resolved.pop()
        elif segment not in ("", "."):
            resolved.append(segment)
    return resolved


def _public_method(method: str) -> str:
    return method if method in _KNOWN_METHODS else "OTHER"


class RequestBoundary:
    """Pure ASGI middleware applied before every route.

    Order: Host, forwarding headers, Origin, then the bearer credential for /mcp, any
    non-read method and WebSocket handshakes. Dashboard GET/HEAD need no bearer. No CORS
    grant is ever emitted.
    """

    def __init__(self, app: ASGIApp, config: Config, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise _invalid_config("A service credential is required.")
        self._app = app
        self._origins_by_host = dict(http_allowlists(config))
        self._token_digest = _digest(token.encode("utf-8"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "GET") if kind == "http" else "GET"
        rejection = self._check(scope, method, kind)
        if rejection is not None:
            _LOG.warning("request rejected: code=%s method=%s", rejection[1], _public_method(method))
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _respond(send, *rejection)
            return
        await self._run(scope, receive, send, method, kind)

    async def _run(self, scope: Scope, receive: Receive, send: Send, method: str, kind: str) -> None:
        started = False

        async def tracking_send(message) -> None:
            nonlocal started
            if message["type"] in ("http.response.start", "websocket.accept"):
                started = True
            await send(message)

        try:
            await self._app(scope, receive, tracking_send)
        except Exception as error:
            # Exception text can carry request data (validation errors echo their input), so only
            # the exception type is logged and nothing from it reaches the client.
            _LOG.error(
                "request failed: code=internal_error method=%s exception=%s",
                _public_method(method),
                type(error).__name__,
            )
            if started:
                return
            try:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1011})
                else:
                    await _respond(send, 500, "internal_error")
            except Exception:
                _LOG.error("request failed: error response could not be sent")

    def _check(self, scope: Scope, method: str, kind: str) -> tuple[int, str] | None:
        headers: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(bytes(name).lower(), []).append(bytes(value))

        hosts = headers.get(b"host", [])
        if len(hosts) != 1 or not self._is_host(hosts[0]):
            return 403, "invalid_host"
        if any(name in _FORWARDING_HEADERS or name.startswith(b"x-forwarded-") for name in headers):
            return 403, "forwarded_rejected"
        origins = headers.get(b"origin", [])
        expected_origin = self._origins_by_host[hosts[0].decode("ascii").lower()]
        if origins and (len(origins) != 1 or origins[0] != expected_origin.encode()):
            return 403, "invalid_origin"
        if self._needs_bearer(scope, method, kind) and not self._is_authorized(headers.get(b"authorization", [])):
            return 401, "unauthorized"
        return None

    def _is_host(self, value: bytes) -> bool:
        try:
            return value.decode("ascii").lower() in self._origins_by_host
        except UnicodeDecodeError:
            return False

    @staticmethod
    def _needs_bearer(scope: Scope, method: str, kind: str) -> bool:
        if kind == "websocket" or method.upper() not in ("GET", "HEAD"):
            return True
        path = scope.get("path", "")
        for candidate in (path, scope.get("root_path", "") + path):
            segments = _segments(candidate)
            if segments and segments[0] == "mcp":
                return True
        return False

    def _is_authorized(self, values: list[bytes]) -> bool:
        scheme, space, credentials = values[0].partition(b" ") if len(values) == 1 else (b"", b"", b"")
        matches = hmac.compare_digest(_digest(credentials), self._token_digest)
        return matches and bool(space) and scheme.lower() == b"bearer"


async def _respond(send: Send, status: int, code: str) -> None:
    error = DomainError(code, _PUBLIC_MESSAGES[code])
    body = json.dumps({"error": {"code": error.code, "message": error.message}}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
