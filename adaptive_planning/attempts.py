"""Transaction-local bounded assignment and frozen worker capability authentication."""
from __future__ import annotations

from dataclasses import replace
import json
import secrets
import sqlite3
import uuid

from adaptive_planning import plans, resources
from adaptive_planning.core import MAX_TEXT_LEN, PlanValidationError, validate_execution_scope, validate_service_graph
from adaptive_planning.ownership import (
    _require_attempt_capability, command_relay, hash_token, require_kind_capabilities, require_orchestrator,
)
from adaptive_planning.types import (
    AttemptId, AttemptView, Command, DomainError, MutationReceipt, PlanId, PrivateGrant, ReadyStep, StepId,
    WORKER_KINDS,
)


def ready_steps(connection: sqlite3.Connection, plan_id: PlanId) -> tuple[ReadyStep, ...]:
    """Return advisory logical readiness; assignment rechecks its context in the write transaction."""
    return plans.list_ready(connection, plan_id)


def _attempt_row(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId):
    return connection.execute(
        "SELECT attempt_id,step_id,assignee,state,version,definition_version,started_at,payload "
        "FROM attempts WHERE plan_id=? AND attempt_id=?", (plan_id, attempt_id)).fetchone()


def current_attempt(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId) -> AttemptView:
    """Read the latest retained attempt for this exact plan and step, without capability authentication."""
    plans._load_plan(connection, plan_id)
    row = connection.execute(
        "SELECT attempt_id,step_id,assignee,state,version,definition_version,started_at,payload "
        "FROM attempts WHERE plan_id=? AND step_id=? ORDER BY rowid DESC LIMIT 1", (plan_id, step_id)).fetchone()
    if row is None:
        raise DomainError("not_found", "No attempt exists for this plan and step")
    return plans._attempt_view(row)


def require_attempt(connection: sqlite3.Connection, command: Command) -> AttemptView:
    """Authenticate epoch/generation/token before replay; NEW version/lifecycle guards belong after replay."""
    if command.epoch != plans._installation_epoch(connection):
        raise DomainError("stale_epoch", "The installation epoch has changed")
    if command.kind not in WORKER_KINDS:
        raise DomainError("invalid", "An attempt capability authorizes only worker commands")
    relay = command_relay(command)
    require_kind_capabilities(command.kind, relay, command.orchestrator_token, command.attempt_token)
    if relay is None:
        if command.expected_revision is not None or command.ownership_version is not None:
            raise DomainError("invalid", "Direct worker commands require null revision and ownership versions")
    else:
        require_orchestrator(connection, command)
    row = _require_attempt_capability(
        connection, command.plan_id, command.generation, command.attempt_id, command.attempt_token)
    origin = (command.payload["checkpoint"].origin_worker if command.kind == "record_checkpoint"
              else command.payload["origin_worker"])
    if origin != row[2] or (relay is None and command.actor != row[2]):
        raise DomainError("forbidden", "Worker identity does not match the assigned attempt")
    return plans._attempt_view(row)


