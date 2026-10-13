"""Explicit recorded stale ownership; logical recovery never implies a host stop."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
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
    StepDefinition,
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
    constructors=[]
    for raw, in connection.execute("SELECT payload FROM events WHERE plan_id=? AND kind='assign_step'",(plan.plan_id,)):
        event=attempts._object(raw,'native assignment constructor')
        if event.get('attempt_id')==row[0]:
            constructors.append(event)
    if (len(constructors)!=1 or any(constructors[0].get(key)!=value for key,value in
            dict(step_id=row[1],assignee=row[2],version=1,definition_version=row[5],definition=data['definition']).items())):
        raise DomainError('unsupported','Native recovery lacks its exact assignment constructor')
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


def legacy_definition_version(host_id, source_directory_id, task_id, generation, revision, step) -> str:
    """Deterministic frozen-definition version over the complete original step and its source key."""
    return "legacy:" + hashlib.sha256(plans.canonical_bytes(
        [host_id, source_directory_id, task_id, generation, revision, step])).hexdigest()


def legacy_step_definition(host_id, source_directory_id, task_id, generation, revision, step) -> StepDefinition:
    """The one frozen definition the importer derives from an original legacy step; proof rebuilds it."""
    return StepDefinition(
        step_id=step["id"], title=step.get("title", ""), dependencies=tuple(step.get("dependsOn", ())),
        completion_check=step.get("check", ""), document=None,
        definition_version=legacy_definition_version(host_id, source_directory_id, task_id, generation, revision, step),
        review_required=False, final_gate=False)


_STALE_HOLD_REASON = "Imported legacy in-progress obligation requires an explicit ownership decision"
_IDENTITY_HOLD_REASON = "Missing required execution identity requires explicit attachment"
_ORIGIN_FIELDS = {"v", "import_id", "plan_id", "plan_generation", "request_epoch", "request_key", "command_digest",
                  "host_id", "source_directory_id", "committed_source_directory_id", "original_task_id",
                  "content_digest", "original_snapshot", "provenance", "writers_stopped", "authorization_renewed",
                  "authorization_note_missing", "initial_contexts", "constructor_attempts"}


def origin_link(connection: sqlite3.Connection, plan_id: PlanId) -> dict:
    """The one validated immutable v1 origin of an imported plan; anything ambiguous or corrupt fails closed."""
    links = connection.execute(
        "SELECT import_id, host_id, source_directory_id, original_task_id, content_digest FROM imports WHERE plan_id=?",
        (plan_id,)).fetchall()
    events = connection.execute(
        "SELECT sequence, payload FROM events WHERE plan_id=? AND kind='import_plan'", (plan_id,)).fetchall()
    if len(links) != 1 or len(events) != 1:
        raise DomainError("invalid", "The imported plan has no unique origin link")
    origin = resources._json_object(events[0][1], "import origin event")
    link = links[0]
    if (set(origin) != _ORIGIN_FIELDS or origin["v"] != 1 or origin["plan_id"] != plan_id
            or (origin["import_id"], origin["host_id"], origin["source_directory_id"],
                origin["original_task_id"], origin["content_digest"]) != tuple(link)
            or not isinstance(origin["request_epoch"], str) or not isinstance(origin["request_key"], str)
            or not isinstance(origin["command_digest"], str) or not isinstance(origin["plan_generation"], str)
            or not isinstance(origin["original_snapshot"], dict) or not isinstance(origin["initial_contexts"], dict)
            or not isinstance(origin["constructor_attempts"], dict)):
        raise DomainError("invalid", "The import origin event contradicts its immutable import map")
    snapshot = origin["original_snapshot"]
    steps = snapshot.get("steps")
    revision = snapshot.get("revision", 0)
    host = connection.execute("SELECT host_id FROM installation WHERE id = 1").fetchone()[0]
    if (not isinstance(steps, list) or type(revision) is not int or snapshot.get("schemaVersion") != 2
            or origin["host_id"] != host or snapshot.get("taskId") != origin["original_task_id"]
            or hashlib.sha256(plans.canonical_bytes(snapshot)).hexdigest() != origin["content_digest"]):
        raise DomainError("invalid", "The retained origin snapshot contradicts its recorded digest and source tuple")
    in_progress = {item.get("id") for item in steps if isinstance(item, dict) and item.get("status") == "in-progress"}
    if (set(origin["initial_contexts"]) != in_progress or set(origin["constructor_attempts"]) != in_progress
            or not all(isinstance(value, str) for value in origin["constructor_attempts"].values())):
        raise DomainError("invalid", "The origin constructor entries contradict the source in-progress steps")
    return origin


def _hold_json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bound_holds(connection, plan_id, attempt_id, step_id, state, data, origin, initial, context):
    """Exact source-owned stale hold and (for an initially unknown context) identity reason/hold rows."""
    source = data["import_source"]
    stale_source = f"{source['source']}:{attempt_id}:stale"
    stale_expected = {"kind": "stale", "step_id": step_id, "source_id": stale_source, "reason": _STALE_HOLD_REASON}
    holds = connection.execute("SELECT hold_id, reason, active FROM holds WHERE plan_id=?", (plan_id,)).fetchall()
    mine = [row for row in holds if (_hold_json(row[1]) or {}).get("source_id") == stale_source]
    if (len(mine) != 1 or mine[0][0] != source["stale_hold_id"] or _hold_json(mine[0][1]) != stale_expected
            or (mine[0][2] == 1) != (state == "stale") or mine[0][2] not in (0, 1)):
        raise DomainError("invalid", "The stale hold is not this obligation's exact source-owned hold")
    identity_id = data.get("identity_hold_id")
    if initial is not None:
        if identity_id is not None:
            raise DomainError("invalid", "A known-context obligation has no identity hold")
        return source["stale_hold_id"], None
    identity_source = f"{source['source']}:{attempt_id}"
    owned = [row for row in holds if (_hold_json(row[1]) or {}).get("source_id") == identity_source]
    reason = connection.execute("SELECT epoch, kind, details, resolved FROM store_hold_reasons WHERE reason_id=?",
                                (identity_id,)).fetchone() if isinstance(identity_id, str) else None
    identity_expected = {"kind": "identity", "step_id": step_id, "source_id": identity_source, "hold_id": identity_id,
                         "reason": _IDENTITY_HOLD_REASON}
    details = {"plan_id": plan_id, "attempt_id": attempt_id, "step_id": step_id, "source": source["source"]}
    unresolved = context is None
    if (reason is None or len(owned) != 1 or owned[0][0] != identity_id or _hold_json(owned[0][1]) != identity_expected
            or reason[0] != origin["request_epoch"] or reason[1] != "identity" or _hold_json(reason[2]) != details
            or reason[3] != (0 if unresolved else 1) or owned[0][2] != (1 if unresolved else 0)):
        raise DomainError("invalid", "The identity reason is not this obligation's exact source-owned reason")
    return source["stale_hold_id"], identity_id


def _imported_proof(connection, plan_id, attempt_id):
    """Positive proof that this attempt is the exact constructor product of its unique v1 import origin."""
    row = attempts._attempt_row(connection, plan_id, attempt_id)
    if row is None:
        raise DomainError("not_found", "The attempt does not exist in this plan")
    step_id, assignee, state, version, definition_version, started_at, raw = row[1:]
    data = resources._json_object(raw, "imported attempt")
    origin = origin_link(connection, plan_id)
    source = data.get("import_source")
    if (assignee != f"legacy-unresolved:{step_id}" or state not in ("stale", "released", "rejected")
            or started_at is not None or data.get("started_at") is not None
            or data.get("recovery_required") is not True or data.get("prior_state") != "active"
            or "stale_source" in data or not isinstance(source, dict)
            or set(source) != {"source", "import_id", "step_id", "stale_hold_id"}
            or source["import_id"] != origin["import_id"] or source["source"] != f"import:{origin['import_id']}"
            or source["step_id"] != step_id or not isinstance(source["stale_hold_id"], str)):
        raise DomainError("invalid", "The attempt is not an unresolved imported obligation")
    if (origin["constructor_attempts"].get(step_id) != attempt_id or step_id not in origin["initial_contexts"]
            or data.get("epoch") != origin["request_epoch"] or data.get("generation") != origin["plan_generation"]):
        raise DomainError("invalid", "The attempt is not the immutable constructor product of its origin")
    snapshot = origin["original_snapshot"]
    original = next((item for item in snapshot["steps"] if isinstance(item, dict) and item.get("id") == step_id), None)
    if original is None or original.get("status") != "in-progress":
        raise DomainError("invalid", "The origin source step was not in progress")
    plan = plans._load_plan(connection, plan_id)
    node = next((item for item in plan.steps if item.step_id == step_id), None)
    expected = plans.to_jsonable(legacy_step_definition(
        origin["host_id"], origin["source_directory_id"], origin["original_task_id"],
        snapshot.get("generation"), snapshot.get("revision", 0), original))
    if (node is None or data.get("definition") != expected or definition_version != expected["definition_version"]
            or node.definition != expected):
        raise DomainError("invalid", "The attempt definition contradicts its mapped origin step")
    if connection.execute("SELECT count(*) FROM capabilities WHERE plan_id=? AND json_valid(payload) AND "
                          "json_extract(payload, '$.attempt_id')=?", (plan_id, attempt_id)).fetchone()[0] != 0:
        raise DomainError("invalid", "An imported obligation never carries a worker capability")
    initial = origin["initial_contexts"][step_id]
    attached = []
    for (event_raw,) in connection.execute("SELECT payload FROM events WHERE plan_id=? AND kind='resolve_attempt'", (plan_id,)):
        event = resources._json_object(event_raw, "attachment event")
        if event.get("attempt_id") == attempt_id and event.get("resolution") == "attach-context":
            attached.append(event)
    context = data.get("context")
    if initial is not None:
        if context != initial or attached:
            raise DomainError("invalid", "The attempt context contradicts its origin lineage")
    elif context is not None:
        event = attached[0] if len(attached) == 1 else None
        if (event is None or set(event) != {"attempt_id", "step_id", "version", "definition_version", "resolution",
                                            "before_version", "reason", "attached_context", "next_step_state"}
                or event["step_id"] != step_id or event["definition_version"] != definition_version
                or event["before_version"] != 1 or event["version"] != 2 or event["attached_context"] != context):
            raise DomainError("invalid", "The attached context lacks one valid immutable lineage")
    elif attached:
        raise DomainError("invalid", "An attachment exists without an attached context")
    base = 2 if context is not None and initial is None else 1
    if version != base + (0 if state == "stale" else 1):
        raise DomainError("invalid", "The attempt version contradicts its immutable history")
    if context is not None:
        namespace, _ = resources.execution_uses(connection, plan_id, attempt_id)
        if namespace["implementation"]["writer"] is not None or namespace["groups"]:
            raise DomainError("invalid", "An imported cohort has unknown writer liveness and no verification roles")
    holds = _bound_holds(connection, plan_id, attempt_id, step_id, state, data, origin, initial, context)
    return row, data, origin, holds


def clear_imported_stale(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId) -> None:
    """Logical terminal ownership ended: clear only the proven source-owned stale hold of this obligation."""
    _, _, _, (stale_id, _) = _imported_proof(connection, plan_id, attempt_id)
    connection.execute("UPDATE holds SET active=0 WHERE hold_id=? AND plan_id=?", (stale_id, plan_id))


def is_imported_attempt(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId) -> bool:
    """True only with full positive imported-origin proof on this connection; anything else fails closed."""
    try:
        _imported_proof(connection, plan_id, attempt_id)
    except (DomainError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return False
    return True


def has_restored_origin(connection, plan_id, attempt_id):
    return bool(connection.execute('SELECT 1 FROM restore_attempt_origins a JOIN restore_origins r USING(restore_id) WHERE a.plan_id=? AND a.attempt_id=? AND r.new_epoch=(SELECT epoch FROM installation)',(plan_id,attempt_id)).fetchone())


def restore_origin(connection,restore_id):
    """Positively bind the sealed origin and its complete, bounded ancestry."""
    try:
        producers=[(actor,attempts._object(raw,'restore producer')) for actor,raw in connection.execute("SELECT actor,payload FROM events WHERE plan_id IS NULL AND plan_revision IS NULL AND kind='database_restored'")]
        head=None;visited=set();expected_link=None
        while True:
            if restore_id in visited or len(visited)>128:
                raise DomainError('invalid','Restore ancestry is cyclic or exceeds 128 ancestors')
            visited.add(restore_id)
            row=connection.execute('SELECT prior_epoch,new_epoch,host_id,source_digest,source_cutoff,payload FROM restore_origins WHERE restore_id=?',(restore_id,)).fetchone()
            if row is None:
                raise DomainError('invalid','Restore origin is absent')
            origin=attempts._object(row[5],'restore origin')
            source=origin['source_metadata'];lineage=origin['lineage_restore_ids']
            if (origin['format']!=1 or origin['restore_id']!=restore_id or source['epoch']!=row[0] or source['host_id']!=row[2] or source['restore_cutoff']!=row[4] or origin['source_digest']!=row[3]
                    or not all(isinstance(value,str) and value.strip() for value in row[:3]) or row[0]==row[1]
                    or not isinstance(row[4],str) or not row[4].strip() or not re.fullmatch('[0-9a-f]{64}',row[3])):
                raise DomainError('invalid','Restore origin metadata is contradictory')
            if (not isinstance(lineage,list) or len(lineage)>128 or
                    not all(isinstance(identity,str) and identity.strip() for identity in lineage) or
                    len(set(lineage))!=len(lineage) or restore_id in lineage or
                    origin['inherited_inventory']!=['restore-inventory:'+identity for identity in lineage]):
                raise DomainError('invalid','Restore ancestry is malformed or cyclic')
            if expected_link is not None and (row[1],row[2],lineage)!=expected_link:
                raise DomainError('invalid','Restore ancestry contradicts its epoch, host or lineage link')
            expected=dict(restore_id=restore_id,epoch=row[1],backup_cutoff=row[4],origin_digest=hashlib.sha256(json.dumps(origin,sort_keys=True).encode()).hexdigest())
            if [(actor,event) for actor,event in producers if event.get('restore_id')==restore_id]!=[('local-administrator',expected)]:
                raise DomainError('invalid','Restore origin contradicts its immutable producer')
            if head is None:
                head=origin
            if not lineage:
                if (connection.execute('SELECT 1 FROM restore_origins WHERE new_epoch=?',(row[0],)).fetchone() or
                        any(event.get('epoch')==row[0] for _,event in producers)):
                    raise DomainError('invalid','Restore ancestry omits its prior origin')
                return head
            expected_link=(row[0],row[2],lineage[1:]);restore_id=lineage[0]
    except (KeyError,TypeError,ValueError) as error:
        raise DomainError('invalid','Restore origin is malformed') from error


def current_restore_origin(connection,epoch,host_id):
    """Return the unique positively validated source origin, or a first restore."""
    rows=connection.execute('SELECT restore_id,host_id FROM restore_origins WHERE new_epoch=?',(epoch,)).fetchall()
    if not rows:
        if (connection.execute('SELECT 1 FROM restore_origins').fetchone() or
                connection.execute("SELECT 1 FROM events WHERE kind='database_restored' AND plan_id IS NULL").fetchone()):
            raise DomainError('invalid','Current restore origin is absent')
        return None
    if len(rows)!=1 or rows[0][1]!=host_id:
        raise DomainError('invalid','Current restore origin is ambiguous or belongs to another host')
    identity=rows[0][0]
    return identity,restore_origin(connection,identity)


def _immutable_namespace(value):
    return dict(v=value['v'],source=value['source'],implementation_members=value['implementation']['members'],
                groups={anchor:{key:item for key,item in group.items() if key!='writer'} for anchor,group in value['groups'].items()})


def _restore_record(connection,restore_id,plan_id,attempt_id,visited=None):
    visited=set() if visited is None else visited
    if restore_id in visited:
        raise DomainError('invalid','Restore ancestry is cyclic')
    visited.add(restore_id)
    origin=restore_origin(connection,restore_id)
    saved=connection.execute('SELECT payload FROM restore_attempt_origins WHERE restore_id=? AND plan_id=? AND attempt_id=?',(restore_id,plan_id,attempt_id)).fetchall()
    if len(saved)!=1:
        raise DomainError('invalid','Restore ancestry is missing or ambiguous')
    record=attempts._object(saved[0][0],'restore attempt origin')
    transition=record.get('transition')
    if not isinstance(transition,dict) or type(transition.get('event_id')) is not int:
        raise DomainError('invalid','Restore producer transition is malformed')
    unsigned={key:value for key,value in record.items() if key!='transition'}
    digest=hashlib.sha256(json.dumps(unsigned,sort_keys=True).encode()).hexdigest()
    event=connection.execute('SELECT kind,actor,payload FROM events WHERE plan_id=? AND sequence=?',(plan_id,transition['event_id'])).fetchone()
    expected={key:value for key,value in transition.items() if key!='event_id'}
    if (event is None or event[:2]!=('restore_attempt','local-administrator') or
            attempts._object(event[2],'restore transition')!=expected or transition.get('record_digest')!=digest or
            transition.get('restore_id')!=restore_id or transition.get('attempt_id')!=attempt_id):
        raise DomainError('invalid','Restore producer contradicts its retained transition')
    if record.get('kind') not in ('native','imported') or not isinstance(record.get('source'),dict):
        raise DomainError('invalid','Restore producer source is malformed')
    source=record['source']
    original=source.get('attempt_row')
    restored=record.get('restored_row')
    if (not isinstance(original,list) or len(original)!=10 or original[:2]!=[plan_id,attempt_id] or
            not isinstance(restored,list) or len(restored)!=10 or restored[:4]!=original[:4] or
            restored[6:9]!=original[6:9] or restored[4]!='stale' or
            restored[5]!=original[5]+int(original[4]!='stale')):
        raise DomainError('invalid','Restore producer full attempt row is incoherent')
    parent=record.get('prior_restore_id')
    if parent is not None:
        prior=_restore_record(connection,parent,plan_id,attempt_id,visited)
        same=prior['restored_row']==original
        if not same and record['kind']==prior['kind']=='native':
            before=prior['restored_row']
            old_data=attempts._object(before[9],'prior native restore')
            new_data=attempts._object(original[9],'native restored role transition')
            old_namespace=old_data.get(resources._USE_NAMESPACE)
            new_namespace=new_data.get(resources._USE_NAMESPACE)
            immutable=lambda data:{key:value for key,value in data.items() if key!=resources._USE_NAMESPACE}
            same=(before[:9]==original[:9] and immutable(old_data)==immutable(new_data) and
                  old_namespace is not None and new_namespace is not None and
                  _immutable_namespace(old_namespace)==_immutable_namespace(new_namespace))
            if same:
                namespace,_=resources.execution_uses(connection,plan_id,attempt_id)
                same=_immutable_namespace(namespace)==_immutable_namespace(new_namespace)
                for anchor,group in new_namespace['groups'].items():
                    resources._writer_proof(connection,new_namespace['source'],group['writer'],'verification',anchor,[member['use_id'] for member in group['members']])
                resources._writer_proof(connection,new_namespace['source'],new_namespace['implementation']['writer'],'implementation',None,[member['use_id'] for member in new_namespace['implementation']['members']])
        if not same and record['kind']==prior['kind']=='imported':
            before=prior['restored_row']
            old_data=attempts._object(before[9],'prior imported restore')
            new_data=attempts._object(original[9],'attached imported restore')
            immutable=lambda data:{key:value for key,value in data.items() if key not in ('context',resources._USE_NAMESPACE)}
            attachment=[]
            for sequence,raw in connection.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='resolve_attempt'",(plan_id,)):
                event=attempts._object(raw,'imported attachment lineage')
                if event.get('attempt_id')==attempt_id and event.get('resolution')=='attach-context':
                    attachment.append((sequence,event))
            same=(before[:5]==original[:5] and before[6:9]==original[6:9] and original[5]==before[5]+1 and
                  old_data.get('context') is None and new_data.get('context') is not None and immutable(old_data)==immutable(new_data) and
                  len(attachment)==1 and prior['transition']['event_id']<attachment[0][0]<transition['event_id'] and
                  attachment[0][1].get('before_version')==before[5] and attachment[0][1].get('version')==original[5] and
                  attachment[0][1].get('attached_context')==new_data['context'])
            if same:
                _imported_proof(connection,plan_id,attempt_id)
        if not same or prior['kind']!=record['kind']:
            raise DomainError('invalid','Restore ancestry does not retain the exact prior source')
    if record['kind']=='native':
        grants=source.get('capabilities')
        if not isinstance(grants,list) or len(grants)!=1:
            raise DomainError('invalid','Native restore has no exact retained constructor grant')
        for grant in grants:
            actual=connection.execute('SELECT * FROM capabilities WHERE capability_id=?',(grant[0],)).fetchone()
            expected=list(grant)
            expected[6]=1
            if actual is None or list(actual)!=expected:
                raise DomainError('invalid','Retained native grant history is absent or altered')
    return record


def restored_proof(connection, plan_id, attempt_id):
    row=attempts._attempt_row(connection,plan_id,attempt_id)
    saved=connection.execute('SELECT a.restore_id,r.host_id,r.payload FROM restore_attempt_origins a JOIN restore_origins r USING(restore_id) WHERE a.plan_id=? AND a.attempt_id=? AND r.new_epoch=(SELECT epoch FROM installation)',(plan_id,attempt_id)).fetchall()
    if row is None or len(saved)!=1:
        raise DomainError('invalid','Restored attempt lacks a unique current producer')
    record=_restore_record(connection,saved[0][0],plan_id,attempt_id)
    origin=attempts._object(saved[0][2],'restore origin')
    generation=connection.execute('SELECT generation FROM plans WHERE plan_id=?',(plan_id,)).fetchone()[0]
    if saved[0][1]!=connection.execute('SELECT host_id FROM installation').fetchone()[0] or origin['generations'][plan_id][1]!=generation:
        raise DomainError('invalid','Restore producer is not current authority')
    data=attempts._object(row[7],'restored attempt')
    if record.get('kind') not in ('native','imported') or not isinstance(record.get('source'),dict):
        raise DomainError('invalid','Restored attempt producer is malformed')
    holds=[record['hold'],*record['source']['owned_holds']]
    complete=list(connection.execute('SELECT * FROM attempts WHERE plan_id=? AND attempt_id=?',(plan_id,attempt_id)).fetchone())
    baseline=record['restored_row']
    if complete[:4]!=baseline[:4] or complete[6:9]!=baseline[6:9]:
        raise DomainError('invalid','Restored attempt full identity has changed')
    if record['kind']=='imported':
        _imported_proof(connection,plan_id,attempt_id)
    else:
        expected=attempts._object(baseline[9],'restored source payload')
        terminal=row[3] in ('released','rejected')
        allowed=('cancellation_reason','cancelled_from','cancelled_at',resources._USE_NAMESPACE) if terminal else (resources._USE_NAMESPACE,)
        actual={key:value for key,value in data.items() if key not in allowed}
        immutable_expected={key:value for key,value in expected.items() if key!=resources._USE_NAMESPACE}
        if actual!=immutable_expected or complete[4] not in ('stale','released','rejected') or complete[5]!=baseline[5]+int(terminal):
            raise DomainError('invalid','Restored payload or lifecycle differs from the immutable source')
        namespace,_=resources.execution_uses(connection,plan_id,attempt_id)
        source_namespace=expected.get(resources._USE_NAMESPACE)
        if source_namespace is not None and _immutable_namespace(namespace)!=_immutable_namespace(source_namespace):
            raise DomainError('invalid','Restored role membership differs from its frozen source namespace')
        actual_uses=[list(item) for item in connection.execute('SELECT rowid,* FROM resource_uses WHERE plan_id=? AND attempt_id=? ORDER BY rowid',(plan_id,attempt_id))]
        source_uses=record['source']['uses']
        if (len(actual_uses)!=len(source_uses) or any(actual[:7]!=source[:7] or actual[8:]!=source[8:] for actual,source in zip(actual_uses,source_uses))):
            raise DomainError('invalid','Restored physical cohort differs from its retained source')
    if row[3] in ('released','rejected'):
        matches=[]
        for sequence,raw in connection.execute("SELECT sequence,payload FROM events WHERE plan_id=? AND kind='resolve_attempt'",(plan_id,)):
            event=attempts._object(raw,'terminal restore decision')
            if event.get('attempt_id')==attempt_id and event.get('resolution') in ('release','reject','replace'):
                matches.append((sequence,event))
        valid=(len(matches)==1 and matches[0][0]>record['transition']['event_id'] and
                matches[0][1].get('before_version')==complete[5]-1 and matches[0][1].get('version')==complete[5] and
                matches[0][1].get('definition')==data['definition'] and matches[0][1].get('reason')==data.get('cancellation_reason') and
                (row[3]=='rejected')==(matches[0][1]['resolution']=='reject'))
        if not matches and row[3]=='released':
            retirements=[]
            for sequence,published,raw in connection.execute("SELECT sequence,published_at,payload FROM events WHERE plan_id=? AND kind='revise_plan'",(plan_id,)):
                event=attempts._object(raw,'restored retirement publication')
                if any(item==dict(step_id=row[1],reason=data.get('cancellation_reason')) for item in event.get('retired_steps',())):
                    retirements.append((sequence,published))
            valid=len(retirements)==1 and retirements[0][0]>record['transition']['event_id'] and retirements[0][1]==data.get('cancelled_at')
        if (not valid or
                data.get('cancelled_from')!='stale' or not attempts._nonblank(data.get('cancelled_at'))):
            raise DomainError('invalid','Terminal restore state has no exact published ownership decision')
    for identity,raw in holds:
        found=connection.execute('SELECT reason,active FROM holds WHERE plan_id=? AND hold_id=?',(plan_id,identity)).fetchone()
        if found is None or found[0]!=raw or found[1] != int(row[3]=='stale'):
            raise DomainError('invalid','Restore source-owned hold is missing or contradictory')
    return row,data,record,holds,record['kind']


def clear_restored_stale(connection,plan_id,attempt_id):
    _,_,record,holds,kind=restored_proof(connection,plan_id,attempt_id)
    if kind=='imported':
        clear_imported_stale(connection,plan_id,attempt_id)
    else:
        for identity,raw in holds:
            connection.execute('UPDATE holds SET active=0 WHERE plan_id=? AND hold_id=? AND reason=?',(plan_id,identity,raw))


def _reserve_known_context(connection, plan_id, attempt_id, execution, ts):
    """Register one known context's strict original cohort; every failure rolls back with the caller."""
    row = attempts._attempt_row(connection, plan_id, attempt_id)
    data = attempts._object(row[7], "imported attempt")
    if execution.writes:
        resource_ids = resources.resolve_context(connection, execution)
        if resources.conflicting_uses(connection, resource_ids, execution.repository_exclusive):
            raise DomainError("resource_conflict", "Writable execution overlaps another unresolved resource use")
        for resource_id in resource_ids:
            scope = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (resource_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO resource_uses(use_id, resource_id, plan_id, attempt_id, scope, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, 'unresolved', ?)",
                (str(uuid.uuid4()), resource_id, plan_id, attempt_id, scope, ts))
    data["context"] = plans.to_jsonable(execution)
    data.pop(resources._USE_NAMESPACE, None)
    connection.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data, sort_keys=True, ensure_ascii=False), plan_id, attempt_id))
    # Decode the owned rows through the strict resource path, persist its namespace, then re-read the result.
    namespace, _ = resources.execution_uses(connection, plan_id, attempt_id)
    resources.save_execution_uses(connection, plan_id, attempt_id, namespace)
    resources.execution_uses(connection, plan_id, attempt_id)


