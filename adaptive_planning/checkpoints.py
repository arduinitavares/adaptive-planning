"""Attributed safe-boundary checkpoints and provisional reassessment holds."""
from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
import uuid

from adaptive_planning import attempts, plans
from adaptive_planning.core import MAX_EVIDENCE_ITEMS, MAX_FILE_BYTES, MAX_TEXT_LEN
from adaptive_planning.types import (
    Checkpoint, Command, DomainError, JSONValue, MutationReceipt, PlanId, Reassessment, StepId,
    _to_checkpoint, _to_execution_context, _to_reassessment,
)


def affected_closure(snapshot: dict[str, JSONValue], steps: tuple[StepId, ...]) -> tuple[StepId, ...]:
    """Include identified steps and their transitive consumers in authoritative graph order."""
    nodes = snapshot.get("steps") if isinstance(snapshot, dict) else None
    if not isinstance(nodes, list) or not isinstance(steps, tuple):
        raise DomainError("invalid", "Reassessment requires a valid graph and identified step scope")
    dependencies = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise DomainError("invalid", "Reassessment graph contains a malformed step")
        sid, deps = node.get("id"), node.get("dependsOn")
        if (not attempts._nonblank(sid) or sid in dependencies or not isinstance(deps, list)
                or any(not attempts._nonblank(dep) for dep in deps) or len(set(deps)) != len(deps)):
            raise DomainError("invalid", "Reassessment graph contains a malformed step or dependencies")
        dependencies[sid] = tuple(deps)
    if (any(not attempts._nonblank(sid) or sid not in dependencies for sid in steps)
            or len(set(steps)) != len(steps)
            or any(dep not in dependencies for deps in dependencies.values() for dep in deps)):
        raise DomainError("invalid", "Reassessment scope must identify distinct known steps")
    # A malformed cycle cannot become an apparently valid consumer closure.
    visited = set()
    remaining = set(dependencies)
    while remaining:
        ready = {sid for sid in remaining if set(dependencies[sid]) <= visited}
        if not ready:
            raise DomainError("invalid", "Reassessment graph contains a dependency cycle")
        visited.update(ready)
        remaining.difference_update(ready)
    affected = set(steps)
    while True:
        consumers = {sid for sid, deps in dependencies.items() if affected.intersection(deps)}
        if consumers <= affected:
            return tuple(sid for sid in dependencies if sid in affected)
        affected.update(consumers)


def _text(value: str, description: str) -> None:
    if not attempts._nonblank(value):
        raise DomainError("invalid", f"{description} must contain essential text")
    if len(value) > MAX_TEXT_LEN:
        raise DomainError("limit_exceeded", f"{description} exceeds the shared text limit")


def _validate_reassessment(snapshot: dict[str, JSONValue], value: Reassessment) -> tuple[StepId, ...]:
    _text(value.summary, "Reassessment summary")
    _text(value.reason, "Reassessment reason")
    for item in (*value.facts, *value.assumptions):
        _text(item, "Reassessment finding")
    closure = affected_closure(snapshot, value.affected_steps)
    if value.disposition == "unchanged":
        if value.affected_steps or value.unbounded:
            raise DomainError("invalid", "Unchanged reassessment cannot declare affected or unbounded effects")
    elif not value.unbounded and not closure:
        raise DomainError("invalid", "Material reassessment needs identified effects or an unbounded scope")
    return closure


def _validate_checkpoint(value: Checkpoint, snapshot: dict[str, JSONValue], prior_evidence: list[str]) -> list[str]:
    _text(value.check, "Checkpoint check")
    _text(value.origin_worker, "Checkpoint origin worker")
    if value.relay is not None:
        _text(value.relay, "Checkpoint relay")
    if len(value.evidence) > MAX_EVIDENCE_ITEMS or len(prior_evidence) + len(value.evidence) > MAX_EVIDENCE_ITEMS:
        raise DomainError("limit_exceeded", "Checkpoint publication exceeds the 50-item evidence limit")
    if len(plans.canonical_bytes(value)) > MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Checkpoint content exceeds the 10MB limit")
    _validate_reassessment(snapshot, value.reassessment)
    if value.reassessment.proposed_draft is not None:
        plans._check_draft_limits(value.reassessment.proposed_draft)
    artifacts = [value.artifact, *(item.artifact for item in value.evidence)]
    for artifact in artifacts:
        if artifact is not None:
            for name in ("identity", "version", "summary"):
                _text(getattr(artifact, name), "Checkpoint artifact reference")
            if artifact.digest is not None:
                _text(artifact.digest, "Checkpoint artifact digest")
    formatted = []
    for item in value.evidence:
        _text(item.summary, "Checkpoint evidence summary")
        _text(item.check, "Checkpoint evidence check")
        for detail in (*item.limitations, *item.blockers):
            _text(detail, "Checkpoint evidence finding")
        public = plans.format_evidence(item)
        if len(public) > MAX_TEXT_LEN:
            raise DomainError("limit_exceeded", "Formatted checkpoint evidence exceeds the shared text limit")
        formatted.append(public)
    if value.outcome == "passed":
        if (not any(item.result == "passed" and item.check == value.check
                    and (value.artifact is None or item.artifact == value.artifact) for item in value.evidence)
                or any(item.result not in ("passed", "observation") or item.blockers
                       or (value.artifact is not None and item.artifact is not None and item.artifact != value.artifact)
                       for item in value.evidence)):
            raise DomainError("invalid", "A passing checkpoint needs consistent successful proof of its exact check and artifact")
    return [*prior_evidence, *formatted]


