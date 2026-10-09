"""Explicit recorded stale ownership; logical recovery never implies a host stop."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import re
import sqlite3
import uuid

from adaptive_planning import attempts, checkpoints, plans, resources
from adaptive_planning.ownership import current_ownership, current_private
from adaptive_planning.types import (
    AttemptId,
    Command,
    DomainError,
    ExecutionContext,
    MutationReceipt,
    PlanId,
    RetirementRequest,
    StepId,
    _to_execution_context,
)


def _policy(now: datetime, threshold_seconds: int) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DomainError("invalid", "Recovery requires an aware observation clock")
    if type(threshold_seconds) is not int or threshold_seconds < 1:
        raise DomainError("invalid", "Recovery requires a positive integer inactivity policy")


def mark_stale(connection: sqlite3.Connection, plan_id: PlanId, now: datetime,
               threshold_seconds: int = 900) -> tuple[AttemptId, ...]:
    """Persist one explicit plan batch, retaining each original claim and obligation."""
    actor, _ = current_ownership(connection, plan_id)
    return _mark_stale(connection, plan_id, now, threshold_seconds, actor)


def _mark_stale(connection, plan_id, now, threshold_seconds, actor):
    _policy(now, threshold_seconds)
    plan = plans._load_plan(connection, plan_id)
    items = []
    for row in connection.execute(
            "SELECT attempt_id,step_id,assignee,state,version,definition_version,started_at,payload "
            "FROM attempts WHERE plan_id=? AND state IN ('assigned','active','submitted') ORDER BY rowid", (plan_id,)):
        data = attempts._object(row[7], "attempt")
        activity = attempts.activity_timestamp(data)
        if (now - activity).total_seconds() < threshold_seconds:
            continue
        items.append((row, data, {
            "attempt_id": row[0], "step_id": row[1], "assignee": row[2], "prior_state": row[3],
            "before_version": row[4], "version": row[4] + 1, "definition_version": row[5],
            "definition": data.get("definition"), "context": data.get("context"),
            "epoch": data.get("epoch"), "generation": data.get("generation"),
            "last_activity": data["last_activity"], "hold_id": str(uuid.uuid4()),
        }))
    if not items:
        return ()
    previous = plans.project_plan(connection, plan_id)
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    payload = {"observed_at": now.isoformat(), "threshold_seconds": threshold_seconds,
               "attempts": [item for _, _, item in items]}
    checkpoints._event_limits(payload)
    plans._insert_event(connection, plan_id, revision, "sweep_stale", actor, ts, payload)
    sequence = plans._store_sequence(connection)
    for row, data, item in items:
        source_id = f"{sequence}:{row[0]}"
        data.update(prior_state=row[3], recovery_required=True,
                    stale_source={"event_id": str(sequence), "source_id": source_id,
                                  "hold_id": item["hold_id"], "version": item["version"]})
        connection.execute("UPDATE attempts SET state='stale',version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                           (item["version"], json.dumps(data, sort_keys=True, ensure_ascii=False), plan_id, row[0]))
        reason = {"kind": "stale", "step_id": row[1], "source_id": source_id,
                  "reason": "Recorded inactivity requires an explicit ownership decision"}
        connection.execute("INSERT INTO holds(hold_id,plan_id,reason,active,created_at) VALUES(?,?,?,1,?)",
                           (item["hold_id"], plan_id, json.dumps(reason, sort_keys=True), ts))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan_id))
    plans._commit_projection(connection, plan_id, previous)
    return tuple(row[0] for row, _, _ in items)


def sweep_stale(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "sweep_stale")
    if any(value is not None for value in (command.attempt_id, command.attempt_version, command.definition_version)):
        raise DomainError("invalid", "A sweep targets a whole plan rather than an individual attempt")
    identities = _mark_stale(connection, plan.plan_id, plans._now(), attempts._STALE_SECONDS.get(), command.actor)
    metadata = plans._metadata(connection, plan.plan_id)
    return MutationReceipt({"attempt_ids": identities}, metadata, metadata, False, None)


def _target(connection, plan, command):
    if (not attempts._nonblank(command.attempt_id) or command.attempt_version is None
            or command.definition_version is None):
        raise DomainError("invalid", "Recovery requires an exact attempt, own version and frozen definition")
    row = attempts._attempt_row(connection, plan.plan_id, command.attempt_id)
    if row is None:
        raise DomainError("not_found", "The recovery attempt does not exist in this plan")
    if command.attempt_version != row[4] or command.definition_version != row[5]:
        raise DomainError("stale", "The recovery attempt assertions have changed")
    data = attempts._object(row[7], "recovery attempt")
    definition = data.get("definition")
    if (not isinstance(definition, dict) or definition.get("step_id") != row[1]
            or definition.get("definition_version") != row[5]):
        raise DomainError("invalid", "The recovery attempt has no coherent frozen definition")
    return row, data


def _native_origin(connection, plan, row, data):
    """Only a proven current native producer can be restored; future origins stay unsupported."""
    step = next((node for node in plan.steps if node.step_id == row[1]), None)
    epoch = plans._installation_epoch(connection)
    if (step is None or data.get("definition") != step.definition or data.get("epoch") != epoch
            or data.get("generation") != plan.generation or "cancelled_from" in data):
        raise DomainError("unsupported", "This recovery origin has no supported current native ownership")
    grants = connection.execute(
        "SELECT secret_hash FROM capabilities WHERE epoch=? AND plan_id=? AND role='attempt' AND revoked=0 "
        "AND valid_until IS NULL AND json_valid(payload) AND json_extract(payload,'$.attempt_id')=? "
        "AND json_extract(payload,'$.generation')=?", (epoch, plan.plan_id, row[0], plan.generation)).fetchall()
    if len(grants) != 1 or not isinstance(grants[0][0], str) or re.fullmatch(r"[0-9a-f]{64}", grants[0][0]) is None:
        raise DomainError("unsupported", "This recovery origin has no unique current native grant")
    _to_execution_context(data.get("context"))
    resources.execution_uses(connection, plan.plan_id, row[0])
    return step


def linked_sweep(connection, plan_id, row, data, marker):
    """Validate immutable original attribution, independently of later mutable activity/step state."""
    if not isinstance(marker, dict) or set(marker) != {"event_id", "source_id", "hold_id", "version"}:
        raise DomainError("invalid", "Recovery requires an exact explicit sweep source")
    identity = marker["event_id"]
    if not isinstance(identity, str) or re.fullmatch(r"[1-9][0-9]{0,18}", identity) is None or int(identity) > 2**63 - 1:
        raise DomainError("invalid", "Recovery sweep source is malformed")
    event = connection.execute("SELECT kind,payload FROM events WHERE plan_id=? AND sequence=?", (plan_id, int(identity))).fetchone()
    if event is None or event[0] != "sweep_stale":
        raise DomainError("invalid", "Recovery has no scoped explicit sweep event")
    payload = attempts._object(event[1], "sweep event")
    items = payload.get("attempts")
    if (not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items)
            or any(not attempts._nonblank(item.get("attempt_id")) for item in items)
            or len({item["attempt_id"] for item in items}) != len(items)):
        raise DomainError("invalid", "The explicit sweep batch is malformed or ambiguous")
    matches = [item for item in items if item["attempt_id"] == row[0]]
    if len(matches) != 1:
        raise DomainError("invalid", "The explicit sweep does not identify this exact attempt")
    item = matches[0]
    links = {"step_id": row[1], "assignee": row[2], "definition_version": row[5], "definition": data["definition"],
             "epoch": data.get("epoch"), "generation": data.get("generation"), "hold_id": marker["hold_id"],
             "version": marker["version"]}
    if (any(item.get(key) != value for key, value in links.items())
            or item.get("prior_state") not in ("assigned", "active", "submitted")
            or type(item.get("before_version")) is not int or item["before_version"] < 1
            or type(item.get("version")) is not int or item["version"] != item["before_version"] + 1
            or item["version"] > row[4] or marker["source_id"] != f"{identity}:{row[0]}"):
        raise DomainError("invalid", "The explicit sweep contradicts the original frozen attempt")
    try:
        now = datetime.fromisoformat(payload["observed_at"])
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "The explicit sweep has no aware observation time") from None
    _policy(now, payload.get("threshold_seconds"))
    if (now - attempts.activity_timestamp(item)).total_seconds() < payload["threshold_seconds"]:
        raise DomainError("invalid", "The explicit sweep contradicts recorded elapsed inactivity")
    _to_execution_context(item.get("context"))
    return item


def swept_target(connection, plan, command):
    row, data = _target(connection, plan, command)
    step = _native_origin(connection, plan, row, data)
    if row[3] != "stale":
        raise DomainError("stale", "Recovery requires ownership recorded stale by an explicit sweep")
    marker = data.get("stale_source")
    item = linked_sweep(connection, plan.plan_id, row, data, marker)
    holds = connection.execute("SELECT hold_id,reason FROM holds WHERE plan_id=? AND active=1", (plan.plan_id,)).fetchall()
    expected = {"kind": "stale", "step_id": row[1], "source_id": marker["source_id"],
                "reason": "Recorded inactivity requires an explicit ownership decision"}
    source_holds = [identity for identity, raw in holds
                    if plans._hold_view(identity, raw).source_id == marker["source_id"]]
    exact = next((raw for identity, raw in holds if identity == marker["hold_id"]), None)
    if (data.get("recovery_required") is not True or data.get("prior_state") != item["prior_state"]
            or data.get("last_activity") != item["last_activity"] or data.get("context") != item["context"]
            or source_holds != [marker["hold_id"]] or exact is None or attempts._object(exact, "recovery hold") != expected):
        raise DomainError("invalid", "Stale ownership has no exact retained sweep and hold proof")
    return row, data, step, marker


def is_imported_attempt(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId) -> bool:
    row = connection.execute(
        "SELECT step_id, assignee, state, version, definition_version, payload FROM attempts WHERE plan_id=? AND attempt_id=?",
        (plan_id, attempt_id),
    ).fetchone()
    if row is None:
        return False
    step_id, assignee, state, version, def_ver, raw = row
    if not assignee.startswith(f"legacy-unresolved:{step_id}"):
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("recovery_required") is not True:
        return False
    import_source = data.get("import_source")
    if not isinstance(import_source, dict):
        return False
    import_id = import_source.get("import_id")
    if not import_id:
        return False
    imp_count = connection.execute(
        "SELECT count(*) FROM imports WHERE import_id=? AND plan_id=?", (import_id, plan_id)
    ).fetchone()[0]
    if imp_count != 1:
        return False
    cap_count = connection.execute(
        "SELECT count(*) FROM capabilities WHERE plan_id=? AND json_extract(payload, '$.attempt_id')=?",
        (plan_id, attempt_id),
    ).fetchone()[0]
    if cap_count != 0:
        return False
    return True


def create_unresolved_attempt(
    connection: sqlite3.Connection,
    plan_id: PlanId,
    step_id: StepId,
    context: ExecutionContext | None,
    *,
    source: str,
) -> AttemptId:
    plan = plans._load_plan(connection, plan_id)
    step = next((s for s in plan.steps if s.step_id == step_id), None)
    if step is None:
        raise DomainError("not_found", f"Step {step_id!r} not found in plan {plan_id!r}")
    epoch = plans._installation_epoch(connection)
    attempt_id = str(uuid.uuid4())
    ts = plans._timestamp(plan.updated_at)
    assignee = f"legacy-unresolved:{step_id}"

    import_id = source.split(":", 1)[1] if source.startswith("import:") else source
    payload = {
        "epoch": epoch,
        "generation": plan.generation,
        "definition": step.definition,
        "context": plans.to_jsonable(context) if context is not None else None,
        "started_at": None,
        "last_activity": None,
        "recovery_required": True,
        "prior_state": "active",
        "import_source": {
            "source": source,
            "import_id": import_id,
            "step_id": step_id,
        },
    }

    connection.execute(
        "INSERT INTO attempts(plan_id, attempt_id, step_id, assignee, state, version, definition_version, started_at, stopped_at, payload) "
        "VALUES(?, ?, ?, ?, 'stale', 1, ?, NULL, NULL, ?)",
        (plan_id, attempt_id, step_id, assignee, step.definition["definition_version"],
         json.dumps(payload, sort_keys=True, ensure_ascii=False)),
    )

    if context is not None:
        if context.writes:
            resource_ids = resources.resolve_context(connection, context)
            conflicts = resources.conflicting_uses(connection, resource_ids, context.repository_exclusive)
            if conflicts:
                raise DomainError("resource_conflict", "Writable execution overlaps another unresolved resource use")
            members = []
            for resource_id in resource_ids:
                scope = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (resource_id,)).fetchone()[0]
                use_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO resource_uses(use_id, resource_id, plan_id, attempt_id, scope, status, created_at) "
                    "VALUES(?, ?, ?, ?, ?, 'unresolved', ?)",
                    (use_id, resource_id, plan_id, attempt_id, scope, ts),
                )
                members.append({"use_id": use_id, "resource_id": resource_id, "scope": scope, "created_at": ts})
            source_dict = {
                "plan_id": plan_id,
                "attempt_id": attempt_id,
                "step_id": step_id,
                "assignee": assignee,
                "definition_version": step.definition["definition_version"],
                "definition": step.definition,
                "origin_epoch": epoch,
                "origin_generation": plan.generation,
            }
            namespace = {
                "v": 1,
                "source": source_dict,
                "implementation": {"members": members, "writer": None},
                "groups": {},
            }
            payload[resources._USE_NAMESPACE] = namespace
            connection.execute(
                "UPDATE attempts SET payload = ? WHERE plan_id = ? AND attempt_id = ?",
                (json.dumps(payload, sort_keys=True, ensure_ascii=False), plan_id, attempt_id),
            )
    else:
        hold_id = str(uuid.uuid4())
        details = json.dumps({"plan_id": plan_id, "attempt_id": attempt_id, "step_id": step_id, "source": source}, sort_keys=True)
        connection.execute(
            "INSERT INTO store_hold_reasons(reason_id, epoch, kind, details, resolved, created_at) VALUES (?, ?, 'identity', ?, 0, ?)",
            (hold_id, epoch, details, ts),
        )
        hold_reason = {
            "kind": "identity",
            "step_id": step_id,
            "source_id": f"{source}:{attempt_id}",
            "hold_id": hold_id,
            "reason": "Missing required execution identity requires explicit attachment",
        }
        connection.execute(
            "INSERT INTO holds(hold_id, plan_id, reason, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (hold_id, plan_id, json.dumps(hold_reason, sort_keys=True), ts),
        )
        payload["identity_hold_id"] = hold_id
        connection.execute(
            "UPDATE attempts SET payload = ? WHERE plan_id = ? AND attempt_id = ?",
            (json.dumps(payload, sort_keys=True, ensure_ascii=False), plan_id, attempt_id),
        )

    return attempt_id


def _attach_context(connection: sqlite3.Connection, plan, command: Command) -> MutationReceipt:
    from adaptive_planning.imports import resolve_context_claims
    payload = command.payload
    if (payload["replacement_assignee"] is not None or payload["replacement_context"] is not None
            or payload["attached_context"] is None):
        raise DomainError("invalid", "Context attachment requires attached_context without replacement fields")
    row, data = _target(connection, plan, command)
    if not is_imported_attempt(connection, plan.plan_id, command.attempt_id):
        raise DomainError("unsupported", "Tokenless and restored origins require their producer recovery contract")
    if row[3] != "stale":
        raise DomainError("stale", "Recovery requires ownership recorded stale")
    if data.get("context") is not None:
        raise DomainError("invalid", "Context is already attached to this attempt")

    execution = resolve_context_claims(connection, payload["attached_context"])
    now = plans._now()
    ts = plans._timestamp(plan.updated_at)
    epoch = plans._installation_epoch(connection)
    members = []
    if execution.writes:
        resource_ids = resources.resolve_context(connection, execution)
        conflicts = resources.conflicting_uses(connection, resource_ids, execution.repository_exclusive)
        if conflicts:
            raise DomainError("resource_conflict", "Writable execution overlaps another unresolved resource use")
        for resource_id in resource_ids:
            scope = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (resource_id,)).fetchone()[0]
            use_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO resource_uses(use_id, resource_id, plan_id, attempt_id, scope, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, 'unresolved', ?)",
                (use_id, resource_id, plan.plan_id, row[0], scope, ts),
            )
            members.append({"use_id": use_id, "resource_id": resource_id, "scope": scope, "created_at": ts})
        source_dict = {
            "plan_id": plan.plan_id,
            "attempt_id": row[0],
            "step_id": row[1],
            "assignee": row[2],
            "definition_version": row[5],
            "definition": data["definition"],
            "origin_epoch": epoch,
            "origin_generation": plan.generation,
        }
        namespace = {
            "v": 1,
            "source": source_dict,
            "implementation": {"members": members, "writer": None},
            "groups": {},
        }
        data[resources._USE_NAMESPACE] = namespace

    version = row[4] + 1
    data["context"] = plans.to_jsonable(execution)
    connection.execute(
        "UPDATE attempts SET version=?, payload=? WHERE plan_id=? AND attempt_id=?",
        (version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]),
    )

    hold_id = data.get("identity_hold_id")
    if hold_id:
        connection.execute(
            "UPDATE store_hold_reasons SET resolved = 1 WHERE epoch = ? AND reason_id = ?",
            (epoch, hold_id),
        )
        checkpoints._clear_holds(connection, [hold_id])

    previous = plans.project_plan(connection, plan.plan_id)
    revision = plan.revision + 1
    committed = {
        "attempt_id": row[0],
        "step_id": row[1],
        "version": version,
        "definition_version": row[5],
        "resolution": "attach-context",
    }
    event = {
        **committed,
        "before_version": row[4],
        "reason": payload["reason"],
        "attached_context": plans.to_jsonable(execution),
        "context": plans.to_jsonable(execution),
        "next_step_state": payload["next_step_state"],
    }
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    connection.execute("UPDATE plans SET revision=?, updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def resolve_attempt(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "resolve_attempt")
    payload = command.payload
    checkpoints._text(payload["reason"], "Recovery ownership observation")
    if payload["resolution"] == "attach-context":
        return _attach_context(connection, plan, command)
    if payload["resolution"] != "confirm-owner":
        return _resolve_logical(connection, plan, command)
    if is_imported_attempt(connection, plan.plan_id, command.attempt_id):
        raise DomainError("forbidden", "Imported attempts cannot be confirmed native")
    if (payload["next_step_state"] != "pending" or payload["stopped_resource_ids"]
            or payload["replacement_assignee"] is not None or payload["replacement_context"] is not None
            or payload["attached_context"] is not None):
        raise DomainError("invalid", "Owner confirmation has only a neutral logical-state envelope and an observation")
    row, data, step, marker = swept_target(connection, plan, command)
    now = plans._now()
    _policy(now, attempts._STALE_SECONDS.get())
    previous = plans.project_plan(connection, plan.plan_id)
    state, version = data["prior_state"], row[4] + 1
    data.update(last_activity=now.isoformat(), recovery_required=False)
    data.pop("prior_state")
    data.pop("stale_source")
    connection.execute("UPDATE attempts SET state=?,version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                       (state, version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]))
    checkpoints._clear_holds(connection, [marker["hold_id"]])
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    committed = {"attempt_id": row[0], "step_id": step.step_id, "version": version, "definition_version": row[5],
                 "resolution": "confirm-owner"}
    event = {**committed, "before_version": row[4], "restored_state": state, "observed_at": now.isoformat(),
             "reason": payload["reason"], "stale_source": marker, "definition": data["definition"]}
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def _internal_command(connection, command, kind, payload, *, row=None):
    current = plans._load_plan(connection, command.plan_id)
    return replace(command, kind=kind, payload=payload, expected_revision=current.revision,
                   ownership_version=current.ownership_version, attempt_token=None,
                   attempt_id=None if row is None else row[0], attempt_version=None if row is None else row[4],
                   definition_version=None if row is None else row[5])


def _original_stops(connection, plan, row, identities):
    from adaptive_planning import reviews
    if not identities:
        return []
    canonical = [resources.resolve_resource_id(connection, rid) for rid in identities]
    if len(set(canonical)) != len(canonical):
        raise DomainError("invalid", "Stopped original resources must be distinct canonical identities")
    namespace, owned = resources.execution_uses(connection, plan.plan_id, row[0])
    return [(uid, resources.resolve_resource_id(connection, owned[uid][1]))
            for uid in reviews._implementation_release_ids(connection, namespace, owned, set(canonical))]


def _resolve_logical(connection, plan, command):
    """Compose real cancellation, retirement, release and assignment within the caller transaction."""
    from adaptive_planning import reviews
    payload, resolution = command.payload, command.payload["resolution"]
    if resolution not in ("replace", "reject", "release"):
        raise DomainError("invalid", "Unknown recovery resolution")
    if payload["attached_context"] is not None:
        raise DomainError("invalid", "Native logical recovery cannot attach a future execution origin")
    if resolution == "replace":
        if (payload["next_step_state"] != "pending" or payload["replacement_context"] is None
                or not attempts._nonblank(payload["replacement_assignee"])):
            raise DomainError("invalid", "Replacement needs the same pending step, assignee and declared context")
        checkpoints._text(payload["replacement_assignee"], "Replacement assignee")
    elif payload["replacement_context"] is not None or payload["replacement_assignee"] is not None:
        raise DomainError("invalid", "Only replacement may declare a new assignee and context")
    if resolution == "reject" and payload["next_step_state"] != "blocked":
        raise DomainError("invalid", "Rejection leaves its logical step blocked")
    imported = is_imported_attempt(connection, plan.plan_id, command.attempt_id)
    if imported:
        row, data = _target(connection, plan, command)
        if row[3] != "stale":
            raise DomainError("stale", "Recovery requires ownership recorded stale")
        if data.get("context") is None:
            raise DomainError("held", "Unknown context imported attempt must be attached before resolution")
        step = next((node for node in plan.steps if node.step_id == row[1]), None)
        if step is None:
            raise DomainError("not_found", "The step does not exist in this plan")
        marker = data.get("import_source", {})
    else:
        row, data, step, marker = swept_target(connection, plan, command)
    release_targets = _original_stops(connection, plan, row, payload["stopped_resource_ids"])
    for use_id, rid in release_targets:
        reviews.release_resource_use(connection, _internal_command(connection, command, "release_resource_use", {
            "use_id": use_id, "resource_ids": (rid,), "stopped": True, "reason": payload["reason"]}, row=row))
    previous = plans.project_plan(connection, plan.plan_id)
    if payload["next_step_state"] == "retired":
        # Retirement itself cancels while the live in-progress claim still exists.
        plans.retire_step(connection, plan.plan_id, RetirementRequest(step.step_id, payload["reason"]), command.actor)
        if step.state == "in-progress":
            previous = plans.project_plan(connection, plan.plan_id)  # Both real transitions were validated by retire_step.
        previous = plans._commit_projection(connection, plan.plan_id, previous)
    else:
        attempts.cancel_attempt(connection, plan.plan_id, row[0], payload["reason"])
        previous = plans._commit_projection(connection, plan.plan_id, previous)
        if resolution == "reject":
            connection.execute("UPDATE attempts SET state='rejected' WHERE plan_id=? AND attempt_id=?", (plan.plan_id, row[0]))
        connection.execute("UPDATE steps SET state=? WHERE plan_id=? AND step_id=?",
                           (payload["next_step_state"], plan.plan_id, step.step_id))
        previous = plans._commit_projection(connection, plan.plan_id, previous)
    if marker and "hold_id" in marker:
        checkpoints._clear_holds(connection, [marker["hold_id"]])
    previous = plans._commit_projection(connection, plan.plan_id, previous)
    assignment, assignment_event = None, None
    if resolution == "replace":
        assignment = attempts.assign_step(connection, _internal_command(connection, command, "assign_step", {
            "step_id": step.step_id, "assignee": payload["replacement_assignee"], "context": payload["replacement_context"]}))
        assignment_event = plans._store_sequence(connection)
        previous = plans.project_plan(connection, plan.plan_id)
    current = plans._load_plan(connection, plan.plan_id)
    cancelled = attempts._attempt_row(connection, plan.plan_id, row[0])
    revision, ts = current.revision + 1, plans._timestamp(current.updated_at)
    committed = {"attempt_id": row[0], "step_id": row[1], "version": cancelled[4], "definition_version": row[5],
                 "resolution": resolution}
    event = {**committed, "before_version": row[4], "definition": data["definition"], "context": data["context"],
             "stale_source": marker, "reason": payload["reason"], "next_step_state": payload["next_step_state"],
             "released_use_ids": [uid for uid, _ in release_targets], "old_token_fenced": True}
    if assignment is not None:
        event.update(old_attempt_id=row[0], old_version=cancelled[4], replacement=dict(assignment.committed),
                     assignment_event_id=str(assignment_event))
        committed.update(assignment.committed, old_attempt_id=row[0], old_version=cancelled[4])
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None if assignment is None else assignment.private)


def append_late_evidence(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "append_late_evidence")
    payload = command.payload
    checkpoints._text(payload["reason"], "Late historical observation reason")
    if command.attempt_id != payload["origin_attempt_id"]:
        raise DomainError("invalid", "Late evidence must name the same exact historical origin attempt")
    row, data = _target(connection, plan, command)
    evidence = checkpoints._evidence(payload["evidence"])
    ids = [resources.resolve_resource_id(connection, rid) for rid in payload["stopped_resource_ids"]]
    if len(set(ids)) != len(ids):
        raise DomainError("invalid", "Late stop reports must name distinct canonical resources")
    if ids:
        namespace, owned = resources.execution_uses(connection, plan.plan_id, row[0])
        attributable = {resources.resolve_resource_id(connection, item[1]) for item in owned.values()}
        if not set(ids) <= attributable:
            raise DomainError("invalid", "Late stop reports must belong to their exact historical origin")
    previous = plans.project_plan(connection, plan.plan_id)
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    committed = {"attempt_id": row[0], "releases_performed": False}
    event = {**committed, "step_id": row[1], "version": row[4], "definition_version": row[5],
             "definition": data["definition"], "origin_epoch": data.get("epoch"), "origin_generation": data.get("generation"),
             "origin_worker": row[2], "evidence": plans.to_jsonable(evidence), "reason": payload["reason"],
             "stopped_resource_ids": list(payload["stopped_resource_ids"])}
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def replacement_replay(connection, command, replayed):
    """Disclose only the exact outer receipt's still-current assignment grant after owner authentication."""
    if command.kind != "resolve_attempt" or command.payload["resolution"] != "replace":
        return replayed
    stored = connection.execute("SELECT response_payload,plan_id FROM requests WHERE epoch=? AND key=?",
                                (command.epoch, command.key)).fetchone()
    committed, metadata, private = plans.decode_response(stored[0])
    bound = (isinstance(committed, dict) and isinstance(committed.get("attempt_id"), str)
             and committed.get("old_attempt_id") == command.attempt_id and committed.get("resolution") == "replace"
             and private is not None and private.orchestrator_token is None and isinstance(private.attempt_token, str)
             and metadata.plan_id == stored[1] == command.plan_id
             and metadata.epoch == command.epoch == plans._installation_epoch(connection)
             and metadata.generation == command.generation)
    grant = (current_private(connection, command.plan_id, private, generation=metadata.generation,
                             attempt_id=committed["attempt_id"]) if bound else None)
    return replace(replayed, private=grant)


