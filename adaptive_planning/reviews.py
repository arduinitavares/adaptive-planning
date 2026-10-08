"""Immutable artifact observations and transaction-local writable verification dispatch."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import uuid

from adaptive_planning import attempts, checkpoints, plans, resources
from adaptive_planning.core import MAX_EVIDENCE_ITEMS
from adaptive_planning.types import (
    ArtifactRef, AttemptId, Command, DefinitionVersion, DomainError, MutationReceipt, PlanId, StepId, _to_review,
)


@dataclass
class _Target:
    plan: object
    row: tuple
    data: dict
    step: object
    artifact: ArtifactRef
    cycle: dict
    result_version: str | None = None


def _checkpoint_sequences(connection, plan_id, attempt_id):
    sequences = {row[0] for row in connection.execute(
        "SELECT sequence FROM checkpoints WHERE plan_id=? AND attempt_id=?", (plan_id, attempt_id))}
    for sequence, raw in connection.execute(
            "SELECT sequence,payload FROM events WHERE plan_id=? AND kind='record_checkpoint' ORDER BY sequence", (plan_id,)):
        data = attempts._object(raw, "checkpoint publication")
        if data.get("attempt_id") == attempt_id:
            sequences.add(sequence)
    return sorted(sequences)


def _submission(connection, plan, row, data, step, *, require_artifact=True):
    sequences = _checkpoint_sequences(connection, plan.plan_id, row[0])
    if not sequences:
        raise DomainError("invalid", "Submitted work has no attributed checkpoint publication")
    sequence = sequences[-1]
    value, _ = checkpoints._linked_checkpoint(connection, plan.plan_id, str(sequence))
    if value.artifact is None and require_artifact:
        raise DomainError("invalid", "Artifact review requires the current checkpoint's explicit artifact")
    artifact = None if value.artifact is None else attempts._artifact(value.artifact)
    event = attempts._object(connection.execute("SELECT payload FROM events WHERE plan_id=? AND sequence=?",
                                               (plan.plan_id, sequence)).fetchone()[0], "checkpoint publication")
    if (event.get("attempt_id") != row[0] or event.get("step_id") != step.step_id
            or event.get("definition") != step.definition or type(event.get("version")) is not int
            or not 1 <= event["version"] <= row[4]
            or ("checkpoint_sequence" in data and data["checkpoint_sequence"] != sequence)):
        raise DomainError("invalid", "Current checkpoint links contradict its frozen submission")
    for (raw,) in connection.execute(
            "SELECT payload FROM events WHERE plan_id=? AND kind='reopen_attempt' AND sequence>?", (plan.plan_id, sequence)):
        event = attempts._object(raw, "reopen publication")
        if event.get("attempt_id") == row[0]:
            raise DomainError("invalid", "The checkpoint's submission ended at an explicit reopen boundary")
    cycle = {"kind": "submission", "plan_id": plan.plan_id, "step_id": step.step_id, "attempt_id": row[0],
             "definition_version": row[5], "checkpoint_sequence": sequence}
    return artifact, cycle


def _submitted_row(connection, plan, step_id):
    current = attempts.current_attempt(connection, plan.plan_id, step_id)
    row = attempts._attempt_row(connection, plan.plan_id, current.attempt_id)
    data = attempts._object(row[7], "review attempt")
    step = next((s for s in plan.steps if s.step_id == step_id), None)
    if step is None or data.get("definition") != step.definition or row[5] != step.definition["definition_version"]:
        raise DomainError("stale", "The review target's full frozen definition has changed")
    if data.get("epoch") != plans._installation_epoch(connection) or data.get("generation") != plan.generation:
        raise DomainError("fenced", "Submitted review target is not current in this epoch and generation")
    if "cancelled_from" in data:
        raise DomainError("fenced", "The review target has been cancelled")
    if row[3] == "stale":
        raise DomainError("stale", "The review target requires recovery")
    if row[3] != "submitted" or step.state not in ("in-progress", "blocked"):
        raise DomainError("invalid", "A current submitted attempt is required for artifact review")
    if data.get("recovery_required"):
        raise DomainError("held", "The review target requires recovery")
    count = connection.execute(
        "SELECT COUNT(*) FROM capabilities WHERE epoch=? AND plan_id=? AND role='attempt' AND revoked=0 "
        "AND valid_until IS NULL AND json_valid(payload) AND json_extract(payload,'$.attempt_id')=? "
        "AND json_extract(payload,'$.generation')=?", (plans._installation_epoch(connection), plan.plan_id, row[0], plan.generation)).fetchone()[0]
    if count != 1:
        raise DomainError("fenced", "Submitted target has no unique current bound capability")
    return row, data, step


def _accepted_cycle(connection, plan, step_id, attempt_id, definition, result_version, *, boundary=None):
    applicable, open_ids = [], []
    for sequence, raw in connection.execute(
            "SELECT sequence,payload FROM events WHERE plan_id=? AND kind='invalidate_result' ORDER BY sequence", (plan.plan_id,)):
        if boundary is not None and sequence > boundary:
            continue
        data = attempts._object(raw, "invalidation history")
        producer = data.get("producer_step_id", data.get("step_id"))
        affected = data.get("affected_steps")
        if producer == step_id:
            relevant = data.get("result_version") == result_version
        else:
            relevant = isinstance(affected, list) and step_id in affected
        if not relevant:
            continue
        identity = data.get("invalidation_id")
        linked_sequence, _, linked = checkpoints._linked_invalidation(connection, plan, identity)
        if linked_sequence != sequence:
            raise DomainError("invalid", "Applicable invalidation history is ambiguous")
        if producer != step_id:
            definitions = linked.get("affected_definitions")
            if (not isinstance(definitions, dict) or definitions.get(step_id) != definition
                    or not checkpoints._invalid_path(definitions, step_id, producer)):
                raise DomainError("invalid", "Consumer invalidation contradicts its frozen accepted scope")
        applicable.append({"sequence": sequence, "invalidation_id": identity, "producer_step_id": producer,
                           "result_version": linked["result_version"], "affected_step_id": step_id,
                           "affected_definition": definition})
        holds = checkpoints._source_holds(connection, plan.plan_id, "invalidation", identity,
                                          ... if producer == step_id else step_id)
        if holds:
            open_ids.append(identity)
    if not applicable or (boundary is not None and applicable[-1]["sequence"] != boundary):
        raise DomainError("invalid", "Accepted artifact has no exact applicable invalidation boundary")
    cycle = {"kind": "accepted-invalidation", "plan_id": plan.plan_id, "step_id": step_id,
             "attempt_id": attempt_id, "definition_version": definition["definition_version"],
             "result_version": result_version, "boundary": applicable[-1]["sequence"], "invalidations": applicable}
    return cycle, open_ids


def _resolve_review_target(connection, plan, step_id, *, require_open=True):
    step = next((s for s in plan.steps if s.step_id == step_id), None)
    if step is None:
        raise DomainError("not_found", "The review step does not exist in this plan")
    if step.state != "complete":
        row, data, step = _submitted_row(connection, plan, step_id)
        artifact, cycle = _submission(connection, plan, row, data, step)
        return _Target(plan, row, data, step, artifact, cycle)
    candidates = []
    for (version,) in connection.execute("SELECT result_version FROM result_versions WHERE plan_id=? AND step_id=?",
                                         (plan.plan_id, step_id)):
        artifact, aid, definition, _ = attempts._accepted_history(connection, plan, step_id, version)
        cycle, open_ids = _accepted_cycle(connection, plan, step_id, aid, definition, version)
        if not require_open or open_ids:
            row = attempts._attempt_row(connection, plan.plan_id, aid)
            candidates.append(_Target(plan, row, attempts._object(row[7], "accepted review target"), step, artifact, cycle, version))
    if len(candidates) != 1:
        raise DomainError("invalid", "Accepted review requires one exact accepted target with an open applicable invalidation")
    return candidates[0]


def _validate_review_cycle(connection, source, cycle, supplied_artifact, before_sequence):
    """Validate immutable historical group binding without granting current execution authority."""
    if not isinstance(cycle, dict):
        raise DomainError("invalid", "Verifier review cycle is malformed")
    scoped = {"plan_id": source["plan_id"], "step_id": source["step_id"], "attempt_id": source["attempt_id"],
              "definition_version": source["definition_version"]}
    if any(cycle.get(key) != value for key, value in scoped.items()):
        raise DomainError("invalid", "Verifier review cycle belongs to another frozen target")
    artifact = attempts._artifact(supplied_artifact)
    if cycle.get("kind") == "submission":
        if set(cycle) != {"kind", *scoped, "checkpoint_sequence"} or type(cycle["checkpoint_sequence"]) is not int:
            raise DomainError("invalid", "Verifier submission cycle is malformed")
        sequence = cycle["checkpoint_sequence"]
        value, _ = checkpoints._linked_checkpoint(connection, source["plan_id"], str(sequence))
        event = attempts._object(connection.execute("SELECT payload FROM events WHERE sequence=? AND plan_id=?",
                                                    (sequence, source["plan_id"])).fetchone()[0], "verifier checkpoint")
        if (value.artifact != artifact or any(event.get(key) != val for key, val in scoped.items() if key != "plan_id")
                or event.get("definition") != source["definition"]):
            raise DomainError("invalid", "Verifier artifact contradicts its original checkpoint cycle")
    elif cycle.get("kind") == "accepted-invalidation":
        if set(cycle) != {"kind", *scoped, "result_version", "boundary", "invalidations"} or type(cycle["boundary"]) is not int:
            raise DomainError("invalid", "Verifier invalidation cycle is malformed")
        plan = plans._load_plan(connection, source["plan_id"])
        accepted, aid, definition, _ = attempts._accepted_history(connection, plan, source["step_id"], cycle["result_version"])
        expected, _ = _accepted_cycle(connection, plan, source["step_id"], aid, definition,
                                      cycle["result_version"], boundary=cycle["boundary"])
        if accepted != artifact or aid != source["attempt_id"] or definition != source["definition"] or cycle != expected:
            raise DomainError("invalid", "Verifier artifact contradicts accepted invalidation history")
        sequence = cycle["boundary"]
    else:
        raise DomainError("invalid", "Verifier review cycle is unknown")
    if sequence >= before_sequence:
        raise DomainError("invalid", "Verifier creation precedes its claimed artifact cycle")


def _assert_target(command, target):
    assertions = ((command.attempt_id, target.row[0]), (command.attempt_version, target.row[4]),
                  (command.definition_version, target.row[5]))
    if any(supplied is not None and supplied != actual for supplied, actual in assertions):
        raise DomainError("stale", "The supplied attempt assertions contradict the exact review target")


def _group_reference(connection, target, anchor, reviewer):
    namespace, _ = resources.execution_uses(connection, target.plan.plan_id, target.row[0])
    group = namespace["groups"].get(anchor)
    if (group is None or group["verifier"] != reviewer or group["cycle"] != target.cycle
            or group["artifact"] != plans.to_jsonable(target.artifact) or group["definition"] != target.step.definition):
        raise DomainError("invalid", "Review does not cite its exact original verifier anchor, actor and artifact cycle")


def _review_content(review, target):
    checkpoints._text(review.reviewer, "Reviewer label")
    if review.reviewer == target.row[2] or attempts._artifact(review.artifact) != target.artifact:
        raise DomainError("invalid", "Review requires a distinct recorded reviewer and the exact current full artifact")
    if len(review.findings) > MAX_EVIDENCE_ITEMS:
        raise DomainError("limit_exceeded", "Review findings exceed the 50-item limit")
    for finding in review.findings:
        checkpoints._text(finding, "Review finding")
    evidence = checkpoints._evidence(review.evidence) if review.evidence else ()
    if any(item.artifact is not None and item.artifact != target.artifact for item in evidence):
        raise DomainError("invalid", "Review evidence contradicts its exact artifact")
    if review.verdict == "passed" and not attempts._passing_evidence(evidence, target.artifact):
        raise DomainError("invalid", "Passing review requires nonempty exact artifact-bound blocker-free passed evidence")
    if review.verdict == "failed" and not (review.findings or evidence):
        raise DomainError("invalid", "Failed review requires meaningful retained findings or evidence")
    checkpoints._event_limits(review)


def _links(target):
    return {"attempt_id": target.row[0], "step_id": target.step.step_id, "definition_version": target.row[5],
            "definition": target.step.definition, "artifact": plans.to_jsonable(target.artifact),
            "cycle": target.cycle, **({"result_version": target.result_version} if target.result_version is not None else {})}


def record_review(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "record_review")
    target = _resolve_review_target(connection, plan, command.payload["step_id"])
    _assert_target(command, target)
    review = _to_review(command.payload["review"])
    _review_content(review, target)
    if review.verification_use_id is not None:
        _group_reference(connection, target, review.verification_use_id, review.reviewer)
    previous = plans.project_plan(connection, plan.plan_id)
    identity, revision, ts = str(uuid.uuid4()), plan.revision + 1, plans._timestamp(plan.updated_at)
    connection.execute("INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,artifact,verification_use_id,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (identity, plan.plan_id, target.row[0], target.step.step_id, review.reviewer, review.verdict,
                        json.dumps(plans.to_jsonable(review.findings), ensure_ascii=False),
                        json.dumps(plans.to_jsonable(review.evidence), sort_keys=True, ensure_ascii=False),
                        json.dumps(plans.to_jsonable(review.artifact), sort_keys=True, ensure_ascii=False), review.verification_use_id, ts))
    if review.verdict == "failed" and target.result_version is None:
        connection.execute("UPDATE steps SET state='blocked' WHERE plan_id=? AND step_id=?", (plan.plan_id, target.step.step_id))
    event = {**_links(target), "review_id": identity, "reviewer": review.reviewer,
             "verification_use_id": review.verification_use_id, "verdict": review.verdict}
    checkpoints._event_limits(event)
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"review_id": identity, "attempt_id": target.row[0], "verdict": review.verdict},
                           view.metadata, view.metadata, False, None)


def _selected_review(connection: sqlite3.Connection, target: _Target) -> tuple[str, int] | None:
    """Return the original successful review pointer under the current verdict policy."""
    try:
        plan_id, step_id, attempt_id = target.plan.plan_id, target.step.step_id, target.row[0]
        rows = {row[0]: row for row in connection.execute(
            "SELECT review_id,reviewer,verdict,artifact,evidence,findings,verification_use_id FROM reviews "
            "WHERE plan_id=? AND step_id=? AND attempt_id=?", (plan_id, step_id, attempt_id))}
        latest, seen = None, set()
        for sequence, raw in connection.execute("SELECT sequence,payload FROM events WHERE plan_id=? ORDER BY sequence", (plan_id,)):
            event = attempts._object(raw, "review provenance")
            identity = event.get("review_id")
            if not isinstance(identity, str) or identity not in rows or identity in seen:
                continue
            seen.add(identity)
            row = rows[identity]
            try:
                supplied = attempts._artifact(json.loads(row[3]))
                if (supplied != target.artifact or not attempts._nonblank(row[1]) or row[2] not in ("passed", "failed")
                        or event.get("cycle") != target.cycle or any(event.get(key) != val for key, val in _links(target).items())
                        or not attempts._audit_links(event, step_id=step_id, attempt_id=attempt_id,
                                                     result_version=target.result_version, artifact=target.artifact,
                                                     definition=target.step.definition)
                        or ("plan_id" in event and event["plan_id"] != plan_id)
                        or ("checkpoint_sequence" in event and event["checkpoint_sequence"] != target.cycle.get("checkpoint_sequence"))
                        or ("reviewer" in event and event["reviewer"] != row[1])
                        or ("verdict" in event and event["verdict"] != row[2])
                        or ("verification_use_id" in event and event["verification_use_id"] != row[6])):
                    continue
                if row[6] is not None:
                    _group_reference(connection, target, row[6], row[1])
                    namespace, _ = resources.execution_uses(connection, plan_id, attempt_id)
                    if namespace["groups"][row[6]]["creation"]["sequence"] >= sequence:
                        continue
                latest = row, sequence
            except (DomainError, ValueError, TypeError, KeyError):
                continue
        if latest is None or latest[0][2] != "passed":
            return None
        row, sequence = latest
        selected = _to_review({"reviewer": row[1], "verdict": row[2], "artifact": json.loads(row[3]),
                               "evidence": json.loads(row[4]), "findings": json.loads(row[5]), "verification_use_id": row[6]})
        _review_content(selected, target)
        return row[0], sequence
    except (DomainError, ValueError, TypeError, KeyError, RecursionError):
        return None


def review_satisfied(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId, attempt_id: AttemptId,
                     definition_version: DefinitionVersion, artifact: ArtifactRef) -> bool:
    """Select the latest original exact-cycle verdict before testing successful review evidence."""
    try:
        target = _resolve_review_target(connection, plans._load_plan(connection, plan_id), step_id, require_open=False)
        if target.row[0] != attempt_id or target.row[5] != definition_version or target.artifact != attempts._artifact(artifact):
            return False
        return _selected_review(connection, target) is not None
    except (DomainError, ValueError, TypeError, KeyError, RecursionError):
        return False


def _store_hold(connection):
    epoch, hold = connection.execute("SELECT epoch,store_hold FROM installation WHERE id=1").fetchone()
    if hold or connection.execute("SELECT 1 FROM store_hold_reasons WHERE epoch=? AND resolved=0 LIMIT 1", (epoch,)).fetchone():
        raise DomainError("held", "Writable verification is held pending inventory resolution")


def reserve_verification(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "reserve_verification")
    execution = command.payload["context"]
    if not execution.writes:
        raise DomainError("invalid", "Read-only observations use record_review without a verification use")
    _store_hold(connection)
    target = _resolve_review_target(connection, plan, command.payload["step_id"])
    _assert_target(command, target)
    verifier = command.payload["verifier"]
    checkpoints._text(verifier, "Verifier label")
    if verifier == target.row[2]:
        raise DomainError("invalid", "Verifier and implementer must have distinct recorded labels")
    execution = resources.canonical_context(connection, execution)
    selected = resources.resolve_context(connection, execution)
    if not selected:
        raise DomainError("invalid", "Writable verification needs a registered resource set")
    namespace, owned = resources.execution_uses(connection, plan.plan_id, target.row[0])
    originals = [item["use_id"] for item in namespace["implementation"]["members"]]
    source, reason = command.payload["handoff_from"], command.payload["stopped_reason"]
    conflicts = set(resources.conflicting_uses(connection, selected, execution.repository_exclusive))
    source_ids = []
    if source is None:
        if reason is not None:
            raise DomainError("invalid", "Stopped handoff reason requires an original source use")
    else:
        checkpoints._text(reason, "Full implementation-cohort stopped confirmation")
        if (target.result_version is not None or source not in originals or not originals
                or any(owned[item][6] != "active" for item in originals) or source not in conflicts):
            raise DomainError("invalid", "Handoff requires its current retained implementation cohort and actual named overlap")
        source_ids = originals
        conflicts -= set(originals)
    if conflicts:
        raise DomainError("resource_conflict", "Writable verification overlaps a retained resource obligation")
    coordination = plans._validation_coordination(connection, plan.plan_id)
    allowed_invalidations = {item["invalidation_id"] for item in target.cycle.get("invalidations", ())}
    if (plan.draft_json.get("stop_reason") or plan.draft_json.get("block_reason")
            or any(h.step_id in (None, target.step.step_id) and not (h.kind == "invalidation" and h.source_id in allowed_invalidations)
                   for h in coordination.holds)):
        raise DomainError("held", "Verification has an unrelated unresolved plan hold")
    previous = plans.project_plan(connection, plan.plan_id)
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    members = []
    for rid in selected:
        scope = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (rid,)).fetchone()[0]
        use_id = str(uuid.uuid4())
        connection.execute("INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,scope,status,created_at) VALUES(?,?,?,?,?,'active',?)",
                           (use_id, rid, plan.plan_id, target.row[0], scope, ts))
        members.append({"use_id": use_id, "resource_id": rid, "scope": scope, "created_at": ts})
    anchor = members[0]["use_id"]
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, {"use_id": anchor})
    sequence = plans._store_sequence(connection)
    group = {"anchor": anchor, "members": members, "verifier": verifier, "context": plans.to_jsonable(execution),
             "artifact": plans.to_jsonable(target.artifact), "cycle": target.cycle, "definition": target.step.definition,
             "source_use_ids": source_ids,
             "creation": {"sequence": sequence, "epoch": command.epoch, "generation": plan.generation,
                          "ownership_version": command.ownership_version, "actor": command.actor, "published_at": ts}, "writer": None}
    namespace["groups"][anchor] = group
    transitions = []
    if source_ids:
        transitions.append(resources.writer_transition(namespace, "implementation", None, "stopped", command, plan, sequence, reason))
    transitions.append(resources.writer_transition(namespace, "verification", anchor, "active", command, plan, sequence, "Declared writable verification"))
    event = {**_links(target), "use_id": anchor, "verifier": verifier, "handoff_from": source,
             "stopped_reason": reason, "verification_group": {key: val for key, val in group.items() if key != "writer"},
             "execution_transitions": transitions}
    checkpoints._event_limits(event)
    connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                       (json.dumps(event, sort_keys=True, ensure_ascii=False), plan.plan_id, sequence))
    resources.save_execution_uses(connection, plan.plan_id, target.row[0], namespace)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"use_id": anchor, "attempt_id": target.row[0], "resource_ids": list(selected)}, view.metadata, view.metadata, False, None)


def _implementation_release_ids(connection, namespace, owned, canonical_ids):
    """Validate stopped original rows without publishing or stopping another actor."""
    selected = [item["use_id"] for item in namespace["implementation"]["members"]
                if resources.resolve_resource_id(connection, owned[item["use_id"]][1]) in canonical_ids]
    if {resources.resolve_resource_id(connection, owned[uid][1]) for uid in selected} != canonical_ids:
        raise DomainError("invalid", "Stopped resources must belong to the exact original implementation cohort")
    for uid in selected:
        row = owned[uid]
        identity = resources.resolve_resource_id(connection, row[1])
        conflicts = set(resources.conflicting_uses(connection, (identity,), row[5] == "repository"))
        if any(uid in group["source_use_ids"] and any(item["use_id"] in conflicts for item in group["members"])
               for group in namespace["groups"].values()):
            raise DomainError("resource_conflict", "Linked verifier obligations must be stopped and released before the parent guard")
    return selected


def _release_rows(connection, use_ids):
    for use_id in use_ids:
        connection.execute("UPDATE resource_uses SET status='released' WHERE use_id=?", (use_id,))


def release_resource_use(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan = checkpoints._owner_target(connection, command, "release_resource_use")
    payload = command.payload
    checkpoints._text(payload["reason"], "Stopped-use observation reason")
    row = connection.execute("SELECT use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status FROM resource_uses WHERE use_id=?",
                             (payload["use_id"],)).fetchone()
    if row is None:
        raise DomainError("not_found", "Resource use does not exist")
    if row[2] is not None and row[2] != plan.plan_id:
        raise DomainError("forbidden", "Owned resource release requires its own plan owner")
    supplied = {resources.resolve_resource_id(connection, rid) for rid in payload["resource_ids"]}
    if not supplied:
        raise DomainError("invalid", "Release requires the exact nonempty current canonical resource set")
    namespace, group, ids = None, None, [row[0]]
    if row[2] is None:
        if any(value is not None for value in (command.attempt_id, command.attempt_version, command.definition_version)):
            raise DomainError("invalid", "Orphan release has no owned attempt target for supplied assertions")
        orphan = connection.execute("SELECT resource_id,host_id FROM absent_host_work WHERE absent_work_id=? AND scope=?",
                                    (row[4], row[5])).fetchone()
        host = connection.execute("SELECT host_id FROM installation WHERE id=1").fetchone()[0]
        if row[3] is not None or orphan is None or orphan[1] != host or resources.resolve_resource_id(connection, orphan[0]) != resources.resolve_resource_id(connection, row[1]):
            raise DomainError("invalid", "Release has no exact coherent orphan ownership proof")
        expected = {resources.resolve_resource_id(connection, row[1])}
    else:
        namespace, owned = resources.execution_uses(connection, plan.plan_id, row[3])
        attempt = attempts._attempt_row(connection, plan.plan_id, row[3])
        assertions = ((command.attempt_id, attempt[0]), (command.attempt_version, attempt[4]),
                      (command.definition_version, attempt[5]))
        if any(supplied is not None and supplied != actual for supplied, actual in assertions):
            raise DomainError("stale", "The supplied attempt assertions contradict the exact resource-use owner")
        originals = {item["use_id"] for item in namespace["implementation"]["members"]}
        if row[0] in originals:
            expected = {resources.resolve_resource_id(connection, row[1])}
            if payload["stopped"]:
                _implementation_release_ids(connection, namespace, owned, expected)
        elif row[0] in namespace["groups"]:
            group = namespace["groups"][row[0]]
            ids = [item["use_id"] for item in group["members"]]
            expected = {resources.resolve_resource_id(connection, owned[item][1]) for item in ids}
        else:
            raise DomainError("invalid", "Verifier release must name its real immutable group anchor")
    if supplied != expected:
        raise DomainError("invalid", "Release must name the entire exact current canonical resource set")
    previous = plans.project_plan(connection, plan.plan_id)
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    event = {"use_id": row[0], "resource_ids": list(payload["resource_ids"]), "stopped": payload["stopped"],
             "reason": payload["reason"], "use_ids": ids}
    if namespace is not None:
        event.update({"attempt_id": row[3], "step_id": namespace["source"]["step_id"],
                      "definition_version": namespace["source"]["definition_version"], "definition": namespace["source"]["definition"]})
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, event)
    sequence = plans._store_sequence(connection)
    if payload["stopped"]:
        if group is not None:
            event["execution_transitions"] = [resources.writer_transition(namespace, "verification", row[0], "stopped",
                                                                           command, plan, sequence, payload["reason"])]
            connection.execute("UPDATE events SET payload=? WHERE plan_id=? AND sequence=?",
                               (json.dumps(event, sort_keys=True, ensure_ascii=False), plan.plan_id, sequence))
            resources.save_execution_uses(connection, plan.plan_id, row[3], namespace)
        _release_rows(connection, ids)
    connection.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt({"use_id": row[0], "stopped": payload["stopped"], "released": payload["stopped"]},
                           view.metadata, view.metadata, False, None)