def _active_target(connection: sqlite3.Connection, command: Command):
    """NEW-only currentness, without the assignment/start guards that would suppress held observations."""
    attempts.require_attempt(connection, command)
    plan = plans._load_plan(connection, command.plan_id)
    if command.payload["checkpoint"].relay is not None:
        plans._check_preconditions(plan, command)
    row = attempts._attempt_row(connection, plan.plan_id, command.attempt_id)
    if row is None:
        raise DomainError("not_found", "The checkpoint attempt does not exist in this plan")
    data = attempts._object(row[7], "attempt")
    step = next((node for node in plan.steps if node.step_id == row[1]), None)
    if (command.attempt_version != row[4] or command.definition_version != row[5] or step is None
            or data.get("definition") != step.definition or row[5] != step.definition["definition_version"]):
        raise DomainError("stale", "The checkpoint attempt version or complete frozen definition has changed")
    if data.get("epoch") != plans._installation_epoch(connection) or data.get("generation") != plan.generation:
        raise DomainError("fenced", "The checkpoint attempt is not current in this epoch and generation")
    if "cancelled_from" in data:
        raise DomainError("fenced", "The checkpoint attempt has been cancelled")
    if row[3] == "stale":
        raise DomainError("stale", "A stale checkpoint requires the explicit recovery operation")
    if row[3] != "active" or step.state != "in-progress":
        raise DomainError("invalid", "Only an active in-progress attempt may submit a new checkpoint")
    if data.get("recovery_required"):
        raise DomainError("held", "The checkpoint attempt requires explicit recovery")
    try:
        timestamp = datetime.fromisoformat(data["last_activity"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "The checkpoint attempt has no valid activity timestamp") from None
    if (plans._now() - timestamp).total_seconds() >= attempts._STALE_SECONDS.get():
        raise DomainError("stale", "The checkpoint attempt exceeded its configured inactivity threshold")
    if data.get("context") is None:
        raise DomainError("invalid", "The checkpoint attempt has no recorded execution context")
    _to_execution_context(data["context"])
    return plan, row, data, step


def _linked_checkpoint(connection: sqlite3.Connection, plan_id: PlanId, checkpoint_id: str) -> tuple[Checkpoint, str]:
    """Resolve an actual attributed audit event, never a recent-history summary or another row's rowid."""
    if (not isinstance(checkpoint_id, str) or re.fullmatch(r"[1-9][0-9]*", checkpoint_id) is None
            or len(checkpoint_id) > 19 or int(checkpoint_id) > 2**63 - 1):
        raise DomainError("invalid", "Checkpoint ID must be a canonical positive event sequence")
    sequence = int(checkpoint_id)
    event = connection.execute(
        "SELECT kind,actor,origin_worker,relay,published_at,payload FROM events WHERE plan_id=? AND sequence=?",
        (plan_id, sequence)).fetchone()
    rows = connection.execute(
        "SELECT c.attempt_id,c.outcome,c.check_name,c.evidence,c.origin_worker,c.relay,c.artifact,c.reassessment,"
        "c.created_at,a.step_id,a.assignee,a.definition_version,a.payload FROM checkpoints c "
        "JOIN attempts a ON a.plan_id=c.plan_id AND a.attempt_id=c.attempt_id "
        "WHERE c.plan_id=? AND c.sequence=?", (plan_id, sequence)).fetchall()
    if event is None or event[0] != "record_checkpoint" or len(rows) != 1:
        raise DomainError("invalid", "Checkpoint ID has no coherent scoped checkpoint event and row")
    row = rows[0]
    try:
        value = _to_checkpoint({"outcome": row[1], "check": row[2], "evidence": json.loads(row[3]),
                                "origin_worker": row[4], "relay": row[5],
                                "artifact": None if row[6] is None else json.loads(row[6]),
                                "reassessment": json.loads(row[7])})
    except (ValueError, TypeError):
        raise DomainError("invalid", "The durable checkpoint is malformed") from None
    payload = attempts._object(event[5], "checkpoint event")
    attempt = attempts._object(row[12], "checkpoint attempt")
    links = {"checkpoint_id": checkpoint_id, "attempt_id": row[0], "step_id": row[9],
             "definition_version": row[11], "definition": attempt.get("definition"),
             "checkpoint": plans.to_jsonable(value)}
    if (any(payload.get(key) != expected for key, expected in links.items())
            or (event[1], event[2], event[3], event[4]) != (value.relay or value.origin_worker,
                                                              value.origin_worker, value.relay, row[8])
            or value.origin_worker != row[10]):
        raise DomainError("invalid", "Checkpoint audit links or attribution contradict durable history")
    return value, event[4]


def install_holds(connection: sqlite3.Connection, plan_id: PlanId, checkpoint_id: str,
                  reassessment: Reassessment) -> None:
    """Append source-linked immediate holds; publication never clears any prior hold."""
    stored, ts = _linked_checkpoint(connection, plan_id, checkpoint_id)
    reassessment = _to_reassessment(reassessment)
    if reassessment != stored.reassessment:
        raise DomainError("invalid", "Hold reassessment does not match its durable checkpoint")
    snapshot = plans.project_plan(connection, plan_id).legacy_snapshot
    closure = _validate_reassessment(snapshot, reassessment)
    if reassessment.disposition == "unchanged":
        return
    targets = (None,) if reassessment.unbounded else closure
    for sid in targets:
        reason = {"kind": "reassessment", "step_id": sid, "source_id": checkpoint_id, "reason": reassessment.reason}
        connection.execute("INSERT INTO holds(hold_id,plan_id,reason,active,created_at) VALUES(?,?,?,1,?)",
                           (str(uuid.uuid4()), plan_id, json.dumps(reason, sort_keys=True, ensure_ascii=False), ts))


def record_checkpoint(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Submit one current active worker, its complete findings and provisional holds in the service transaction."""
    if command.kind != "record_checkpoint":
        raise DomainError("invalid", "The command kind does not match checkpoint publication")
    plan, row, data, step = _active_target(connection, command)
    previous = plans.project_plan(connection, plan.plan_id)
    value = _to_checkpoint(command.payload["checkpoint"])
    evidence = _validate_checkpoint(value, previous.legacy_snapshot, step.evidence)
    version, revision = row[4] + 1, plan.revision + 1
    activity_ts, ts = plans._timestamp(data["last_activity"]), plans._timestamp(plan.updated_at)
    checkpoint_json = plans.to_jsonable(value)
    committed = {"attempt_id": row[0], "step_id": step.step_id, "version": version, "definition_version": row[5]}
    event_payload = {**committed, "definition": data["definition"], "checkpoint": checkpoint_json}
    cursor = connection.execute(
        "INSERT INTO events(plan_id,plan_revision,kind,actor,origin_worker,relay,published_at,payload) VALUES(?,?,?,?,?,?,?,?)",
        (plan.plan_id, revision, command.kind, command.actor, value.origin_worker, value.relay, ts,
         json.dumps(event_payload, sort_keys=True, ensure_ascii=False)))
    sequence = cursor.lastrowid
    committed["checkpoint_id"] = str(sequence)
    # Complete this still-uncommitted event with its actual allocated ID; no committed audit record is rewritten.
    connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                       (json.dumps({**event_payload, "checkpoint_id": str(sequence)}, sort_keys=True, ensure_ascii=False),
                        plan.plan_id, sequence))
    connection.execute(
        "INSERT INTO checkpoints(plan_id,attempt_id,sequence,outcome,check_name,evidence,origin_worker,relay,artifact,"
        "reassessment,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (plan.plan_id, row[0], sequence, value.outcome, value.check,
         json.dumps(checkpoint_json["evidence"], sort_keys=True, ensure_ascii=False), value.origin_worker, value.relay,
         None if value.artifact is None else json.dumps(checkpoint_json["artifact"], sort_keys=True, ensure_ascii=False),
         json.dumps(checkpoint_json["reassessment"], sort_keys=True, ensure_ascii=False), ts))
    data["last_activity"] = activity_ts
    connection.execute("UPDATE attempts SET state='submitted',version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                       (version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]))
    step.evidence = evidence
    step.state = "in-progress" if value.outcome == "passed" else "blocked"
    connection.execute("UPDATE steps SET state=?,payload=? WHERE plan_id=? AND step_id=?",
                       (step.state, plans._step_payload(step), plan.plan_id, step.step_id))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    install_holds(connection, plan.plan_id, committed["checkpoint_id"], value.reassessment)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)