def create_unresolved_attempt(
    connection: sqlite3.Connection,
    plan_id: PlanId,
    step_id: StepId,
    context: ExecutionContext | None,
    *,
    source: str,
    attempt_id: AttemptId | None = None,
) -> AttemptId:
    plan = plans._load_plan(connection, plan_id)
    step = next((s for s in plan.steps if s.step_id == step_id), None)
    if step is None:
        raise DomainError("not_found", f"Step {step_id!r} not found in plan {plan_id!r}")
    epoch = plans._installation_epoch(connection)
    attempt_id = attempt_id or str(uuid.uuid4())
    ts = plans._timestamp(plan.updated_at)
    assignee = f"legacy-unresolved:{step_id}"
    import_id = source[len("import:"):] if source.startswith("import:") else source
    stale_hold_id = str(uuid.uuid4())
    payload = {
        "epoch": epoch,
        "generation": plan.generation,
        "definition": step.definition,
        "context": None,
        "started_at": None,
        "last_activity": None,
        "recovery_required": True,
        "prior_state": "active",
        "import_source": {"source": source, "import_id": import_id, "step_id": step_id, "stale_hold_id": stale_hold_id},
    }
    connection.execute(
        "INSERT INTO attempts(plan_id, attempt_id, step_id, assignee, state, version, definition_version, started_at, stopped_at, payload) "
        "VALUES(?, ?, ?, ?, 'stale', 1, ?, NULL, NULL, ?)",
        (plan_id, attempt_id, step_id, assignee, step.definition["definition_version"],
         json.dumps(payload, sort_keys=True, ensure_ascii=False)),
    )
    # Source-owned stale plan hold for every imported in-progress obligation, known context or not.
    stale_reason = {"kind": "stale", "step_id": step_id, "source_id": f"{source}:{attempt_id}:stale",
                    "reason": _STALE_HOLD_REASON}
    connection.execute("INSERT INTO holds(hold_id, plan_id, reason, active, created_at) VALUES (?, ?, ?, 1, ?)",
                       (stale_hold_id, plan_id, json.dumps(stale_reason, sort_keys=True), ts))

    if context is not None:
        _reserve_known_context(connection, plan_id, attempt_id, context, ts)
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
            "reason": _IDENTITY_HOLD_REASON,
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
    try:
        if has_restored_origin(connection,plan.plan_id,command.attempt_id):
            _,_,_,_,kind=restored_proof(connection,plan.plan_id,command.attempt_id)
            if kind!='imported':
                raise DomainError('unsupported','This restored native source had a known constructor context')
        _, _, import_origin, (_, identity_id) = _imported_proof(connection, plan.plan_id, command.attempt_id)
    except (DomainError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise DomainError("unsupported", "Tokenless and restored origins require their producer recovery contract") from None
    if row[3] != "stale":
        raise DomainError("stale", "Recovery requires ownership recorded stale")
    if data.get("context") is not None:
        raise DomainError("invalid", "Context is already attached to this attempt")

    # The baseline is taken before registration, reservation or hold changes so the cross-revision
    # validator compares against the genuinely previous state.
    previous = plans.project_plan(connection, plan.plan_id)
    execution = resolve_context_claims(connection, payload["attached_context"])
    ts = plans._timestamp(plan.updated_at)
    _reserve_known_context(connection, plan.plan_id, row[0], execution, ts)
    version = row[4] + 1
    connection.execute("UPDATE attempts SET version=? WHERE plan_id=? AND attempt_id=?", (version, plan.plan_id, row[0]))

    if identity_id is not None:
        # Only the proven identity reason/hold of this exact obligation; the stale hold stays until a terminal decision.
        if connection.execute("UPDATE store_hold_reasons SET resolved = 1 WHERE epoch = ? AND reason_id = ? AND resolved = 0",
                              (import_origin['request_epoch'], identity_id)).rowcount != 1:
            raise DomainError("invalid", "The proven identity reason is no longer unresolved")
        connection.execute("UPDATE holds SET active=0 WHERE hold_id=? AND plan_id=?", (identity_id, plan.plan_id))

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
    restored=has_restored_origin(connection,plan.plan_id,command.attempt_id)
    imported = False if restored else is_imported_attempt(connection, plan.plan_id, command.attempt_id)
    if restored:
        row,data,record,_,kind=restored_proof(connection,plan.plan_id,command.attempt_id)
        _target(connection,plan,command)
        if row[3]!='stale':
            raise DomainError('stale','Restored recovery requires stale ownership')
        if data.get('context') is None:
            raise DomainError('held','Unknown restored identity must be attached first')
        step=next(node for node in plan.steps if node.step_id==row[1])
        marker=dict(restore_id=record['transition']['restore_id'],hold_id=record['hold'][0])
    elif imported:
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
    if restored:
        pass  # cancel_attempt already discharged its exact positive restore holds.
    elif imported:
        clear_imported_stale(connection, plan.plan_id, row[0])
    elif marker and "hold_id" in marker:
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
