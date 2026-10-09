"""Transaction-local assignment, accepted input availability and semantic worker progress."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import secrets
import sqlite3
import uuid

from adaptive_planning import plans, resources
from adaptive_planning.config import DEFAULT_STALE_SECONDS
from adaptive_planning.core import MAX_TEXT_LEN, PlanValidationError, validate_execution_scope, validate_service_graph
from adaptive_planning.ownership import (
    _require_attempt_capability, command_relay, hash_token, require_kind_capabilities, require_orchestrator,
)
from adaptive_planning.types import (
    ArtifactRef, AttemptId, AttemptView, Availability, Command, DomainError, MutationReceipt, PlanId,
    PrivateGrant, ReadyStep, StepId, WORKER_KINDS, _to_artifact_ref, _to_availability, _to_evidence,
    _to_execution_context,
)


_STALE_SECONDS: ContextVar[int] = ContextVar("adaptive_planning_attempt_stale_seconds", default=DEFAULT_STALE_SECONDS)


@contextmanager
def activity_policy(stale_seconds: int):
    """Keep each service's inactivity policy local to its write operation."""
    token = _STALE_SECONDS.set(stale_seconds)
    try:
        yield
    finally:
        _STALE_SECONDS.reset(token)


def activity_timestamp(data: dict) -> datetime:
    """Decode an observed aware timestamp without manufacturing or refreshing activity."""
    try:
        timestamp = datetime.fromisoformat(data["last_activity"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "The attempt has no valid activity timestamp") from None
    return timestamp


def require_fresh_activity(data: dict) -> None:
    if (plans._now() - activity_timestamp(data)).total_seconds() >= _STALE_SECONDS.get():
        raise DomainError("stale", "The attempt exceeded its configured inactivity threshold")


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
    if origin != row[2] or (relay is None and command.actor != row[2]) or (relay is not None and relay != command.actor):
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
    sequence = plans._store_sequence(connection)
    namespace, _ = resources.execution_uses(connection, plan.plan_id, attempt_id)
    transition = resources.writer_transition(namespace, "implementation", None, "active", command, plan, sequence,
                                             "Declared implementation assignment")
    connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                       (json.dumps({**committed, "step_id": step_id, "assignee": command.payload["assignee"],
                                    "definition": definition.definition, "execution_transitions": [transition]},
                                   sort_keys=True, ensure_ascii=False), plan.plan_id, sequence))
    resources.save_execution_uses(connection, plan.plan_id, attempt_id, namespace)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, PrivateGrant(None, token))


