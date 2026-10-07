"""Private orchestrator capabilities and plan ownership.

Capabilities are random 32-byte bearer secrets. Only their SHA-256 digests are kept in the capability table and
used for lookup; the recoverable plaintext lives solely in the idempotency record of the command that issued it,
which inherits the store's owner-only (0600) file protection. That is access control, not encryption.

A takeover fences every earlier orchestrator capability of the plan. Fenced capabilities fail new mutations and
replays of their earlier commands, and a superseded grant is never handed out again by bootstrap replay.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid

from adaptive_planning.types import (
    BOOTSTRAP_KINDS,
    ORCHESTRATOR_KINDS,
    WORKER_KINDS,
    Command,
    DomainError,
    PlanId,
    PrivateGrant,
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _installation_epoch(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT epoch FROM installation WHERE id = 1").fetchone()[0]


def issue_orchestrator(connection: sqlite3.Connection, plan_id: PlanId, actor: str) -> PrivateGrant:
    """Persist a fresh orchestrator capability for the plan and return its one-time private grant."""
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO capabilities (capability_id, epoch, plan_id, role, secret_hash, valid_until, revoked, payload) "
        "VALUES (?, ?, ?, 'orchestrator', ?, NULL, 0, ?)",
        (str(uuid.uuid4()), _installation_epoch(connection), plan_id, hash_token(token),
         json.dumps({"actor": actor}, ensure_ascii=False)),
    )
    return PrivateGrant(orchestrator_token=token, attempt_token=None)


def command_relay(command: Command) -> str | None:
    """Return the relay of a worker command, or None for direct worker commands and every other kind."""
    if command.kind == "record_checkpoint":
        return getattr(command.payload.get("checkpoint"), "relay", None)
    if command.kind in WORKER_KINDS:
        return command.payload.get("relay")
    return None


def require_kind_capabilities(
    kind: str, relay: str | None, orchestrator_token: str | None, attempt_token: str | None,
) -> None:
    """Enforce the capability matrix's token presence and forbidden extras before any authentication.

    Forbidden extras are a malformed envelope (`invalid`); a missing required token is `forbidden`.
    """
    if kind in BOOTSTRAP_KINDS:
        needs_orchestrator, needs_attempt = False, False
    elif kind in WORKER_KINDS:
        needs_orchestrator, needs_attempt = relay is not None, True
    elif kind in ORCHESTRATOR_KINDS:
        needs_orchestrator, needs_attempt = True, False
    else:
        raise DomainError("invalid", f"Unknown command kind {kind!r}")
    checks = (("orchestrator", orchestrator_token, needs_orchestrator), ("attempt", attempt_token, needs_attempt))
    for name, token, needed in checks:
        if token is not None and not needed:
            raise DomainError("invalid", f"Command kind {kind!r} forbids a {name} token here")
    for name, token, needed in checks:
        if needed and not token:
            raise DomainError("forbidden", f"Command kind {kind!r} requires a {name} token")


def require_orchestrator(connection: sqlite3.Connection, command: Command) -> None:
    """Authenticate the current orchestrator of the command's plan, epoch and generation.

    Runs before any exact replay: a fenced capability can neither mutate nor replay. Absent or unknown
    capabilities are `forbidden`; capabilities superseded by a takeover are `fenced`. The operation-required
    ownership version must be supplied here, but its value is compared only for new commands.
    """
    token, plan_id = command.orchestrator_token, command.plan_id
    if not token or plan_id is None:
        raise DomainError("forbidden", "An orchestrator capability for the plan is required")
    row = connection.execute(
        "SELECT revoked, valid_until FROM capabilities WHERE plan_id = ? AND role = 'orchestrator' "
        "AND secret_hash = ? AND epoch = ?",
        (plan_id, hash_token(token), _installation_epoch(connection)),
    ).fetchone()
    if row is None or row[1] is not None:
        raise DomainError("forbidden", "Orchestrator capability is not valid for this plan")
    if row[0]:
        raise DomainError("fenced", "Orchestrator capability was superseded by a takeover")
    if command.ownership_version is None:
        raise DomainError("invalid", "ownership_version is required")
    if command.generation is None:
        raise DomainError("invalid", "generation is required")
    generation = connection.execute("SELECT generation FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if generation is None or command.generation != generation[0]:
        raise DomainError("stale", "The plan generation has changed; re-read the plan")


def current_private(
    connection: sqlite3.Connection, plan_id: PlanId, private: PrivateGrant | None,
) -> PrivateGrant | None:
    """Return recovered grant material only while its orchestrator capability is current in this epoch.

    Anything else, including grant material this module did not issue, is withheld.
    """
    if private is None or private.orchestrator_token is None or private.attempt_token is not None:
        return None
    row = connection.execute(
        "SELECT 1 FROM capabilities WHERE plan_id = ? AND role = 'orchestrator' AND secret_hash = ? "
        "AND epoch = ? AND revoked = 0 AND valid_until IS NULL",
        (plan_id, hash_token(private.orchestrator_token), _installation_epoch(connection)),
    ).fetchone()
    return private if row is not None else None


def fence_orchestrators(connection: sqlite3.Connection, plan_id: PlanId) -> None:
    """Fence every current orchestrator capability of the plan; worker capabilities are untouched."""
    connection.execute(
        "UPDATE capabilities SET revoked = 1 WHERE plan_id = ? AND role = 'orchestrator' AND revoked = 0",
        (plan_id,))


def current_ownership(connection: sqlite3.Connection, plan_id: PlanId) -> tuple[str, int]:
    """Return the current owning actor and ownership version of a plan."""
    version = connection.execute("SELECT ownership_version FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if version is None:
        raise DomainError("not_found", f"Plan {plan_id!r} does not exist")
    row = connection.execute(
        "SELECT payload FROM capabilities WHERE plan_id = ? AND role = 'orchestrator' AND revoked = 0 "
        "ORDER BY rowid DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    actor = json.loads(row[0]).get("actor", "") if row else ""
    return actor, version[0]