def assign_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Atomically freeze one pending definition, its scope latch, resource reservations and private grant."""
    if command.kind != "assign_step":
        raise DomainError("invalid", "The command kind does not match assignment")
    if command.epoch != plans._installation_epoch(connection):
        raise DomainError("stale_epoch", "The installation epoch has changed")
    require_kind_capabilities(command.kind, None, command.orchestrator_token, command.attempt_token)
    require_orchestrator(connection, command)
    plan = plans._load_plan(connection, command.plan_id)
    plans._check_preconditions(plan, command)
    previous = plans.project_plan(connection, plan.plan_id)
    step_id = command.payload["step_id"]
    definition = next((s for s in plan.steps if s.step_id == step_id), None)
    if definition is None:
        raise DomainError("not_found", "The assignment step does not exist")
    if definition.state != "pending":
        raise DomainError("invalid", "Only a pending step may be assigned")
    try:
        authorization = validate_execution_scope(plan.authorization, step_id)
    except PlanValidationError as error:
        raise DomainError("forbidden", str(error)) from error

    coordination = plans._validation_coordination(connection, plan.plan_id)
    if coordination.store_hold or any(h.step_id is None or h.step_id == step_id for h in coordination.holds):
        raise DomainError("held", "Assignment is held pending explicit resolution")
    if any(a.state == "stale" for a in coordination.attempts):
        raise DomainError("held", "A stale attempt requires recovery before assignment")
    if plan.draft_json.get("stop_reason") or plan.draft_json.get("block_reason"):
        raise DomainError("held", "The plan is stopped or blocked")
    if any(a.step_id == step_id for a in coordination.attempts):
        raise DomainError("conflict", "The step already has a live attempt")
    steps = {s.step_id: s for s in plan.steps}
    for prerequisite in definition.definition["dependencies"]:
        producer = steps.get(prerequisite)
        if (producer is None or producer.state != "complete" or not producer.evidence
                or any(not isinstance(item, str) or not item.strip() for item in producer.evidence)
                or any(a.step_id == prerequisite for a in coordination.attempts)):
            raise DomainError("invalid", "Assignment requires coherent completed prerequisites with evidence")
        if any(h.step_id == prerequisite for h in coordination.holds):
            raise DomainError("held", "A prerequisite has an unresolved hold")
    try:
        validate_service_graph(plans._proposal(previous.legacy_snapshot), None, coordination)
    except PlanValidationError as error:
        raise DomainError("invalid", str(error)) from error

    execution = command.payload["context"]
    resource_ids = resources.resolve_context(connection, execution)
    if execution.writes and resources.conflicting_uses(connection, resource_ids, execution.repository_exclusive):
        raise DomainError("resource_conflict", "Writable execution context overlaps unresolved resource use")
    execution = replace(
        execution,
        repository_id=(None if execution.repository_id is None else
                       resources.resolve_resource_id(connection, execution.repository_id)),
        workspace_id=(None if execution.workspace_id is None else
                      resources.resolve_resource_id(connection, execution.workspace_id)),
        external_ids=tuple(dict.fromkeys(resources.resolve_resource_id(connection, rid)
                                        for rid in execution.external_ids)))
    attempt_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    epoch = plans._installation_epoch(connection)
    payload = {"epoch": epoch, "generation": plan.generation, "definition": definition.definition,
               "context": plans.to_jsonable(execution), "last_activity": ts, "recovery_required": False}
    connection.execute(
        "INSERT INTO attempts(plan_id,attempt_id,step_id,assignee,state,version,definition_version,payload) "
        "VALUES(?,?,?,?,'assigned',1,?,?)",
        (plan.plan_id, attempt_id, step_id, command.payload["assignee"], definition.definition["definition_version"],
         json.dumps(payload, sort_keys=True, ensure_ascii=False)))
    connection.execute(
        "INSERT INTO capabilities(capability_id,epoch,plan_id,role,secret_hash,payload) "
        "VALUES(?,?,?,'attempt',?,?)",
        (str(uuid.uuid4()), epoch, plan.plan_id, hash_token(token),
         json.dumps({"attempt_id": attempt_id, "generation": plan.generation}, sort_keys=True)))
    if execution.writes:
        for resource_id in resource_ids:
            scope = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (resource_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,scope,status,created_at) "
                "VALUES(?,?,?,?,?,'active',?)",
                (str(uuid.uuid4()), resource_id, plan.plan_id, attempt_id, scope, ts))
    connection.execute(
        "UPDATE plans SET revision=?,updated_at=?,payload=? WHERE plan_id=?",
        (revision, ts, json.dumps({"draft": plans._draft_json(plan.draft(), authorization)},
                                  sort_keys=True, ensure_ascii=False), plan.plan_id))
    committed = {"attempt_id": attempt_id, "version": 1,
                 "definition_version": definition.definition["definition_version"]}
    plans._insert_event(connection, plan.plan_id, revision, "assign_step", command.actor, ts,
                        {**committed, "step_id": step_id, "assignee": command.payload["assignee"]})
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, PrivateGrant(None, token))


def cancel_attempt(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId, reason: str) -> None:
    """Logically cancel an exact claim; unresolved writer use and scope are never released by cancellation."""
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > MAX_TEXT_LEN:
        raise DomainError("invalid", "Cancellation requires a bounded non-empty reason")
    plan = plans._load_plan(connection, plan_id)
    row = _attempt_row(connection, plan_id, attempt_id)
    if row is None:
        raise DomainError("not_found", "The attempt does not exist in this plan")
    step = next(s for s in plan.steps if s.step_id == row[1])
    if row[3] not in plans.LIVE_ATTEMPT_STATES or step.state not in ("pending", "blocked", "in-progress"):
        raise DomainError("invalid", "Completed or terminal attempt history cannot be cancelled")
    try:
        data = json.loads(row[7])
    except (ValueError, TypeError):
        raise DomainError("invalid", "The attempt has no coherent frozen definition") from None
    epoch = plans._installation_epoch(connection)
    if (not isinstance(data, dict) or data.get("definition") != step.definition
            or data.get("epoch") != epoch or data.get("generation") != plan.generation
            or row[5] != step.definition["definition_version"]):
        raise DomainError("invalid", "The attempt has no coherent frozen definition")
    capability_ids = []
    for cid, secret_hash, raw in connection.execute(
            "SELECT capability_id,secret_hash,payload FROM capabilities WHERE epoch=? AND plan_id=? AND role='attempt'",
            (epoch, plan_id)):
        try:
            binding = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (isinstance(binding, dict) and binding.get("attempt_id") == attempt_id
                and binding.get("generation") == plan.generation and isinstance(secret_hash, str)
                and len(secret_hash) == 64):
            capability_ids.append(cid)
    if not capability_ids:
        raise DomainError("invalid", "The claimed attempt has no bound worker capability")
    data.update(cancellation_reason=reason.strip(), cancelled_from=row[3], cancelled_at=plans._timestamp())
    connection.execute("UPDATE attempts SET state='released',version=version+1,payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data, sort_keys=True, ensure_ascii=False), plan_id, attempt_id))
    for cid in capability_ids:
        connection.execute("UPDATE capabilities SET revoked=1 WHERE capability_id=?", (cid,))
    # Logical ownership ended; a writer's stopped state remains unknown. Retain
    # every identity and use, and leave released/unrelated obligations untouched.
    connection.execute(
        "UPDATE resource_uses SET status='unresolved' WHERE plan_id=? AND attempt_id=? AND status='active'",
        (plan_id, attempt_id))
    if step.state == "in-progress":
        connection.execute("UPDATE steps SET state='blocked' WHERE plan_id=? AND step_id=?", (plan_id, step.step_id))
