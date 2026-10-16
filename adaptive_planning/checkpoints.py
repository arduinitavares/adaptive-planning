"""Attributed safe-boundary checkpoints and provisional reassessment holds."""
from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
import uuid

from adaptive_planning import attempts, plans
from adaptive_planning.core import MAX_EVIDENCE_ITEMS, MAX_FILE_BYTES, MAX_TEXT_LEN
from adaptive_planning.ownership import require_kind_capabilities, require_orchestrator
from adaptive_planning.types import (
    ArtifactRef, AttemptId, Checkpoint, Command, DefinitionVersion, DomainError, Evidence, JSONValue,
    MutationReceipt, PlanId, Reassessment, ResultVersion, StepId,
    _to_checkpoint, _to_evidence, _to_execution_context, _to_reassessment,
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


def _require_consistent_checkpoint_reassessment(value: Reassessment) -> None:
    """Reject contradictory NEW publication without reinterpreting durable historical sources."""
    if value.disposition == "unchanged" and value.proposed_draft is not None:
        raise DomainError("invalid", "Unchanged reassessment cannot propose a revised plan")


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
    stored, ts = _reassessment_source(connection, plan_id, checkpoint_id)
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


def _require_new_artifact_version(connection, plan_id, attempt_id, artifact):
    """NEW publication changes the latest explicit version, looking past artifact-less cycles."""
    if artifact is None:
        return
    from adaptive_planning.reviews import _checkpoint_sequences
    for sequence in reversed(_checkpoint_sequences(connection, plan_id, attempt_id)):
        previous, _ = _linked_checkpoint(connection, plan_id, str(sequence))
        if previous.artifact is not None:
            if attempts._artifact(previous.artifact).version == artifact.version:
                raise DomainError("invalid", "A new explicit checkpoint artifact must change its previous explicit version")
            return


def record_checkpoint(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Submit one current active worker, its complete findings and provisional holds in the service transaction."""
    if command.kind != "record_checkpoint":
        raise DomainError("invalid", "The command kind does not match checkpoint publication")
    attempts.require_attempt(connection, command)
    target = attempts._attempt_row(connection, command.plan_id, command.attempt_id)
    if target is not None and target[3] == "stale":
        return _recovery_checkpoint(connection, command)
    plan, row, data, step = _active_target(connection, command)
    previous = plans.project_plan(connection, plan.plan_id)
    value = _to_checkpoint(command.payload["checkpoint"])
    evidence = _validate_checkpoint(value, previous.legacy_snapshot, step.evidence)
    _require_new_artifact_version(connection, plan.plan_id, row[0], value.artifact)
    _require_consistent_checkpoint_reassessment(value.reassessment)
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


def _recovery_checkpoint(connection, command):
    from adaptive_planning.recovery import swept_target
    plan = plans._load_plan(connection, command.plan_id)
    if command.payload["checkpoint"].relay is not None:
        plans._check_preconditions(plan, command)
    row, data, step, marker = swept_target(connection, plan, command)
    previous = plans.project_plan(connection, plan.plan_id)
    value = _to_checkpoint(command.payload["checkpoint"])
    _validate_checkpoint(value, previous.legacy_snapshot, [])
    version, revision, ts = row[4] + 1, plan.revision + 1, plans._timestamp(plan.updated_at)
    committed = {"attempt_id": row[0], "step_id": row[1], "version": version, "definition_version": row[5],
                 "recovery_only": True}
    event = {**committed, "before_version": row[4], "definition": data["definition"], "context": data["context"],
             "epoch": data["epoch"], "generation": data["generation"], "stale_source": marker,
             "source_graph": _graph(plan), "checkpoint": plans.to_jsonable(value)}
    _event_limits(event)
    _require_consistent_checkpoint_reassessment(value.reassessment)
    cursor = connection.execute(
        "INSERT INTO events(plan_id,plan_revision,kind,actor,origin_worker,relay,published_at,payload) VALUES(?,?,?,?,?,?,?,?)",
        (plan.plan_id, revision, "recovery_checkpoint", command.actor, value.origin_worker, value.relay, ts,
         json.dumps(event, sort_keys=True, ensure_ascii=False)))
    committed["recovery_event_id"] = str(cursor.lastrowid)
    connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                       (json.dumps({**event, "recovery_event_id": committed["recovery_event_id"]}, sort_keys=True,
                                   ensure_ascii=False), plan.plan_id, cursor.lastrowid))
    connection.execute("UPDATE attempts SET version=? WHERE plan_id=? AND attempt_id=?", (version, plan.plan_id, row[0]))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    install_holds(connection, plan.plan_id, committed["recovery_event_id"], value.reassessment)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def _reassessment_source(connection, plan_id, source_id):
    """A separate source reader; canonical checkpoint selection stays strictly unchanged."""
    if (not isinstance(source_id, str) or re.fullmatch(r"[1-9][0-9]{0,18}", source_id) is None
            or int(source_id) > 2**63 - 1):
        raise DomainError("invalid", "Reassessment source must be a canonical event sequence")
    event = connection.execute(
        "SELECT kind,actor,origin_worker,relay,published_at,payload FROM events WHERE plan_id=? AND sequence=?",
        (plan_id, int(source_id))).fetchone()
    if event is None or event[0] != "recovery_checkpoint":
        return _linked_checkpoint(connection, plan_id, source_id)
    from adaptive_planning.recovery import linked_sweep
    payload = attempts._object(event[5], "recovery checkpoint event")
    row = attempts._attempt_row(connection, plan_id, payload.get("attempt_id"))
    if row is None or connection.execute("SELECT 1 FROM checkpoints WHERE plan_id=? AND sequence=?", (plan_id, int(source_id))).fetchone():
        raise DomainError("invalid", "Recovery source contradicts canonical or attempt history")
    data = attempts._object(row[7], "recovery source attempt")
    value = _to_checkpoint(payload.get("checkpoint"))
    item = linked_sweep(connection, plan_id, row, data, payload.get("stale_source"))
    links = {"recovery_only": True, "recovery_event_id": source_id, "step_id": row[1], "definition_version": row[5],
             "definition": data.get("definition"), "context": item["context"], "epoch": item["epoch"],
             "generation": item["generation"]}
    if (payload.get("recovery_only") is not True or any(payload.get(key) != expected for key, expected in links.items())
            or type(payload.get("before_version")) is not int or type(payload.get("version")) is not int
            or not item["version"] <= payload["before_version"] < payload["version"] <= row[4]
            or payload["version"] != payload["before_version"] + 1
            or (event[1], event[2], event[3]) != (value.relay or value.origin_worker, value.origin_worker, value.relay)
            or value.origin_worker != row[2]):
        raise DomainError("invalid", "Recovery source contradicts its frozen attribution and semantic versions")
    # Content is validated against its original scope, not a later changed current definition.
    graph = payload.get("source_graph")
    if (not isinstance(graph, dict) or not isinstance(graph.get("steps"), list)
            or [node for node in graph["steps"] if isinstance(node, dict) and node.get("id") == row[1]]
            != [{"id": row[1], "dependsOn": data["definition"]["dependencies"]}]):
        raise DomainError("invalid", "Recovery source graph contradicts the frozen original definition")
    _validate_checkpoint(value, graph, [])
    return value, event[4]


def _owner_target(connection: sqlite3.Connection, command: Command, kind: str):
    """Authenticate a NEW owner operation; the service orders exact replay before this CAS."""
    if command.kind != kind:
        raise DomainError("invalid", "The command kind does not match its owner operation")
    if command.epoch != plans._installation_epoch(connection):
        raise DomainError("stale_epoch", "The installation epoch has changed")
    require_kind_capabilities(kind, None, command.orchestrator_token, command.attempt_token)
    require_orchestrator(connection, command)
    plan = plans._load_plan(connection, command.plan_id)
    plans._check_preconditions(plan, command)
    return plan


def _graph(plan) -> dict[str, JSONValue]:
    return {"steps": [{"id": s.step_id, "dependsOn": list(s.definition["dependencies"])} for s in plan.steps]}


def _evidence(value: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    """Preserve meaningful observed proof, including failures, blockers and limitations."""
    if not isinstance(value, tuple) or not value:
        raise DomainError("invalid", "The owner operation requires nonempty observed evidence")
    if len(value) > MAX_EVIDENCE_ITEMS:
        raise DomainError("limit_exceeded", "Owner evidence exceeds the 50-item limit")
    items = tuple(_to_evidence(item) for item in value)
    for item in items:
        _text(item.summary, "Evidence summary")
        _text(item.check, "Evidence check")
        for detail in (*item.limitations, *item.blockers):
            _text(detail, "Evidence finding")
        if item.artifact is not None:
            attempts._artifact(item.artifact)
            for field in (item.artifact.identity, item.artifact.version, item.artifact.summary):
                _text(field, "Evidence artifact")
            if item.artifact.digest is not None:
                _text(item.artifact.digest, "Evidence digest")
        if len(plans.format_evidence(item)) > MAX_TEXT_LEN:
            raise DomainError("limit_exceeded", "Formatted evidence exceeds the shared text limit")
    _event_limits(items)
    return items


def _event_limits(value) -> None:
    if len(plans.canonical_bytes(value)) > MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Owner audit proof exceeds the 10MB limit")


def _source_holds(connection, plan_id, kind, source_id, step_id=...):
    """Select every exact source hold, independently of bounded public coordination."""
    result = []
    for hold_id, raw in connection.execute(
            "SELECT hold_id,reason FROM holds WHERE plan_id=? AND active=1 ORDER BY rowid", (plan_id,)):
        hold = plans._hold_view(hold_id, raw)
        if hold.kind == kind and hold.source_id == source_id and (step_id is ... or hold.step_id == step_id):
            result.append(hold_id)
    return result


def _add_holds(connection, plan_id, kind, source_id, targets, reason, ts):
    for sid in targets:
        payload = {"kind": kind, "step_id": sid, "source_id": source_id, "reason": reason}
        connection.execute("INSERT INTO holds(hold_id,plan_id,reason,active,created_at) VALUES(?,?,?,1,?)",
                           (str(uuid.uuid4()), plan_id, json.dumps(payload, sort_keys=True, ensure_ascii=False), ts))


def _clear_holds(connection, hold_ids):
    for identity in hold_ids:
        connection.execute("UPDATE holds SET active=0 WHERE hold_id=?", (identity,))


def resolve_reassessment(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Classify one durable proposal and atomically replace only its residual effects."""
    plan = _owner_target(connection, command, "resolve_reassessment")
    payload = command.payload
    _text(payload["reason"], "Reassessment classification reason")
    _reassessment_source(connection, plan.plan_id, payload["checkpoint_id"])
    decision, draft = payload["decision"], payload["draft"]
    if decision == "apply":
        if draft is None:
            raise DomainError("invalid", "Applying reassessment requires an actual revised graph")
    elif decision not in ("reject", "hold") or draft is not None:
        raise DomainError("invalid", "Rejecting or holding reassessment cannot apply a draft")
    previous = plans.project_plan(connection, plan.plan_id)
    before_definitions = {s.step_id: s.definition for s in plan.steps}
    if draft is not None:
        plans._apply_revision_draft(connection, plan, draft)
    current = plans._load_plan(connection, plan.plan_id)
    closure = affected_closure(_graph(current), payload["affected_steps"])
    if decision == "hold" and not payload["unbounded"] and not closure:
        raise DomainError("invalid", "Holding reassessment requires identified or unbounded residual effects")
    ts, revision = plans._timestamp(plan.updated_at), plan.revision + 1
    _clear_holds(connection, _source_holds(connection, plan.plan_id, "reassessment", payload["checkpoint_id"]))
    _add_holds(connection, plan.plan_id, "reassessment", payload["checkpoint_id"],
               (None,) if payload["unbounded"] else closure, payload["reason"], ts)
    if draft is None:
        connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    else:
        connection.execute("UPDATE plans SET revision=?,updated_at=?,payload=? WHERE plan_id=?",
                           (revision, ts, json.dumps({"draft": plans._draft_json(draft, plan.authorization)},
                                                   sort_keys=True, ensure_ascii=False), plan.plan_id))
    event = {**{key: value for key, value in plans.to_jsonable(payload).items() if key != "draft"},
             "summary": payload["reason"], "residual_steps": list(closure)}
    if draft is not None:
        event.update(plans._definition_changes(connection, plan.plan_id, before_definitions))
    _event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    committed = {"checkpoint_id": payload["checkpoint_id"], "decision": decision,
                 "affected_steps": list(closure), "unbounded": payload["unbounded"]}
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def invalidation_review_satisfied(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId,
                                  attempt_id: AttemptId, definition_version: DefinitionVersion,
                                  artifact: ArtifactRef, after_sequence: int | None) -> bool:
    """Use the latest scoped original verdict; later mentions never refresh immutable reviews."""
    row = attempts._attempt_row(connection, plan_id, attempt_id)
    if row is None or row[1] != step_id or row[5] != definition_version:
        return False
    try:
        data = attempts._object(row[7], "review attempt")
        definition = data["definition"]
        if (not isinstance(definition, dict) or definition.get("step_id") != step_id
                or definition.get("definition_version") != definition_version):
            return False
        artifact = attempts._artifact(artifact)
    except (KeyError, DomainError):
        return False
    if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 1):
        return False
    reviews = {}
    for identity, reviewer, verdict, raw_artifact, raw_evidence in connection.execute(
            "SELECT review_id,reviewer,verdict,artifact,evidence FROM reviews "
            "WHERE plan_id=? AND step_id=? AND attempt_id=?", (plan_id, step_id, attempt_id)):
        try:
            supplied = attempts._artifact(json.loads(raw_artifact))
            evidence = json.loads(raw_evidence)
        except (ValueError, TypeError, DomainError):
            continue
        if supplied == artifact and verdict in ("passed", "failed") and attempts._nonblank(reviewer):
            reviews[identity] = (verdict, reviewer, evidence)
    links = dict(step_id=step_id, attempt_id=attempt_id, result_version=data.get("result_version"),
                 artifact=artifact, definition=definition)
    seen, latest = set(), None
    for sequence, raw in connection.execute(
            "SELECT sequence,payload FROM events WHERE plan_id=? ORDER BY sequence", (plan_id,)):
        try:
            event = attempts._object(raw, "review provenance")
        except DomainError:
            continue
        identity = event.get("review_id")
        if not isinstance(identity, str) or identity not in reviews or identity in seen:
            continue
        seen.add(identity)
        if attempts._audit_links(event, **links):
            latest = (sequence, *reviews[identity])
    return (latest is not None and (after_sequence is None or latest[0] > after_sequence)
            and latest[1] == "passed" and latest[2] != row[2]
            and attempts._passing_evidence(latest[3], artifact))


def apply_result_invalidation(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId,
                              result_version: ResultVersion, reason: str, evidence: tuple[Evidence, ...]) -> str:
    """Record exact historical invalidity and its full consumer closure in the caller's transaction."""
    _text(reason, "Result invalidation reason")
    evidence = _evidence(evidence)
    plan = plans._load_plan(connection, plan_id)
    artifact, producer_id, definition, _ = attempts._accepted_history(connection, plan, step_id, result_version)
    affected = affected_closure(_graph(plan), (step_id,))
    identity, ts = str(uuid.uuid4()), plans._timestamp(plan.updated_at)
    event = {"invalidation_id": identity, "producer_step_id": step_id, "step_id": step_id,
             "result_version": result_version, "attempt_id": producer_id,
             "definition_version": definition["definition_version"], "definition": definition,
             "artifact": plans.to_jsonable(artifact), "reason": reason, "summary": reason,
             "evidence": plans.to_jsonable(evidence), "affected_steps": list(affected),
             "affected_definitions": {s.step_id: s.definition for s in plan.steps if s.step_id in affected}}
    _event_limits(event)
    plans._insert_event(connection, plan_id, plan.revision + 1, "invalidate_result",
                        plans.current_ownership(connection, plan_id)[0], ts, event)
    _add_holds(connection, plan_id, "invalidation", identity, affected, reason, ts)
    return identity


def invalidate_result(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = _owner_target(connection, command, "invalidate_result")
    previous = plans.project_plan(connection, plan.plan_id)
    payload = command.payload
    identity = apply_result_invalidation(connection, plan.plan_id, payload["step_id"], payload["result_version"],
                                         payload["reason"], payload["evidence"])
    # This event is still uncommitted and owned by this command; retain the actual capability holder's label.
    sequence = plans._store_sequence(connection)
    connection.execute("UPDATE events SET actor=? WHERE plan_id=? AND sequence=?", (command.actor, plan.plan_id, sequence))
    ts = connection.execute("SELECT published_at FROM events WHERE sequence=?", (sequence,)).fetchone()[0]
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?",
                       (plan.revision + 1, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"invalidation_id": identity}, view.metadata, view.metadata, False, None)


def _linked_invalidation(connection, plan, identity):
    matched = []
    for sequence, revision, raw in connection.execute(
            "SELECT sequence,plan_revision,payload FROM events WHERE plan_id=? AND kind='invalidate_result' ORDER BY sequence",
            (plan.plan_id,)):
        data = attempts._object(raw, "invalidation history")
        if data.get("invalidation_id") == identity:
            matched.append((sequence, revision, data))
    if len(matched) != 1:
        raise DomainError("invalid", "The invalidation has no unique same-plan audit record")
    sequence, revision, data = matched[0]
    producer = data.get("producer_step_id", data.get("step_id"))
    version, affected = data.get("result_version"), data.get("affected_steps")
    if (not attempts._nonblank(producer) or not attempts._nonblank(version)
            or not isinstance(affected, list) or producer not in affected
            or any(not attempts._nonblank(sid) for sid in affected) or len(set(affected)) != len(affected)):
        raise DomainError("invalid", "The invalidation has malformed producer or affected scope")
    artifact, attempt_id, definition, _ = attempts._accepted_history(connection, plan, producer, version)
    if not attempts._audit_links(data, step_id=producer, attempt_id=attempt_id, result_version=version,
                                 artifact=artifact, definition=definition):
        raise DomainError("invalid", "The invalidation contradicts its exact accepted historical output")
    return sequence, revision, data


def _invalid_path(definitions, target, producer):
    """Collect only nodes on a dependency path to this producer, with conservative cycle rejection."""
    visiting, memo = set(), {}
    def visit(sid):
        if sid == producer:
            return {producer}
        if sid in visiting:
            raise DomainError("invalid", "The retained dependency proof contains a cycle")
        if sid in memo:
            return memo[sid]
        visiting.add(sid)
        found = set()
        definition = definitions.get(sid)
        if definition is not None:
            deps = definition.get("dependencies") if isinstance(definition, dict) else None
            if not isinstance(deps, (list, tuple)) or any(not attempts._nonblank(dep) for dep in deps):
                raise DomainError("invalid", "The retained dependency proof is malformed")
            for dependency in deps:
                found.update(visit(dependency))
        visiting.remove(sid)
        memo[sid] = found | {sid} if found else set()
        return memo[sid]
    return visit(target)


def _replanned(connection, plan, target, sequence, invalidation_revision, data, corrective_id, version, evidence):
    step = next((s for s in plan.steps if s.step_id == target), None)
    producer = data.get("producer_step_id", data.get("step_id"))
    if (step is None or step.state not in ("pending", "blocked") or target == producer
            or connection.execute(f"SELECT 1 FROM attempts WHERE plan_id=? AND step_id=? AND state IN ({plans._LIVE_SQL})",
                                  (plan.plan_id, target)).fetchone()):
        raise DomainError("invalid", "Replanning requires pending or blocked consumer work without a frozen live claim")
    original = data.get("affected_definitions")
    if (not isinstance(original, dict) or set(original) != set(data["affected_steps"])
            or type(invalidation_revision) is not int or invalidation_revision < 1):
        raise DomainError("invalid", "Replanning requires retained original affected definitions")
    relevant = _invalid_path(original, target, producer) - {producer}
    current = {s.step_id: s.definition for s in plan.steps}
    affected_closure(_graph(plan), (producer,))
    if not relevant or _invalid_path(current, target, producer):
        raise DomainError("invalid", "The actual consumer route still requires the invalid producer")
    known = {sid: original[sid]["dependencies"] for sid in relevant}
    changed = False
    for kind, revision, raw in connection.execute(
            "SELECT kind,plan_revision,payload FROM events WHERE plan_id=? AND sequence>? "
            "AND kind IN ('revise_plan','resolve_reassessment') ORDER BY sequence", (plan.plan_id, sequence)):
        event = attempts._object(raw, "graph revision")
        if kind == "resolve_reassessment" and event.get("decision") != "apply":
            continue
        definitions, prior = event.get("changed_definitions"), event.get("prior_dependencies")
        if not isinstance(definitions, dict) or not isinstance(prior, dict) or set(definitions) != set(prior):
            continue
        if type(revision) is not int or not invalidation_revision < revision <= plan.revision:
            continue
        for sid in relevant.intersection(definitions):
            definition = definitions[sid]
            dependencies = definition.get("dependencies") if isinstance(definition, dict) else None
            if (prior[sid] != known[sid] or not isinstance(dependencies, list)
                    or any(not attempts._nonblank(dep) for dep in dependencies)
                    or definition.get("step_id") != sid):
                raise DomainError("invalid", "The relevant revision contradicts its preceding dependency proof")
            changed |= known[sid] != dependencies
            known[sid] = dependencies
    if not changed or any(known[sid] != current[sid]["dependencies"] for sid in relevant if sid in current):
        raise DomainError("invalid", "The actual corrected route lacks coherent permitted edge provenance")
    if corrective_id is None and version is None:
        if any(item.result != "passed" or item.blockers or item.artifact is not None for item in evidence):
            raise DomainError("invalid", "Replanned removal requires artifact-free successful route evidence without blockers")
        return {}
    if not attempts._nonblank(corrective_id) or not attempts._nonblank(version) or corrective_id in (target, producer):
        raise DomainError("invalid", "Replanned correction requires a distinct exact corrective result")
    if corrective_id not in _ancestors(current, target):
        raise DomainError("invalid", "The actual replanned route does not target the supplied correction")
    artifact = attempts._accepted_output(connection, plan, corrective_id, version)
    if not attempts._passing_evidence(evidence, artifact):
        raise DomainError("invalid", "Replanned evidence does not prove its exact corrective artifact")
    return {"corrective_artifact": plans.to_jsonable(artifact)}


def _ancestors(definitions, target):
    result, todo = set(), list(definitions[target]["dependencies"])
    while todo:
        sid = todo.pop()
        if sid not in result:
            result.add(sid)
            todo.extend(definitions[sid]["dependencies"])
    return result


def resolve_invalidation(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Resolve only the named hold, preserving producer validity and immutable result identities."""
    plan = _owner_target(connection, command, "resolve_invalidation")
    payload = command.payload
    _text(payload["reason"], "Invalidation resolution reason")
    evidence = _evidence(payload["evidence"])
    sequence, invalidation_revision, invalidation = _linked_invalidation(connection, plan, payload["invalidation_id"])
    target, disposition = payload["affected_step_id"], payload["disposition"]
    held = _source_holds(connection, plan.plan_id, "invalidation", payload["invalidation_id"], target)
    if target not in invalidation["affected_steps"] or not held:
        raise DomainError("invalid", "The named step has no unresolved hold for this invalidation")
    corrective, version = payload["corrective_step_id"], payload["corrected_result_version"]
    producer = invalidation.get("producer_step_id", invalidation.get("step_id"))
    proof = {}
    if disposition == "replanned":
        proof = _replanned(connection, plan, target, sequence, invalidation_revision, invalidation, corrective, version, evidence)
    else:
        if disposition == "revalidated":
            if corrective is not None or not attempts._nonblank(version):
                raise DomainError("invalid", "Revalidation must name only this affected step's accepted result")
            if target == producer and version != invalidation["result_version"]:
                raise DomainError("invalid", "Producer revalidation must name the exact invalidated result")
            source = target
        elif disposition == "superseded":
            if (not attempts._nonblank(corrective) or corrective in (target, producer)
                    or not attempts._nonblank(version)):
                raise DomainError("invalid", "Supersession requires a distinct accepted corrective step and result")
            source = corrective
            attempts._accepted_output(connection, plan, source, version)
        else:
            raise DomainError("invalid", "Unknown invalidation disposition")
        artifact, attempt_id, definition, _ = attempts._accepted_history(connection, plan, source, version)
        if not attempts._passing_evidence(evidence, artifact):
            raise DomainError("invalid", "Resolution evidence must prove the exact full accepted artifact")
        if (disposition == "revalidated" and definition["review_required"]
                and not invalidation_review_satisfied(connection, plan.plan_id, source, attempt_id,
                                                     definition["definition_version"], artifact, sequence)):
            raise DomainError("invalid", "Revalidation requires the latest matching passed review recorded after invalidation")
        proof = {"step_id": source, "attempt_id": attempt_id, "result_version": version,
                 "definition_version": definition["definition_version"], "definition": definition,
                 "artifact": plans.to_jsonable(artifact)}
    previous = plans.project_plan(connection, plan.plan_id)
    ts, revision = plans._timestamp(plan.updated_at), plan.revision + 1
    event = {**plans.to_jsonable(payload), **proof, "summary": payload["reason"]}
    _event_limits(event)
    _clear_holds(connection, held)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"invalidation_id": payload["invalidation_id"], "affected_step_id": target,
                            "disposition": disposition}, view.metadata, view.metadata, False, None)