def _object(raw: str, description: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise DomainError("invalid", f"The {description} is malformed") from None
    if not isinstance(value, dict):
        raise DomainError("invalid", f"The {description} is malformed")
    return value


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _artifact(value) -> ArtifactRef:
    artifact = _to_artifact_ref(value)
    if (not all(_nonblank(field) for field in (artifact.identity, artifact.version, artifact.summary))
            or (artifact.digest is not None and not _nonblank(artifact.digest))):
        raise DomainError("invalid", "Accepted output requires a complete artifact reference")
    return artifact


def _passing_evidence(value, artifact: ArtifactRef) -> bool:
    """Require meaningful, blocker-free evidence explicitly bound to the full artifact."""
    if not isinstance(value, (list, tuple)) or not value:
        return False
    try:
        items = tuple(_to_evidence(item) for item in value)
    except DomainError:
        return False
    return (all(_nonblank(item.summary) and _nonblank(item.check) and item.result == "passed"
                and not item.blockers and (item.artifact is None or item.artifact == artifact) for item in items)
            and any(item.artifact == artifact for item in items))


def _audit_links(data: dict, *, step_id: str, attempt_id: str, result_version: str,
                 artifact: ArtifactRef, definition: dict) -> bool:
    """Optional declared links must agree; event names and fixture labels supply no authority."""
    expected = {"step_id": step_id, "producer_step_id": step_id, "attempt_id": attempt_id,
                "result_version": result_version, "definition_version": definition["definition_version"],
                "definition": definition, "artifact": plans.to_jsonable(artifact)}
    return all(key not in data or data[key] == value for key, value in expected.items())


def _audit_event(connection, plan_id, sequence, **links):
    if type(sequence) is not int or sequence < 1:
        raise DomainError("invalid", "Accepted output has an invalid audit pointer")
    row = connection.execute("SELECT payload FROM events WHERE plan_id=? AND sequence=?", (plan_id, sequence)).fetchone()
    if row is None:
        raise DomainError("invalid", "Accepted output has a missing scoped audit event")
    data = _object(row[0], "accepted output audit event")
    if not _audit_links(data, **links):
        raise DomainError("invalid", "Accepted output audit links contradict its retained history")
    return data


def _matching_reviews(connection, plan_id, producer, artifact):
    matched = {}
    for review_id, reviewer, verdict, raw_evidence, raw_artifact in connection.execute(
            "SELECT review_id,reviewer,verdict,evidence,artifact FROM reviews "
            "WHERE plan_id=? AND attempt_id=? AND step_id=?", (plan_id, producer[0], producer[1])):
        try:
            evidence, supplied = json.loads(raw_evidence), _artifact(json.loads(raw_artifact))
        except (ValueError, TypeError, DomainError):
            continue
        if (verdict == "passed" and _nonblank(reviewer) and reviewer != producer[2]
                and supplied == artifact and _passing_evidence(evidence, artifact)):
            matched[review_id] = reviewer
    return matched


def _validate_proof_pointers(connection, plan_id, source, checkpoints, reviews, links):
    """Every supplied pointer must identify scoped passing proof, whichever layout supplies it."""
    if "checkpoint_sequence" in source:
        sequence = source["checkpoint_sequence"]
        _audit_event(connection, plan_id, sequence, **links)
        if sequence not in checkpoints:
            raise DomainError("invalid", "Accepted output requires its artifact-bound passing checkpoint")
    if "review_sequence" in source:
        review_event = _audit_event(connection, plan_id, source["review_sequence"], **links)
        review_id = review_event.get("review_id")
        if not _nonblank(review_id) or review_id not in reviews:
            raise DomainError("invalid", "Accepted output review pointer does not identify a matching passing review")


def _review_after_invalidation(connection, plan_id, invalidation_sequence, reviews, links) -> bool:
    """Only the first exact-ID link records a review; later references cannot refresh it."""
    seen = set()
    for sequence, raw in connection.execute(
            "SELECT sequence,payload FROM events WHERE plan_id=? ORDER BY sequence", (plan_id,)):
        try:
            data = _object(raw, "review audit event")
        except DomainError:
            continue
        review_id = data.get("review_id")
        if not _nonblank(review_id) or review_id not in reviews or review_id in seen:
            continue
        seen.add(review_id)
        if sequence > invalidation_sequence and _audit_links(data, **links):
            return True
    return False


def _require_valid_result(connection, plan_id, prerequisite, result_version, artifact, reviews, definition, producer_id):
    """Each exact invalidation needs its own later producer disposition; consumer holds are independent."""
    invalidations = []
    for sequence, raw in connection.execute(
            "SELECT sequence,payload FROM events WHERE plan_id=? AND kind='invalidate_result' ORDER BY sequence", (plan_id,)):
        data = _object(raw, "invalidation history")
        if (data.get("result_version") == result_version
                and data.get("producer_step_id", data.get("step_id")) == prerequisite):
            invalidations.append((sequence, data.get("invalidation_id")))
    ids = [identity for _, identity in invalidations]
    if any(not _nonblank(identity) for identity in ids) or len(ids) != len(set(ids)):
        raise DomainError("held", "The accepted result has ambiguous invalidation history")
    links = dict(step_id=prerequisite, attempt_id=producer_id, result_version=result_version,
                 artifact=artifact, definition=definition)
    for sequence, invalidation_id in invalidations:
        resolution = None
        for (raw,) in connection.execute(
                "SELECT payload FROM events WHERE plan_id=? AND kind='resolve_invalidation' AND sequence>? ORDER BY sequence",
                (plan_id, sequence)):
            candidate = _object(raw, "invalidation resolution")
            if candidate.get("invalidation_id") == invalidation_id and candidate.get("affected_step_id") == prerequisite:
                resolution = candidate
        if (resolution is None or resolution.get("disposition") != "revalidated"
                or resolution.get("corrected_result_version") != result_version
                or not _audit_links(resolution, **links)
                or not _passing_evidence(resolution.get("evidence"), artifact)
                or (definition["review_required"]
                    and not _review_after_invalidation(connection, plan_id, sequence, reviews, links))):
            raise DomainError("held", "The original accepted result requires explicit producer revalidation")


def _accepted_history(connection: sqlite3.Connection, plan, prerequisite: str, result_version: str):
    """Inspect exact coherent accepted history, including history needing explicit revalidation.

    ResultVersion is the durable result identity, not ArtifactRef.version. Future acceptance/revalidation
    writers must retain that full relationship and coherent checkpoint/review audit links. This private
    read seam supplies remediation proof; it does not authorize consumption of an invalidated output.
    """
    step = next((node for node in plan.steps if node.step_id == prerequisite), None)
    if (step is None or step.state != "complete" or not step.evidence
            or not all(_nonblank(item) for item in step.evidence)):
        raise DomainError("invalid", "Availability requires a completed prerequisite with retained evidence")
    if connection.execute(f"SELECT 1 FROM attempts WHERE plan_id=? AND step_id=? AND state IN ({plans._LIVE_SQL})",
                          (plan.plan_id, prerequisite)).fetchone():
        raise DomainError("invalid", "A live prerequisite claim contradicts accepted output")
    result = connection.execute(
        "SELECT artifact_kind,identity,summary,digest FROM result_versions WHERE plan_id=? AND step_id=? AND result_version=?",
        (plan.plan_id, prerequisite, result_version)).fetchone()
    producers = []
    for row in connection.execute(
            "SELECT attempt_id,step_id,assignee,state,version,definition_version,started_at,payload "
            "FROM attempts WHERE plan_id=? AND step_id=? AND state='accepted'", (plan.plan_id, prerequisite)):
        data = _object(row[7], "accepted attempt")
        if data.get("result_version") == result_version:
            producers.append((row, data))
    if result is None or len(producers) != 1:
        raise DomainError("invalid", "The exact accepted output has no unique coherent producer")
    producer, data = producers[0]
    if data.get("definition") != step.definition or producer[5] != step.definition["definition_version"]:
        raise DomainError("invalid", "Accepted output does not match its frozen prerequisite definition")
    artifact = _artifact(data.get("artifact"))
    if tuple(result) != (artifact.kind, artifact.identity, artifact.summary, artifact.digest):
        raise DomainError("invalid", "Accepted output metadata contradicts its full artifact")
    links = dict(step_id=prerequisite, attempt_id=producer[0], result_version=result_version,
                 artifact=artifact, definition=step.definition)
    checkpoints = set()
    for sequence, outcome, check, raw_evidence, origin, raw_artifact in connection.execute(
            "SELECT sequence,outcome,check_name,evidence,origin_worker,artifact FROM checkpoints WHERE plan_id=? AND attempt_id=?",
            (plan.plan_id, producer[0])):
        try:
            supplied = _artifact(json.loads(raw_artifact))
            evidence = json.loads(raw_evidence)
        except (ValueError, TypeError, DomainError):
            continue
        if (outcome == "passed" and _nonblank(check) and origin == producer[2]
                and supplied == artifact and _passing_evidence(evidence, artifact)):
            _audit_event(connection, plan.plan_id, sequence, **links)
            checkpoints.add(sequence)
    if not checkpoints:
        raise DomainError("invalid", "Accepted output requires its artifact-bound passing checkpoint")
    reviews = _matching_reviews(connection, plan.plan_id, producer, artifact)
    if step.definition["review_required"] and not reviews:
        raise DomainError("invalid", "Accepted output requires a distinct matching passing review")
    _validate_proof_pointers(connection, plan.plan_id, data, checkpoints, reviews, links)
    if "accepted_sequence" in data:
        accepted_event = _audit_event(connection, plan.plan_id, data["accepted_sequence"], **links)
        _validate_proof_pointers(connection, plan.plan_id, accepted_event, checkpoints, reviews, links)
        for name in ("checkpoint_sequence", "review_sequence"):
            if name in accepted_event and name in data and accepted_event[name] != data[name]:
                raise DomainError("invalid", "Accepted output contains contradictory audit pointers")
        if "review_id" in accepted_event and (not _nonblank(accepted_event["review_id"])
                                              or accepted_event["review_id"] not in reviews):
            raise DomainError("invalid", "Accepted output contains a contradictory review link")
    return artifact, producer[0], step.definition, reviews


def _accepted_output(connection: sqlite3.Connection, plan, prerequisite: str, result_version: str) -> ArtifactRef:
    """Grant output availability only after coherent history AND current producer-specific validity."""
    artifact, producer_id, definition, reviews = _accepted_history(connection, plan, prerequisite, result_version)
    _require_valid_result(connection, plan.plan_id, prerequisite, result_version, artifact, reviews, definition, producer_id)
    return artifact


def _validate_binding(connection, plan, definition, execution, binding: Availability):
    if (not all(_nonblank(value) for value in (binding.prerequisite, binding.result_version, binding.checked_base))
            or binding.prerequisite not in definition["dependencies"]
            or binding.checked_base != execution.checked_base):
        raise DomainError("invalid", "Availability must name a dependency at this consumer's exact final base")
    accepted = _accepted_output(connection, plan, binding.prerequisite, binding.result_version)
    evidence = binding.evidence
    if not _nonblank(evidence.summary) or not _nonblank(evidence.check) or evidence.blockers:
        raise DomainError("invalid", "Availability requires meaningful successful evidence without unresolved blockers")
    if evidence.workspace_id is not None:
        if (execution.workspace_id is None or resources.resolve_resource_id(connection, evidence.workspace_id)
                != resources.resolve_resource_id(connection, execution.workspace_id)):
            raise DomainError("invalid", "Availability evidence refers to an unrelated consumer workspace")
    supplied = _artifact(evidence.artifact)
    if binding.method in ("ancestor", "merge", "cherry-pick"):
        valid = accepted.kind == "code" and supplied == accepted and evidence.result == "passed"
    elif binding.method == "equivalent":
        valid = (supplied.kind == accepted.kind and _nonblank(supplied.digest)
                 and binding.artifact_digest == supplied.digest and evidence.result == "passed")
    elif binding.method == "observation":
        valid = accepted.kind != "code" and supplied == accepted and evidence.result in ("passed", "observation")
    else:
        valid = False
    if not valid:
        raise DomainError("invalid", "Availability method does not attest the exact accepted output at the final base")


def _new_target(connection, command, *, binding=False):
    if command.epoch != plans._installation_epoch(connection):
        raise DomainError("stale_epoch", "The installation epoch has changed")
    plan = plans._load_plan(connection, command.plan_id)
    if binding or command_relay(command) is not None:
        plans._check_preconditions(plan, command)
    if not _nonblank(command.attempt_id):
        raise DomainError("invalid", "An exact consumer attempt is required")
    row = _attempt_row(connection, plan.plan_id, command.attempt_id)
    if row is None:
        raise DomainError("not_found", "The attempt does not exist in this plan")
    data = _object(row[7], "attempt")
    step = next((node for node in plan.steps if node.step_id == row[1]), None)
    if (command.attempt_version != row[4] or command.definition_version != row[5] or step is None
            or data.get("definition") != step.definition or row[5] != step.definition["definition_version"]):
        raise DomainError("stale", "The attempt version or complete frozen definition has changed")
    if data.get("epoch") != plans._installation_epoch(connection) or data.get("generation") != plan.generation:
        raise DomainError("fenced", "The attempt is not current in this installation and plan generation")
    if "cancelled_from" in data:
        raise DomainError("fenced", "The attempt has been cancelled")
    if row[3] == "stale":
        raise DomainError("stale", "The attempt requires explicit recovery")
    if row[3] not in ("assigned", "active", "submitted"):
        raise DomainError("invalid", "Terminal attempts cannot report new progress")
    if data.get("recovery_required"):
        raise DomainError("held", "The attempt requires explicit recovery")
    if command.kind != "report_activity" and (row[3] != "assigned" or step.state != "pending"):
        raise DomainError("invalid", "Only an assigned pending attempt may be prepared or started")
    if binding:
        count = connection.execute(
            "SELECT COUNT(*) FROM capabilities WHERE epoch=? AND plan_id=? AND role='attempt' AND revoked=0 "
            "AND valid_until IS NULL AND json_valid(payload) AND json_extract(payload,'$.attempt_id')=? "
            "AND json_extract(payload,'$.generation')=?",
            (plans._installation_epoch(connection), plan.plan_id, row[0], plan.generation)).fetchone()[0]
        if count != 1:
            raise DomainError("fenced", "The target attempt has no current bound capability")
    try:
        timestamp = datetime.fromisoformat(data["last_activity"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "The attempt has no valid activity timestamp") from None
    if (plans._now() - timestamp).total_seconds() >= _STALE_SECONDS.get():
        raise DomainError("stale", "The attempt exceeded its configured inactivity threshold")
    if data.get("context") is None:
        raise DomainError("invalid", "The attempt has no recorded execution context")
    return plan, row, data, step, _to_execution_context(data["context"])


def _require_start_scope(connection, plan, step):
    try:
        validate_execution_scope(plan.authorization, step.step_id)
    except PlanValidationError as error:
        raise DomainError("forbidden", str(error)) from error
    coordination = plans._validation_coordination(connection, plan.plan_id)
    relevant = {step.step_id, *step.definition["dependencies"]}
    if (coordination.store_hold or any(hold.step_id is None or hold.step_id in relevant for hold in coordination.holds)
            or any(attempt.state == "stale" for attempt in coordination.attempts)
            or plan.draft_json.get("stop_reason") or plan.draft_json.get("block_reason")):
        raise DomainError("held", "Execution is held pending explicit resolution")


def _require_execution_resources(connection, plan_id, attempt_id, execution, *, reacquire=False):
    resource_ids = resources.resolve_context(connection, execution)
    try:
        namespace, owned = resources.execution_uses(connection, plan_id, attempt_id)
    except DomainError as error:
        if error.code == "invalid" and execution.writes and not reacquire:
            raise DomainError("resource_conflict", "Writable execution lacks coherent retained resource obligations") from error
        raise
    if not execution.writes:
        return namespace, owned
    retained = {item["use_id"] for item in namespace["implementation"]["members"]}
    for use_id in retained:
        if owned[use_id][6] != "active":
            raise DomainError("resource_conflict", "The attempt has an unresolved or inappropriate retained resource use")
    writer = namespace["implementation"]["writer"]
    if writer is not None and not reacquire and (writer["state"] != "active"
            or writer["epoch"] != plans._installation_epoch(connection)
            or writer["generation"] != plans._load_plan(connection, plan_id).generation):
        raise DomainError("resource_conflict", "The implementation writer requires explicit reacquisition")
    if set(resources.conflicting_uses(connection, resource_ids, execution.repository_exclusive)) - retained:
        raise DomainError("resource_conflict", "Writable execution overlaps another unresolved resource use")
    return namespace, owned


def bind_availability(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Prepare one exact assigned consumer; rebasing discards its previous input attestations."""
    if command.kind != "bind_availability":
        raise DomainError("invalid", "The command kind does not match availability binding")
    require_kind_capabilities(command.kind, None, command.orchestrator_token, command.attempt_token)
    require_orchestrator(connection, command)
    plan, row, data, step, execution = _new_target(connection, command, binding=True)
    _require_start_scope(connection, plan, step)
    _require_execution_resources(connection, plan.plan_id, row[0], execution)
    bindings, checked_base = command.payload["bindings"], command.payload["checked_base"]
    if not bindings or not _nonblank(checked_base) or len({binding.prerequisite for binding in bindings}) != len(bindings):
        raise DomainError("invalid", "Binding requires a nonempty set of distinct prerequisite attestations")
    prepared = replace(execution, checked_base=checked_base)
    for binding in bindings:
        _validate_binding(connection, plan, step.definition, prepared, binding)
    existing = data.get("availability_bindings", {}) if execution.checked_base == checked_base else {}
    if not isinstance(existing, dict):
        raise DomainError("invalid", "The consumer has malformed retained input bindings")
    data["availability_bindings"] = {**existing, **{binding.prerequisite: plans.to_jsonable(binding) for binding in bindings}}
    data["context"] = plans.to_jsonable(prepared)
    previous = plans.project_plan(connection, plan.plan_id)
    version, revision, ts = row[4] + 1, plan.revision + 1, plans._timestamp(plan.updated_at)
    connection.execute("UPDATE attempts SET version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                       (version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    committed = {"attempt_id": row[0], "version": version, "definition_version": row[5], "checked_base": checked_base}
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts,
                        {**committed, "step_id": step.step_id, "bindings": plans.to_jsonable(bindings)})
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def _insert_activity_event(connection, command, revision, ts, committed):
    connection.execute(
        "INSERT INTO events(plan_id,plan_revision,kind,actor,origin_worker,relay,published_at,payload) VALUES(?,?,?,?,?,?,?,?)",
        (command.plan_id, revision, command.kind, command.actor, command.payload["origin_worker"], command.payload["relay"],
         ts, json.dumps({**committed, "observed_activity": command.payload["observed_activity"]}, sort_keys=True, ensure_ascii=False)))


def _worker_progress(connection, command, *, start):
    require_attempt(connection, command)
    plan, row, data, step, execution = _new_target(connection, command)
    if start:
        _require_start_scope(connection, plan, step)
        _require_execution_resources(connection, plan.plan_id, row[0], execution)
        bindings = data.get("availability_bindings", {})
        if not isinstance(bindings, dict):
            raise DomainError("invalid", "The consumer has malformed retained input bindings")
        for prerequisite in step.definition["dependencies"]:
            if prerequisite not in bindings:
                raise DomainError("invalid", "Every accepted prerequisite needs this consumer's final-base binding")
            binding = _to_availability(bindings[prerequisite])
            if binding.prerequisite != prerequisite:
                raise DomainError("invalid", "The consumer binding names a different prerequisite")
            _validate_binding(connection, plan, step.definition, execution, binding)
    previous = plans.project_plan(connection, plan.plan_id)
    activity_ts = plans._timestamp(data["last_activity"])
    version, revision, ts = row[4] + 1, plan.revision + 1, plans._timestamp(plan.updated_at)
    data.update(last_activity=activity_ts, observed_activity=command.payload["observed_activity"])
    if start:
        connection.execute("UPDATE attempts SET state='active',started_at=? WHERE plan_id=? AND attempt_id=?",
                           (activity_ts, plan.plan_id, row[0]))
        connection.execute("UPDATE steps SET state='in-progress' WHERE plan_id=? AND step_id=?", (plan.plan_id, step.step_id))
    connection.execute("UPDATE attempts SET version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                       (version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    committed = {"attempt_id": row[0], "step_id": step.step_id, "version": version, "definition_version": row[5]}
    _insert_activity_event(connection, command, revision, ts, committed)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def start_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    if command.kind != "start_step":
        raise DomainError("invalid", "The command kind does not match worker start")
    return _worker_progress(connection, command, start=True)


def report_activity(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    if command.kind != "report_activity":
        raise DomainError("invalid", "The command kind does not match worker activity")
    return _worker_progress(connection, command, start=False)


def accept_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Accept one exact current submitted output without asserting worker or host activity."""
    from adaptive_planning import checkpoints, reviews
    plan = checkpoints._owner_target(connection, command, "accept_step")
    if (not _nonblank(command.attempt_id) or command.attempt_version is None
            or command.definition_version is None):
        raise DomainError("invalid", "Acceptance requires the exact attempt, own version and frozen definition")
    row, data, step = reviews._submitted_row(connection, plan, command.payload["step_id"])
    if (command.attempt_id != row[0] or command.attempt_version != row[4]
            or command.definition_version != row[5]):
        raise DomainError("stale", "Acceptance assertions do not identify the current exact submitted target")
    try:
        validate_execution_scope(plan.authorization, step.step_id)
    except PlanValidationError as error:
        raise DomainError("forbidden", str(error)) from error
    coordination = plans._validation_coordination(connection, plan.plan_id)
    if [a.attempt_id for a in coordination.attempts if a.step_id == step.step_id] != [row[0]]:
        raise DomainError("invalid", "Acceptance requires exactly one current live claim on its target")
    relevant = {step.step_id, *step.definition["dependencies"]}
    if (coordination.store_hold or any(h.step_id is None or h.step_id in relevant for h in coordination.holds)
            or plan.draft_json.get("stop_reason") or plan.draft_json.get("block_reason")):
        raise DomainError("held", "Acceptance is held pending explicit resolution")
    nodes = {s.step_id: s for s in plan.steps}
    for prerequisite in step.definition["dependencies"]:
        producer = nodes.get(prerequisite)
        if (producer is None or producer.state != "complete" or not producer.evidence
                or any(not _nonblank(item) for item in producer.evidence)
                or any(a.step_id == prerequisite for a in coordination.attempts)):
            raise DomainError("invalid", "Acceptance requires coherent completed prerequisites without live claims")
    try:
        activity = datetime.fromisoformat(data["last_activity"])
        if activity.tzinfo is None or activity.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "Acceptance requires a valid observed activity timestamp") from None
    if (plans._now() - activity).total_seconds() >= _STALE_SECONDS.get():
        raise DomainError("stale", "The submitted attempt exceeded its inactivity threshold")
    artifact, cycle = reviews._submission(connection, plan, row, data, step)
    supplied = _artifact(command.payload["artifact"])
    value, _ = checkpoints._linked_checkpoint(connection, plan.plan_id, str(cycle["checkpoint_sequence"]))
    if (artifact != supplied or value.outcome != "passed" or not _nonblank(value.check)
            or not _passing_evidence(value.evidence, artifact)
            or not any(item.check == value.check and item.artifact == artifact for item in value.evidence)
            or not step.evidence or any(not _nonblank(item) for item in step.evidence)):
        raise DomainError("invalid", "Acceptance requires the current full artifact and passing checkpoint evidence")
    target = reviews._Target(plan, row, data, step, artifact, cycle)
    selected = reviews._selected_review(connection, target) if step.definition["review_required"] else None
    if step.definition["review_required"] and selected is None:
        raise DomainError("invalid", "Acceptance requires the latest original passing review of this exact submission")
    # Decode ALL retained rows, including released originals and verifier groups. Acceptance never reacquires writing.
    namespace, owned = resources.execution_uses(connection, plan.plan_id, row[0])
    stopped, reason = command.payload["stopped_resource_ids"], command.payload["stopped_reason"]
    if not stopped and reason is not None:
        raise DomainError("invalid", "A stopped reason without named implementation resources asserts no release")
    release_ids = []
    if stopped:
        checkpoints._text(reason, "Stopped implementation reason")
        canonical = {resources.resolve_resource_id(connection, rid) for rid in stopped}
        release_ids = reviews._implementation_release_ids(connection, namespace, owned, canonical)
    previous = plans.project_plan(connection, plan.plan_id)
    result_version = str(uuid.uuid4())
    revision, version, ts = plan.revision + 1, row[4] + 1, plans._timestamp(plan.updated_at)
    links = {"step_id": step.step_id, "attempt_id": row[0], "version": version,
             "result_version": result_version, "definition_version": row[5], "definition": step.definition,
             "artifact": plans.to_jsonable(artifact), "checkpoint_sequence": cycle["checkpoint_sequence"]}
    if selected is not None:
        links.update(review_id=selected[0], review_sequence=selected[1])
    event = {**links, "stopped_resource_ids": list(stopped), "stopped_reason": reason, "released_use_ids": release_ids}
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    accepted_sequence = plans._store_sequence(connection)
    data.update({key: value for key, value in links.items() if key not in ("step_id", "attempt_id", "version")},
                accepted_sequence=accepted_sequence)
    connection.execute("INSERT INTO result_versions(plan_id,result_version,step_id,artifact_kind,identity,summary,digest,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (plan.plan_id, result_version, step.step_id, artifact.kind,
                                                artifact.identity, artifact.summary, artifact.digest, ts))
    connection.execute("UPDATE attempts SET state='accepted',version=?,payload=? WHERE plan_id=? AND attempt_id=?",
                       (version, json.dumps(data, sort_keys=True, ensure_ascii=False), plan.plan_id, row[0]))
    connection.execute("UPDATE steps SET state='complete' WHERE plan_id=? AND step_id=?", (plan.plan_id, step.step_id))
    reviews._release_rows(connection, release_ids)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    committed = {"step_id": step.step_id, "attempt_id": row[0], "version": version,
                 "definition_version": row[5], "result_version": result_version}
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def reopen_attempt(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Reacquire the same submitted worker only after exact scope and retained-use proof."""
    from adaptive_planning import checkpoints, reviews
    plan = checkpoints._owner_target(connection, command, "reopen_attempt")
    checkpoints._text(command.payload["reason"], "Reopen reason")
    if not _nonblank(command.attempt_id) or command.attempt_version is None or command.definition_version is None:
        raise DomainError("invalid", "Reopen requires an exact attempt, own version and frozen definition")
    row = _attempt_row(connection, plan.plan_id, command.attempt_id)
    if row is None:
        raise DomainError("not_found", "The reopen attempt does not exist in this plan")
    current, data, step = reviews._submitted_row(connection, plan, row[1])
    if row[0] != current[0] or command.attempt_version != row[4] or command.definition_version != row[5]:
        raise DomainError("stale", "Reopen assertions do not identify the current exact submitted target")
    _require_start_scope(connection, plan, step)
    try:
        activity = datetime.fromisoformat(data["last_activity"])
        if activity.tzinfo is None or activity.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid", "Reopen requires a valid observed activity timestamp") from None
    if (plans._now() - activity).total_seconds() >= _STALE_SECONDS.get():
        raise DomainError("stale", "The submitted attempt exceeded its inactivity threshold")
    execution = _to_execution_context(data.get("context"))
    namespace, _ = _require_execution_resources(connection, plan.plan_id, row[0], execution, reacquire=True)
    bindings = data.get("availability_bindings", {})
    if not isinstance(bindings, dict):
        raise DomainError("invalid", "Reopen has malformed frozen input bindings")
    for prerequisite in step.definition["dependencies"]:
        if prerequisite not in bindings:
            raise DomainError("invalid", "Reopen requires the unchanged final-base prerequisite bindings")
        binding = _to_availability(bindings[prerequisite])
        if binding.prerequisite != prerequisite:
            raise DomainError("invalid", "Reopen input binding names another prerequisite")
        _validate_binding(connection, plan, step.definition, execution, binding)
    _, ended = reviews._submission(connection, plan, row, data, step, require_artifact=False)
    previous = plans.project_plan(connection, plan.plan_id)
    revision, version, ts = plan.revision + 1, row[4] + 1, plans._timestamp(plan.updated_at)
    event = {"attempt_id": row[0], "step_id": step.step_id, "definition_version": row[5],
             "definition": step.definition, "checkpoint_sequence": ended["checkpoint_sequence"],
             "ended_cycle": ended, "before_version": row[4], "version": version, "reason": command.payload["reason"]}
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    sequence = plans._store_sequence(connection)
    event["execution_transitions"] = [resources.writer_transition(namespace, "implementation", None, "active",
                                                                  command, plan, sequence, command.payload["reason"])]
    connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                       (json.dumps(event, sort_keys=True, ensure_ascii=False), plan.plan_id, sequence))
    connection.execute("UPDATE attempts SET state='active',version=? WHERE plan_id=? AND attempt_id=?", (version, plan.plan_id, row[0]))
    resources.save_execution_uses(connection, plan.plan_id, row[0], namespace)
    connection.execute("UPDATE steps SET state='in-progress' WHERE plan_id=? AND step_id=?", (plan.plan_id, step.step_id))
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"attempt_id": row[0], "step_id": step.step_id, "version": version, "definition_version": row[5]},
                           view.metadata, view.metadata, False, None)


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
        from adaptive_planning import recovery
        if recovery.is_imported_attempt(connection, plan_id, attempt_id):
            if data.get("context") is None:
                raise DomainError("held", "Imported attempts with unknown context cannot be cancelled until context is attached")
            recovery.clear_imported_stale(connection, data)
        else:
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