def recovery_warnings(connection, plan_id, now, threshold_seconds):
    """A snapshot advisory about recorded inactivity; no read becomes a recovery transition."""
    _policy(now, threshold_seconds)
    metadata = plans._metadata(connection, plan_id)
    overdue, unassessable = [], []
    for identity, state, version, started_at, raw in connection.execute(
            "SELECT attempt_id,state,version,started_at,payload FROM attempts WHERE plan_id=? "
            "AND state IN ('assigned','active','submitted') ORDER BY rowid", (plan_id,)):
        try:
            data = attempts._object(raw, "activity observation")
        except DomainError:
            data = {}
        # Match the existing canonical display tuple; classify only the raw activity below.
        item = {"attempt_id": identity, "version": version, "state": state,
                "last_activity": data.get("last_activity") or started_at or ""}
        try:
            activity = attempts.activity_timestamp(data)
        except DomainError:
            unassessable.append(item)
            continue
        elapsed = (now - activity).total_seconds()
        if elapsed < 0:
            unassessable.append(item)
        elif elapsed >= threshold_seconds:
            overdue.append(item)
    return {"metadata": plans.to_jsonable(metadata), "store_sequence": plans._store_sequence(connection),
            "observed_at": now.isoformat(), "threshold_seconds": threshold_seconds,
            "overdue": overdue, "unassessable_activity": unassessable}
