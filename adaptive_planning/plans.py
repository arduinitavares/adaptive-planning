"""Durable plan creation, semantic revisions, projection and bounded coordination reads.

Handlers run on the caller's transaction connection (`Store.write`/`Store.read`). They never open
transactions, call models, touch the network, or run host commands.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import dataclasses
import hashlib
import json
import sqlite3
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from adaptive_planning.core import (
    MAX_FILE_BYTES,
    MAX_STEPS,
    MAX_TEXT_LEN,
    PlanValidationError,
    renew_authorization,
    validate_execution_scope,
    validate_published_snapshot,
    validate_service_graph,
)
from adaptive_planning.ownership import current_ownership, issue_orchestrator
from adaptive_planning.types import (
    AttemptView,
    Authorization,
    CoordinationItem,
    CoordinationPage,
    CoordinationView,
    DomainError,
    Event,
    Evidence,
    HoldView,
    InvalidationView,
    Metadata,
    MutationReceipt,
    PlanDraft,
    PlanId,
    PlanView,
    PrivateGrant,
    ReadyStep,
    ReservationView,
    RetirementRequest,
    Review,
    ReviewView,
    _to_artifact_ref,
    _to_evidence,
    _to_execution_context,
    _to_plan_draft,
)

Command = Any

COORDINATION_SUMMARY_LIMIT = 50
COORDINATION_PAGE_DEFAULT = 100
COORDINATION_PAGE_MAX = 500
MAX_PAGE_BYTES = MAX_FILE_BYTES
EVENTS_LIMIT_DEFAULT = 100
EVENTS_LIMIT_MAX = 1000

LIVE_ATTEMPT_STATES = ("assigned", "active", "submitted", "stale")
_START_KEY = (-1, -1, -1)


# --------------------------------------------------------------------------------------
# Service context (clock and public dashboard base URL), injected by PlanningService
# --------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ServiceContext:
    base_url: str
    clock: Callable[[], datetime]


_CONTEXT: ContextVar[ServiceContext] = ContextVar(
    "adaptive_planning_service_context",
    default=ServiceContext("", lambda: datetime.now(timezone.utc)),
)


@contextlib.contextmanager
def service_context(base_url: str, clock: Callable[[], datetime]) -> Iterator[None]:
    token = _CONTEXT.set(ServiceContext(base_url.rstrip("/"), clock))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _now() -> datetime:
    return _CONTEXT.get().clock()


# --------------------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------------------
def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def command_digest(command: Command) -> str:
    return sha256_hex(canonical_bytes(command))


# --------------------------------------------------------------------------------------
# Stored response codec (idempotency records)
# --------------------------------------------------------------------------------------
def encode_response(receipt: MutationReceipt) -> str:
    private = receipt.private
    return json.dumps({
        "committed": to_jsonable(receipt.committed),
        "committed_metadata": to_jsonable(receipt.committed_metadata),
        "private": None if private is None else {
            "orchestrator_token": private.orchestrator_token, "attempt_token": private.attempt_token},
    }, sort_keys=True, ensure_ascii=False, allow_nan=False)


def decode_response(text: str) -> tuple[dict[str, Any], Metadata, PrivateGrant | None]:
    data = json.loads(text)
    private = data["private"]
    grant = None if private is None else PrivateGrant(
        orchestrator_token=private["orchestrator_token"], attempt_token=private["attempt_token"])
    return data["committed"], Metadata(**data["committed_metadata"]), grant


# --------------------------------------------------------------------------------------
# Stored plan model
# --------------------------------------------------------------------------------------
@dataclasses.dataclass
class _Step:
    step_id: str
    state: str
    definition: dict[str, Any]
    evidence: list[str]
    retired_reason: str | None
    ordinal: int


@dataclasses.dataclass
class _Plan:
    plan_id: str
    generation: str
    revision: int
    ownership_version: int
    created_at: str
    updated_at: str
    draft_json: dict[str, Any]
    steps: list[_Step]

    @property
    def authorization(self) -> Authorization:
        return _authorization(self.draft_json["authorization"])

    def draft(self) -> PlanDraft:
        return _to_plan_draft({**self.draft_json, "steps": [s.definition for s in self.steps]})


def _authorization(data: Mapping[str, Any]) -> Authorization:
    return Authorization(
        mode=data["mode"], interval=data["interval"], since_revision=data["since_revision"],
        note=data["note"], baseline=tuple(data["baseline"]), claimed=data["claimed"])


def _installation_epoch(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT epoch FROM installation WHERE id = 1").fetchone()[0]


def _store_sequence(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]


def _load_plan(connection: sqlite3.Connection, plan_id: PlanId) -> _Plan:
    row = connection.execute(
        "SELECT generation, revision, ownership_version, created_at, updated_at, payload "
        "FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", f"Plan {plan_id!r} does not exist")
    steps = []
    for step_id, state, payload in connection.execute(
            "SELECT step_id, state, payload FROM steps WHERE plan_id = ?", (plan_id,)):
        data = json.loads(payload)
        steps.append(_Step(step_id, state, data["definition"], list(data.get("evidence", [])),
                           data.get("retired_reason"), data.get("ordinal", 0)))
    steps.sort(key=lambda s: s.ordinal)
    return _Plan(plan_id, row[0], row[1], row[2], row[3], row[4], json.loads(row[5])["draft"], steps)


def _step_payload(step: _Step, actor: str | None = None) -> str:
    data: dict[str, Any] = {"ordinal": step.ordinal, "definition": step.definition, "evidence": step.evidence}
    if step.retired_reason is not None:
        data["retired_reason"] = step.retired_reason
    if actor is not None:
        data["retired_by"] = actor
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _metadata(connection: sqlite3.Connection, plan_id: PlanId) -> Metadata:
    row = connection.execute(
        "SELECT generation, revision, ownership_version FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", f"Plan {plan_id!r} does not exist")
    return Metadata(epoch=_installation_epoch(connection), plan_id=plan_id, generation=row[0], revision=row[1],
                    ownership_version=row[2], dashboard_url=f"{_CONTEXT.get().base_url}/plans/{plan_id}")


# --------------------------------------------------------------------------------------
# Authority aggregates and derived status
# --------------------------------------------------------------------------------------
_LIVE_SQL = ",".join(f"'{s}'" for s in LIVE_ATTEMPT_STATES)
_GLOBAL_HOLD_SQL = (
    "CASE WHEN json_valid(reason) THEN json_extract(reason, '$.step_id') ELSE NULL END IS NULL")


@dataclasses.dataclass(frozen=True)
class _Aggregates:
    has_live_attempts: bool
    has_stale_attempt: bool
    has_unresolved_uses: bool
    has_plan_holds: bool
    has_global_hold: bool
    store_hold: bool
    restore_cutoff: str | None
    counts: dict[str, int]


def _aggregates(connection: sqlite3.Connection, plan_id: PlanId) -> _Aggregates:
    def one(sql: str, *args: Any) -> int:
        return connection.execute(sql, args).fetchone()[0]

    install = connection.execute("SELECT store_hold, restore_cutoff FROM installation WHERE id = 1").fetchone()
    epoch = _installation_epoch(connection)
    unresolved_reason = one(
        "SELECT EXISTS(SELECT 1 FROM store_hold_reasons WHERE epoch = ? AND resolved = 0)", epoch)
    counts = {
        "attempts": one("SELECT COUNT(*) FROM attempts WHERE plan_id = ?", plan_id),
        "reservations": one("SELECT COUNT(*) FROM resource_uses WHERE plan_id = ?", plan_id),
        "holds": one("SELECT COUNT(*) FROM holds WHERE plan_id = ? AND active = 1", plan_id),
        "reviews": one("SELECT COUNT(*) FROM reviews WHERE plan_id = ?", plan_id),
        "invalidations": one(
            "SELECT COUNT(*) FROM events WHERE plan_id = ? AND kind = 'invalidate_result'", plan_id),
        "orphan_uses": one(
            "SELECT COUNT(*) FROM resource_uses WHERE plan_id IS NULL AND status != 'released'"),
    }
    return _Aggregates(
        has_live_attempts=bool(one(
            f"SELECT EXISTS(SELECT 1 FROM attempts WHERE plan_id = ? AND state IN ({_LIVE_SQL}))", plan_id)),
        has_stale_attempt=bool(one(
            "SELECT EXISTS(SELECT 1 FROM attempts WHERE plan_id = ? AND state = 'stale')", plan_id)),
        has_unresolved_uses=bool(one(
            "SELECT EXISTS(SELECT 1 FROM resource_uses WHERE plan_id = ? AND status = 'unresolved')", plan_id)),
        has_plan_holds=bool(counts["holds"]),
        has_global_hold=bool(one(
            f"SELECT EXISTS(SELECT 1 FROM holds WHERE plan_id = ? AND active = 1 AND {_GLOBAL_HOLD_SQL})",
            plan_id)),
        store_hold=bool(install[0]) or bool(unresolved_reason),
        restore_cutoff=install[1],
        counts=counts,
    )


def _derive_status(plan: _Plan, agg: _Aggregates) -> str:
    draft = plan.draft_json
    states = [s.state for s in plan.steps]
    if draft.get("stop_reason"):
        return "stopped"
    if draft.get("block_reason") or agg.store_hold or agg.has_global_hold or agg.has_stale_attempt:
        return "blocked"
    if states and all(state in ("complete", "retired") for state in states):
        return "complete"
    if "in-progress" in states or agg.has_live_attempts:
        return "running"
    return "planning"


def _next_step_id(plan: _Plan) -> str | None:
    states = {s.step_id: s.state for s in plan.steps}
    for step in plan.steps:
        if step.state in ("pending", "in-progress") and all(
                states.get(dep) == "complete" for dep in step.definition["dependencies"]):
            return step.step_id
    return None


# --------------------------------------------------------------------------------------
# Legacy snapshot projection
# --------------------------------------------------------------------------------------
def _history(connection: sqlite3.Connection, plan: _Plan) -> list[dict[str, Any]]:
    first = max(1, plan.revision - 99)
    by_revision: dict[int, dict[str, Any]] = {}
    for revision, published_at, kind, payload in connection.execute(
            "SELECT plan_revision, published_at, kind, payload FROM events "
            "WHERE plan_id = ? AND plan_revision BETWEEN ? AND ? ORDER BY sequence",
            (plan.plan_id, first, plan.revision)):
        data = json.loads(payload)
        entry = by_revision.setdefault(revision, {"revision": revision})
        entry["timestamp"] = published_at
        entry["summary"] = data.get("summary") or kind
        if data.get("authorization_note"):
            entry["authorizationNote"] = data["authorization_note"]
    history = []
    for revision in range(first, plan.revision + 1):
        history.append(by_revision.get(revision) or {
            "revision": revision, "timestamp": plan.updated_at, "summary": f"Revision {revision}"})
    return history


def _snapshot(connection: sqlite3.Connection, plan: _Plan, status: str) -> dict[str, Any]:
    auth = plan.authorization
    history = _history(connection, plan)
    steps = []
    for s in plan.steps:
        node: dict[str, Any] = {
            "id": s.step_id, "title": s.definition["title"], "status": s.state,
            "dependsOn": list(s.definition["dependencies"]), "check": s.definition["completion_check"],
            "evidence": list(s.evidence)}
        if s.state == "retired":
            node["retiredReason"] = s.retired_reason
        steps.append(node)
    return {
        "schemaVersion": 2, "generation": plan.generation, "revision": plan.revision,
        "updatedAt": plan.updated_at, "taskId": plan.plan_id, "objective": plan.draft_json["objective"],
        "status": status, "executionMode": auth.mode,
        "scope": {"mode": auth.mode, "sinceRevision": auth.since_revision,
                  "baseline": list(auth.baseline), "claimed": auth.claimed},
        "nextStepId": _next_step_id(plan), "changeSummary": history[-1]["summary"],
        "steps": steps, "history": history,
    }


def _publication(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    try:
        validate_published_snapshot(snapshot)
    except PlanValidationError:
        return None
    return copy.deepcopy(snapshot)


# --------------------------------------------------------------------------------------
# Coordination read model
# --------------------------------------------------------------------------------------
_PAGE_SQL = f"""
SELECT prio, kind_rank, rid, kind FROM (
    SELECT CASE WHEN state IN ({_LIVE_SQL}) THEN 0 ELSE 1 END AS prio, 0 AS kind_rank, rowid AS rid,
           'attempt' AS kind FROM attempts WHERE plan_id = :p
    UNION ALL
    SELECT CASE WHEN status != 'released' THEN 0 ELSE 1 END, 1, rowid, 'reservation'
      FROM resource_uses WHERE plan_id = :p OR (plan_id IS NULL AND status != 'released')
    UNION ALL
    SELECT 0, 2, rowid, 'hold' FROM holds WHERE plan_id = :p AND active = 1
    UNION ALL
    SELECT 1, 3, rowid, 'review' FROM reviews WHERE plan_id = :p
    UNION ALL
    SELECT CASE WHEN (SELECT COUNT(DISTINCT json_extract(r.payload, '$.affected_step_id')) FROM events r
                      WHERE r.plan_id = e.plan_id AND r.kind = 'resolve_invalidation'
                        AND json_extract(r.payload, '$.invalidation_id')
                            = json_extract(e.payload, '$.invalidation_id'))
                     < json_array_length(json_extract(e.payload, '$.affected_steps'))
                THEN 0 ELSE 1 END, 4, e.sequence, 'invalidation'
      FROM events e WHERE e.plan_id = :p AND e.kind = 'invalidate_result'
)
WHERE (prio, kind_rank, rid) > (:a, :b, :c)
ORDER BY prio, kind_rank, rid
LIMIT :n
"""


def _attempt_view(row: Any) -> AttemptView:
    attempt_id, step_id, assignee, state, version, definition_version, started_at, payload = row
    data = json.loads(payload)
    context = data.get("context")
    return AttemptView(
        attempt_id=attempt_id, step_id=step_id, assignee=assignee, state=state,
        prior_state=data.get("prior_state"), version=version, definition_version=definition_version,
        last_activity=data.get("last_activity") or started_at or "",
        context=None if context is None else _to_execution_context(context),
        recovery_required=bool(data.get("recovery_required", False)),
        result_version=data.get("result_version"))


def _hold_view(hold_id: str, reason: str) -> HoldView:
    try:
        data = json.loads(reason)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("kind"), str):
        return HoldView(hold_id=hold_id, kind=data["kind"], step_id=data.get("step_id"),
                        source_id=str(data.get("source_id", hold_id)), reason=str(data.get("reason", "")))
    return HoldView(hold_id=hold_id, kind="identity", step_id=None, source_id=hold_id, reason=reason)


def _load_item(connection: sqlite3.Connection, plan_id: PlanId, kind: str, rid: int) -> CoordinationItem:
    if kind == "attempt":
        row = connection.execute(
            "SELECT attempt_id, step_id, assignee, state, version, definition_version, started_at, payload "
            "FROM attempts WHERE rowid = ?", (rid,)).fetchone()
        return CoordinationItem("attempt", _attempt_view(row))
    if kind == "reservation":
        use_id, use_plan, attempt_id, absent_id, resource_id, scope, status, assignee, identity = connection.execute(
            "SELECT u.use_id, u.plan_id, u.attempt_id, u.absent_work_id, u.resource_id, u.scope, u.status, "
            "a.assignee, h.declared_identity FROM resource_uses u "
            "LEFT JOIN attempts a ON a.plan_id = u.plan_id AND a.attempt_id = u.attempt_id "
            "LEFT JOIN absent_host_work h ON h.absent_work_id = u.absent_work_id WHERE u.rowid = ?",
            (rid,)).fetchone()
        return CoordinationItem("reservation", ReservationView(
            use_id=use_id, plan_id=use_plan, attempt_id=attempt_id, absent_work_id=absent_id,
            resource_ids=(resource_id,), actor=assignee or identity or "", writer_active=status != "released",
            unresolved=status == "unresolved", repository_exclusive=scope == "repository"))
    if kind == "hold":
        hold_id, reason = connection.execute("SELECT hold_id, reason FROM holds WHERE rowid = ?", (rid,)).fetchone()
        return CoordinationItem("hold", _hold_view(hold_id, reason))
    if kind == "review":
        (step_id, attempt_id, reviewer, verdict, findings, evidence, artifact, use_id,
         definition_version) = connection.execute(
            "SELECT r.step_id, r.attempt_id, r.reviewer, r.verdict, r.findings, r.evidence, r.artifact, "
            "r.verification_use_id, a.definition_version FROM reviews r "
            "LEFT JOIN attempts a ON a.plan_id = r.plan_id AND a.attempt_id = r.attempt_id "
            "WHERE r.rowid = ?", (rid,)).fetchone()
        review = Review(
            reviewer=reviewer, artifact=_to_artifact_ref(json.loads(artifact)), verdict=verdict,
            findings=tuple(json.loads(findings)),
            evidence=tuple(_to_evidence(e) for e in json.loads(evidence)), verification_use_id=use_id)
        return CoordinationItem("review", ReviewView(
            plan_id=plan_id, step_id=step_id, attempt_id=attempt_id, definition_version=definition_version or "",
            recorded_sequence=rid, review=review))
    payload = json.loads(connection.execute("SELECT payload FROM events WHERE sequence = ?", (rid,)).fetchone()[0])
    invalidation_id = payload.get("invalidation_id", str(rid))
    resolved = []
    for (affected,) in connection.execute(
            "SELECT json_extract(payload, '$.affected_step_id') FROM events WHERE plan_id = ? "
            "AND kind = 'resolve_invalidation' AND json_extract(payload, '$.invalidation_id') = ? "
            "ORDER BY sequence", (plan_id, invalidation_id)):
        if affected is not None and affected not in resolved:
            resolved.append(affected)
    return CoordinationItem("invalidation", InvalidationView(
        invalidation_id=invalidation_id, producer_step_id=payload.get("producer_step_id", payload.get("step_id", "")),
        result_version=payload.get("result_version", ""), affected_steps=tuple(payload.get("affected_steps", ())),
        resolved_steps=tuple(resolved), recorded_sequence=rid))


def encode_cursor(epoch: str, plan_id: str, revision: int, sequence: int, key: tuple[int, int, int]) -> str:
    raw = json.dumps({"e": epoch, "p": plan_id, "r": revision, "s": sequence, "k": list(key)},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise DomainError("invalid", "Malformed coordination cursor") from None
    key = data.get("k") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != {"e", "p", "r", "s", "k"} or not isinstance(key, list)
            or len(key) != 3 or not all(type(v) is int for v in key + [data["r"], data["s"]])
            or not isinstance(data["e"], str) or not isinstance(data["p"], str)):
        raise DomainError("invalid", "Malformed coordination cursor")
    return data


def _coordination_page(
    connection: sqlite3.Connection, plan: _Plan, key: tuple[int, int, int], limit: int, budget: int,
    *, strict: bool,
) -> tuple[list[CoordinationItem], str | None]:
    """Collect priority-ordered rows after `key` within the row and byte budget; rows are never truncated."""
    rows = connection.execute(_PAGE_SQL, {
        "p": plan.plan_id, "a": key[0], "b": key[1], "c": key[2], "n": limit + 1}).fetchall()
    items: list[CoordinationItem] = []
    used = 0
    last = key
    stopped = len(rows) > limit
    for prio, kind_rank, rid, kind in rows[:limit]:
        item = _load_item(connection, plan.plan_id, kind, rid)
        size = len(canonical_bytes(item))
        if strict and size > MAX_PAGE_BYTES:
            raise DomainError("limit_exceeded", "A coordination row exceeds the 10MB page limit")
        if used + size > budget:
            stopped = True
            break
        items.append(item)
        used += size
        last = (prio, kind_rank, rid)
    if not stopped:
        return items, None
    epoch, sequence = _installation_epoch(connection), _store_sequence(connection)
    return items, encode_cursor(epoch, plan.plan_id, plan.revision, sequence, last)


def _coordination_view(
    connection: sqlite3.Connection, plan: _Plan, agg: _Aggregates, items: list[CoordinationItem],
    next_cursor: str | None,
) -> CoordinationView:
    result_versions: dict[str, str] = {}
    for step_id, result_version in connection.execute(
            "SELECT step_id, result_version FROM result_versions WHERE plan_id = ? ORDER BY rowid", (plan.plan_id,)):
        result_versions[step_id] = result_version
    actor, version = current_ownership(connection, plan.plan_id)
    return CoordinationView(
        attempts=tuple(i.value for i in items if i.kind == "attempt"),
        reservations=tuple(i.value for i in items if i.kind == "reservation"),
        holds=tuple(i.value for i in items if i.kind == "hold"),
        reviews=tuple(i.value for i in items if i.kind == "review"),
        result_versions=result_versions, store_hold=agg.store_hold, restore_cutoff=agg.restore_cutoff,
        ownership_actor=actor, ownership_version=version, store_sequence=_store_sequence(connection),
        next_cursor=next_cursor, total_counts=dict(agg.counts), has_live_attempts=agg.has_live_attempts,
        has_unresolved_uses=agg.has_unresolved_uses, has_plan_holds=agg.has_plan_holds)


def read_coordination_page(
    connection: sqlite3.Connection, plan_id: PlanId, after: str | None, limit: int,
) -> CoordinationPage:
    if type(limit) is not int or not 1 <= limit <= COORDINATION_PAGE_MAX:
        raise DomainError("invalid", f"limit must be an integer between 1 and {COORDINATION_PAGE_MAX}")
    cursor = decode_cursor(after) if after is not None else None
    plan = _load_plan(connection, plan_id)
    epoch, sequence = _installation_epoch(connection), _store_sequence(connection)
    key = _START_KEY
    if cursor is not None:
        if (cursor["e"], cursor["p"], cursor["r"], cursor["s"]) != (epoch, plan_id, plan.revision, sequence):
            raise DomainError("conflict", "Coordination cursor no longer matches the plan; restart from read_plan",
                              _metadata(connection, plan_id))
        key = tuple(cursor["k"])  # type: ignore[assignment]
    agg = _aggregates(connection, plan_id)
    items, next_cursor = _coordination_page(connection, plan, key, limit, MAX_PAGE_BYTES, strict=True)
    return CoordinationPage(plan_id=plan_id, revision=plan.revision, store_sequence=sequence,
                            items=tuple(items), next_cursor=next_cursor, total_counts=dict(agg.counts))


# --------------------------------------------------------------------------------------
# Projection and reads
# --------------------------------------------------------------------------------------
def _project(connection: sqlite3.Connection, plan_id: PlanId) -> PlanView:
    plan = _load_plan(connection, plan_id)
    agg = _aggregates(connection, plan_id)
    status = _derive_status(plan, agg)
    snapshot = _snapshot(connection, plan, status)
    graph_bytes = len(canonical_bytes(snapshot)) + len(canonical_bytes(plan.draft_json))
    items, next_cursor = _coordination_page(
        connection, plan, _START_KEY, COORDINATION_SUMMARY_LIMIT, max(0, MAX_PAGE_BYTES - graph_bytes),
        strict=False)
    return PlanView(
        plan_id=plan_id, metadata=_metadata(connection, plan_id), draft=plan.draft(), legacy_snapshot=snapshot,
        legacy_publication=_publication(snapshot),
        coordination=_coordination_view(connection, plan, agg, items, next_cursor),
        status=status, published_at=snapshot["updatedAt"])


def project_plan(connection: sqlite3.Connection, plan_id: PlanId) -> PlanView:
    return _project(connection, plan_id)


def list_plans(connection: sqlite3.Connection, include_terminal: bool = False) -> tuple[PlanView, ...]:
    views = []
    for (plan_id,) in connection.execute("SELECT plan_id FROM plans ORDER BY created_at, rowid").fetchall():
        view = project_plan(connection, plan_id)
        if include_terminal or view.status != "complete":
            views.append(view)
    return tuple(views)


def read_events(connection: sqlite3.Connection, plan_id: PlanId, after: int, limit: int) -> tuple[Event, ...]:
    if type(after) is not int or after < 0:
        raise DomainError("invalid", "after must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= EVENTS_LIMIT_MAX:
        raise DomainError("invalid", f"limit must be an integer between 1 and {EVENTS_LIMIT_MAX}")
    if connection.execute("SELECT 1 FROM plans WHERE plan_id = ?", (plan_id,)).fetchone() is None:
        raise DomainError("not_found", f"Plan {plan_id!r} does not exist")
    rows = connection.execute(
        "SELECT sequence, plan_revision, plan_id, kind, actor, origin_worker, relay, published_at, payload "
        "FROM events WHERE plan_id = ? AND sequence > ? ORDER BY sequence LIMIT ?", (plan_id, after, limit))
    return tuple(Event(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], json.loads(r[8])) for r in rows)


def list_ready(connection: sqlite3.Connection, plan_id: PlanId) -> tuple[ReadyStep, ...]:
    plan = _load_plan(connection, plan_id)
    agg = _aggregates(connection, plan_id)
    status = _derive_status(plan, agg)
    plan_reasons: list[str] = []
    if status == "stopped":
        plan_reasons.append(f"plan is stopped: {plan.draft_json['stop_reason']}")
    elif status == "complete":
        plan_reasons.append("plan is complete")
    elif status == "blocked":
        if plan.draft_json.get("block_reason"):
            plan_reasons.append(f"plan is blocked: {plan.draft_json['block_reason']}")
        if agg.store_hold:
            plan_reasons.append("store hold is active")
        if agg.has_global_hold:
            plan_reasons.append("a plan-level hold is active")
        if agg.has_stale_attempt:
            plan_reasons.append("a stale attempt requires recovery")
    step_holds: dict[str, list[str]] = {}
    for hold_id, reason in connection.execute(
            "SELECT hold_id, reason FROM holds WHERE plan_id = ? AND active = 1 ORDER BY rowid", (plan_id,)):
        hold = _hold_view(hold_id, reason)
        if hold.step_id is not None:
            step_holds.setdefault(hold.step_id, []).append(f"hold {hold.hold_id} ({hold.kind}): {hold.reason}")
    live_steps = {row[0] for row in connection.execute(
        f"SELECT step_id FROM attempts WHERE plan_id = ? AND state IN ({_LIVE_SQL})", (plan_id,))}
    states = {s.step_id: s.state for s in plan.steps}
    ready = []
    for step in plan.steps:
        if step.state in ("complete", "retired"):
            continue
        reasons = list(plan_reasons)
        if step.state != "pending":
            reasons.append(f"step is {step.state}")
        for dep in step.definition["dependencies"]:
            if states.get(dep) != "complete":
                reasons.append(f"prerequisite {dep!r} is not complete")
        reasons.extend(step_holds.get(step.step_id, ()))
        if step.step_id in live_steps:
            reasons.append("step already has a live attempt")
        try:
            validate_execution_scope(plan.authorization, step.step_id)
        except PlanValidationError as error:
            reasons.append(str(error))
        ready.append(ReadyStep(step.step_id, not reasons, tuple(reasons)))
    return tuple(ready)


def format_evidence(evidence: Evidence) -> str:
    """Deterministic public evidence string; never includes workspace identity."""
    parts = [evidence.summary, f"check: {evidence.check}", f"result: {evidence.result}"]
    artifact = evidence.artifact
    if artifact is not None:
        text = f"artifact: {artifact.identity} version {artifact.version}"
        if artifact.digest:
            text += f" digest {artifact.digest}"
        parts.append(f"{text} ({artifact.kind}: {artifact.summary})")
    if evidence.limitations:
        parts.append("limitations: " + "; ".join(evidence.limitations))
    if evidence.blockers:
        parts.append("blockers: " + "; ".join(evidence.blockers))
    return " | ".join(parts)


# --------------------------------------------------------------------------------------
# Write helpers
# --------------------------------------------------------------------------------------
def _timestamp(previous: str | None = None) -> str:
    now = _now()
    if previous is not None:
        now = max(now, datetime.fromisoformat(previous))
    return now.isoformat()


def _insert_event(
    connection: sqlite3.Connection, plan_id: PlanId, revision: int, kind: str, actor: str, ts: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO events (plan_id, plan_revision, kind, actor, published_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, revision, kind, actor, ts, json.dumps(payload, sort_keys=True, ensure_ascii=False)))


def _validation_coordination(connection: sqlite3.Connection, plan_id: PlanId) -> CoordinationView:
    attempts = tuple(_attempt_view(row) for row in connection.execute(
        "SELECT attempt_id, step_id, assignee, state, version, definition_version, started_at, payload "
        f"FROM attempts WHERE plan_id = ? AND state IN ({_LIVE_SQL})", (plan_id,)))
    holds = tuple(_hold_view(h, r) for h, r in connection.execute(
        "SELECT hold_id, reason FROM holds WHERE plan_id = ? AND active = 1", (plan_id,)))
    agg = _aggregates(connection, plan_id)
    actor, version = current_ownership(connection, plan_id)
    return CoordinationView(
        attempts=attempts, reservations=(), holds=holds, reviews=(), result_versions={},
        store_hold=agg.store_hold, restore_cutoff=agg.restore_cutoff, ownership_actor=actor,
        ownership_version=version, store_sequence=_store_sequence(connection), next_cursor=None,
        total_counts=dict(agg.counts), has_live_attempts=agg.has_live_attempts,
        has_unresolved_uses=agg.has_unresolved_uses, has_plan_holds=agg.has_plan_holds)


def _proposal(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("schemaVersion", "taskId", "objective", "status", "executionMode", "nextStepId", "changeSummary",
            "steps")
    return {key: copy.deepcopy(snapshot[key]) for key in keys}


def _commit_projection(connection: sqlite3.Connection, plan_id: PlanId, previous: PlanView | None) -> PlanView:
    """Project the written state, enforce shared graph rules and size limits, then persist derived status."""
    view = project_plan(connection, plan_id)
    if len(canonical_bytes(view.legacy_snapshot)) > MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Plan projection exceeds the 10MB limit")
    try:
        validate_service_graph(
            _proposal(view.legacy_snapshot), None if previous is None else _proposal(previous.legacy_snapshot),
            _validation_coordination(connection, plan_id))
    except PlanValidationError as error:
        raise DomainError("invalid", str(error)) from error
    connection.execute("UPDATE plans SET status = ? WHERE plan_id = ?", (view.status, plan_id))
    return view


def _check_draft_limits(draft: PlanDraft) -> None:
    if len(draft.steps) > MAX_STEPS:
        raise DomainError("limit_exceeded", f"A plan may have at most {MAX_STEPS} steps")
    if len(canonical_bytes(draft)) > MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Plan content exceeds the 10MB limit")
    for name in ("stop_reason", "block_reason"):
        value = getattr(draft, name)
        if value is not None and (not value.strip() or len(value) > MAX_TEXT_LEN):
            raise DomainError("invalid", f"{name} must be a bounded non-empty string or null")
    if len({s.step_id for s in draft.steps}) != len(draft.steps):
        raise DomainError("invalid", "Duplicate step IDs found in steps")


def _draft_json(draft: PlanDraft, authorization: Authorization) -> dict[str, Any]:
    data = to_jsonable(dataclasses.replace(draft, authorization=authorization, steps=()))
    del data["steps"]
    return data


# --------------------------------------------------------------------------------------
# create_plan
# --------------------------------------------------------------------------------------
def _creation_replay(
    connection: sqlite3.Connection, plan_id: PlanId, creation_id: str,
) -> MutationReceipt:
    row = connection.execute(
        "SELECT response_payload FROM requests WHERE plan_id = ? ORDER BY rowid LIMIT 1", (plan_id,)).fetchone()
    current = _metadata(connection, plan_id)
    if row is None:
        return MutationReceipt({"plan_id": plan_id, "creation_id": creation_id}, current, current, True, None)
    committed, committed_metadata, private = decode_response(row[0])
    return MutationReceipt(committed, committed_metadata, current, True, private)


def _duplicate_candidates(connection: sqlite3.Connection, draft: PlanDraft) -> list[str]:
    objective = draft.objective.strip()
    refs = set(draft.workspace_refs)
    found = []
    for (plan_id,) in connection.execute("SELECT plan_id FROM plans ORDER BY created_at, rowid").fetchall():
        plan = _load_plan(connection, plan_id)
        if plan.draft_json["objective"].strip() != objective:
            continue
        existing = set(plan.draft_json["workspace_refs"])
        if (existing & refs or (not existing and not refs)) and _derive_status(
                plan, _aggregates(connection, plan_id)) != "complete":
            found.append(plan_id)
    return found


def create_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    payload = command.payload
    draft: PlanDraft = payload["draft"]
    creation_id: str = payload["creation_id"]
    intent: str | None = payload["new_plan_intent"]
    epoch = _installation_epoch(connection)
    content_digest = sha256_hex(canonical_bytes(draft))

    existing = connection.execute(
        "SELECT plan_id, content_digest FROM creation_ids WHERE epoch = ? AND creation_id = ?",
        (epoch, creation_id)).fetchone()
    if existing is not None:
        if existing[1] != content_digest:
            raise DomainError("conflict", f"creation_id {creation_id!r} was already used with different content")
        return _creation_replay(connection, existing[0], creation_id)

    auth = draft.authorization
    if auth.baseline or auth.claimed is not None or auth.interval != "" or auth.since_revision != 0:
        raise DomainError("invalid", "A new plan must carry an empty baseline, null claimed, blank interval "
                                     "and since_revision 0; the server supplies them")
    if not auth.note.strip() or len(auth.note) > MAX_TEXT_LEN:
        raise DomainError("invalid", "authorization note must be a bounded non-empty string")
    if not draft.objective.strip() or len(draft.objective) > MAX_TEXT_LEN:
        raise DomainError("invalid", "objective must be a bounded non-empty string")
    _check_draft_limits(draft)

    candidates = _duplicate_candidates(connection, draft)
    if candidates and not (intent and intent.strip()):
        raise DomainError("duplicate_candidate", "Possible duplicate of existing plan(s): " + ", ".join(candidates)
                          + "; supply new_plan_intent to create a separate plan")

    plan_id, generation, ts = str(uuid.uuid4()), str(uuid.uuid4()), _timestamp()
    authorization = Authorization(
        mode=auth.mode, interval=str(uuid.uuid4()), since_revision=1, note=auth.note.strip(), baseline=(),
        claimed=None)
    connection.execute(
        "INSERT INTO plans (plan_id, generation, revision, status, title, ownership_version, created_at, "
        "updated_at, payload) VALUES (?, ?, 1, 'planning', ?, 1, ?, ?, ?)",
        (plan_id, generation, draft.objective, ts, ts,
         json.dumps({"draft": _draft_json(draft, authorization)}, sort_keys=True, ensure_ascii=False)))
    for ordinal, definition in enumerate(draft.steps):
        step = _Step(definition.step_id, "pending", to_jsonable(definition), [], None, ordinal)
        connection.execute(
            "INSERT INTO steps (plan_id, step_id, title, state, definition_version, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, step.step_id, definition.title, step.state, definition.definition_version,
             _step_payload(step)))
    grant = issue_orchestrator(connection, plan_id, command.actor)
    _insert_event(connection, plan_id, 1, "create_plan", command.actor, ts, {
        "summary": "Plan created", "authorization_renewed": True, "retired_steps": [],
        "authorization_note": authorization.note, "new_plan_intent": intent})
    connection.execute(
        "INSERT INTO creation_ids (epoch, creation_id, plan_id, content_digest, created_at) VALUES (?, ?, ?, ?, ?)",
        (epoch, creation_id, plan_id, content_digest, ts))
    view = _commit_projection(connection, plan_id, None)
    committed = {"plan_id": plan_id, "generation": generation, "revision": 1, "creation_id": creation_id}
    return MutationReceipt(committed, view.metadata, view.metadata, False, grant)


# --------------------------------------------------------------------------------------
# revise_plan
# --------------------------------------------------------------------------------------
def retire_step(
    connection: sqlite3.Connection, plan_id: PlanId, request: RetirementRequest, actor: str,
) -> None:
    """Explicitly retire a pending or blocked step that has no live attempt, retaining its node and reason."""
    reason = request.reason.strip()
    if not reason or len(reason) > MAX_TEXT_LEN:
        raise DomainError("invalid", f"Retirement of {request.step_id!r} requires a bounded non-empty reason")
    row = connection.execute(
        "SELECT state, payload FROM steps WHERE plan_id = ? AND step_id = ?", (plan_id, request.step_id)).fetchone()
    if row is None:
        raise DomainError("invalid", f"Cannot retire unknown step {request.step_id!r}")
    if row[0] not in ("pending", "blocked"):
        raise DomainError("invalid", f"Step {request.step_id!r} is {row[0]}; only pending or blocked steps retire")
    if connection.execute(
            f"SELECT 1 FROM attempts WHERE plan_id = ? AND step_id = ? AND state IN ({_LIVE_SQL})",
            (plan_id, request.step_id)).fetchone() is not None:
        raise DomainError("invalid", f"Step {request.step_id!r} has a live attempt and cannot be retired")
    data = json.loads(row[1])
    step = _Step(request.step_id, "retired", data["definition"], list(data.get("evidence", [])), reason,
                 data.get("ordinal", 0))
    connection.execute("UPDATE steps SET state = 'retired', payload = ? WHERE plan_id = ? AND step_id = ?",
                       (_step_payload(step, actor), plan_id, request.step_id))


def _check_preconditions(plan: _Plan, command: Command) -> None:
    if command.generation is None:
        raise DomainError("invalid", "generation is required")
    if command.generation != plan.generation:
        raise DomainError("stale_generation", "The plan generation has changed; re-read the plan")
    if command.expected_revision is None:
        raise DomainError("invalid", "expected_revision is required")
    if command.expected_revision != plan.revision:
        raise DomainError("conflict", f"Plan is at revision {plan.revision}, not {command.expected_revision}")
    if command.ownership_version is not None and command.ownership_version != plan.ownership_version:
        raise DomainError("conflict", "Plan ownership has changed; re-read the plan")


def _apply_steps(connection: sqlite3.Connection, plan: _Plan, draft: PlanDraft) -> None:
    existing = {s.step_id: s for s in plan.steps}
    wanted = {d.step_id for d in draft.steps}
    for ordinal, definition in enumerate(draft.steps):
        data = to_jsonable(definition)
        current = existing.get(definition.step_id)
        if current is None:
            step = _Step(definition.step_id, "pending", data, [], None, ordinal)
            connection.execute(
                "INSERT INTO steps (plan_id, step_id, title, state, definition_version, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (plan.plan_id, step.step_id, definition.title, step.state, definition.definition_version,
                 _step_payload(step)))
            continue
        if current.state in ("complete", "retired") and current.definition != data:
            raise DomainError("invalid", f"{current.state.capitalize()} step {current.step_id!r} cannot be altered")
        current.definition, current.ordinal = data, ordinal
        connection.execute(
            "UPDATE steps SET title = ?, definition_version = ?, payload = ? WHERE plan_id = ? AND step_id = ?",
            (definition.title, definition.definition_version, _step_payload(current), plan.plan_id,
             current.step_id))
    for step in plan.steps:
        if step.step_id in wanted:
            continue
        has_history = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM attempts WHERE plan_id = ?1 AND step_id = ?2) "
            "OR EXISTS(SELECT 1 FROM result_versions WHERE plan_id = ?1 AND step_id = ?2)",
            (plan.plan_id, step.step_id)).fetchone()[0]
        if step.state not in ("pending", "blocked") or step.evidence or has_history:
            raise DomainError("invalid", f"Step {step.step_id!r} cannot be removed; retire it instead")
        connection.execute("DELETE FROM steps WHERE plan_id = ? AND step_id = ?", (plan.plan_id, step.step_id))


def revise_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    payload = command.payload
    draft: PlanDraft = payload["draft"]
    reason: str = payload["reason"].strip()
    change = payload["authorization_change"]
    retirements: tuple[RetirementRequest, ...] = payload["retire_steps"]

    plan = _load_plan(connection, command.plan_id)
    _check_preconditions(plan, command)
    _check_draft_limits(draft)
    if len(reason) > MAX_TEXT_LEN:
        raise DomainError("invalid", "reason exceeds maximum length")
    if draft.authorization != plan.authorization:
        raise DomainError("invalid", "The draft must echo the stored authorization unchanged; "
                                     "use authorization_change to renew it")
    if draft.objective != plan.draft_json["objective"]:
        raise DomainError("invalid", "The plan objective cannot change")
    retired_ids = [r.step_id for r in retirements]
    if len(retired_ids) != len(set(retired_ids)):
        raise DomainError("invalid", "Duplicate retirement requests")

    previous = _project(connection, plan.plan_id)
    revision = plan.revision + 1
    ts = _timestamp(plan.updated_at)

    _apply_steps(connection, plan, draft)
    for request in retirements:
        retire_step(connection, plan.plan_id, request, command.actor)

    authorization = plan.authorization
    if change is not None:
        steps = {s.step_id: s.state for s in _load_plan(connection, plan.plan_id).steps}
        completed = tuple(s for s, state in steps.items() if state == "complete")
        live = {row[0] for row in connection.execute(
            f"SELECT step_id FROM attempts WHERE plan_id = ? AND state IN ({_LIVE_SQL})", (plan.plan_id,))}
        claimed = tuple(s for s, state in steps.items() if (s in live or state == "in-progress")
                        and state != "complete")
        try:
            authorization = renew_authorization(
                plan.authorization, change, completed, claimed, revision=revision, interval=str(uuid.uuid4()))
        except PlanValidationError as error:
            raise DomainError("invalid", str(error)) from error

    connection.execute(
        "UPDATE plans SET revision = ?, updated_at = ?, payload = ? WHERE plan_id = ?",
        (revision, ts, json.dumps({"draft": _draft_json(draft, authorization)}, sort_keys=True, ensure_ascii=False),
         plan.plan_id))
    event: dict[str, Any] = {
        "summary": reason, "authorization_renewed": change is not None,
        "retired_steps": [{"step_id": r.step_id, "reason": r.reason.strip()} for r in retirements]}
    if change is not None:
        event["authorization_note"] = authorization.note
    _insert_event(connection, plan.plan_id, revision, "revise_plan", command.actor, ts, event)

    view = _commit_projection(connection, plan.plan_id, previous)
    committed = {"plan_id": plan.plan_id, "revision": revision,
                 "authorization_renewed": change is not None, "retired_steps": retired_ids}
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)
