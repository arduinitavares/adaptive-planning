"""Private orchestrator capabilities and plan ownership.

Capabilities are random bearer secrets. Only their SHA-256 digests are kept in the capability table; the
plaintext lives solely in the protected idempotency record of the command that issued it. Takeover and the
complete replay/fencing rules are layered on `authenticate_orchestrator` by a later task.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime

from adaptive_planning.types import Command, DomainError, PlanId, PrivateGrant


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


def authenticate_orchestrator(connection: sqlite3.Connection, command: Command, now: datetime) -> None:
    """Require a current, unrevoked orchestrator capability for the command's plan."""
    token = command.orchestrator_token
    if not token or command.plan_id is None:
        raise DomainError("forbidden", "An orchestrator capability for the plan is required")
    row = connection.execute(
        "SELECT valid_until FROM capabilities WHERE plan_id = ? AND role = 'orchestrator' AND secret_hash = ? "
        "AND revoked = 0 AND epoch = ?",
        (command.plan_id, hash_token(token), _installation_epoch(connection)),
    ).fetchone()
    if row is None or (row[0] is not None and row[0] <= now.isoformat()):
        raise DomainError("forbidden", "Orchestrator capability is not valid for this plan")


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
