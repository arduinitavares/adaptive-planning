"""Explicit legacy authority handoff and recovery import."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import sqlite3
import uuid

from adaptive_planning import attempts, checkpoints, plans, recovery, resources
from adaptive_planning.core import validate_published_snapshot, PlanValidationError
from adaptive_planning.ownership import current_private, hash_token, issue_orchestrator
from adaptive_planning.types import (
    Authorization,
    Command,
    DocumentRef,
    DomainError,
    ExecutionContext,
    ExecutionContextClaims,
    JSONValue,
    Metadata,
    MutationReceipt,
    PlanDraft,
    PlanId,
    ResourceClaim,
    ResourceId,
    StepDefinition,
    StepId,
    _to_execution_context,
    _to_resource_claim,
)
from typing import Any, Mapping


def _unfreeze_json(val: Any) -> Any:
    if isinstance(val, (list, tuple)):
        return [_unfreeze_json(x) for x in val]
    if isinstance(val, (dict, Mapping)):
        return {k: _unfreeze_json(v) for k, v in val.items()}
    return val


def resolve_context_claims(
    connection: sqlite3.Connection,
    context: ExecutionContextClaims | ExecutionContext,
) -> ExecutionContext:
    """Resolve context claims repository-first into canonical ResourceIds."""
    if isinstance(context, ExecutionContext):
        return resources.canonical_context(connection, context)

    if not isinstance(context, ExecutionContextClaims):
        raise DomainError("invalid", "Context must be ExecutionContext or ExecutionContextClaims")

    repo_id: ResourceId | None = None
    if context.repository_claim is not None:
        if context.repository_claim.kind != "repository":
            raise DomainError("invalid", "repository_claim must be repository kind")
        repo_id = resources.register_claim(connection, context.repository_claim)

    ws_id: ResourceId | None = None
    if context.workspace_claim is not None:
        ws_claim = context.workspace_claim
        if ws_claim.kind != "workspace":
            raise DomainError("invalid", "workspace_claim must be workspace kind")
        if ws_claim.repository_id is not None and repo_id is not None and ws_claim.repository_id != repo_id:
            raise DomainError("invalid", "Contradictory repository parent in workspace claim")
        if repo_id is not None and ws_claim.repository_id is None:
            ws_claim = replace(ws_claim, repository_id=repo_id)
        ws_id = resources.register_claim(connection, ws_claim)

    ext_ids: list[ResourceId] = []
    for ext_claim in context.external_claims:
        if ext_claim.kind != "external":
            raise DomainError("invalid", "external_claim must be external kind")
        ext_ids.append(resources.register_claim(connection, ext_claim))

    return resources.canonical_context(
        connection,
        ExecutionContext(
            kind=context.kind,
            repository_id=repo_id,
            workspace_id=ws_id,
            repository_exclusive=context.repository_exclusive,
            external_ids=tuple(ext_ids),
            writes=context.writes,
            checked_base=context.checked_base,
        ),
    )


def import_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    """Import an authorized legacy plan with explicit publisher authority handoff."""
    epoch = plans._installation_epoch(connection)
    if command.epoch != epoch:
        raise DomainError("stale_epoch", "The installation epoch has changed; re-read service info")

    host_id = connection.execute("SELECT host_id FROM installation WHERE id = 1").fetchone()[0]
    payload = command.payload

    if payload["host_id"] != host_id:
        raise DomainError("invalid", f"Host ID {payload['host_id']!r} does not match installation {host_id!r}")

    s_dir_id = payload["source_directory_id"]
    s_dir_claim = payload["source_directory_claim"]
    if (s_dir_id is not None) == (s_dir_claim is not None):
        raise DomainError("invalid", "Import requires exactly one of source_directory_id or source_directory_claim")

    writers_stopped = payload.get("writers_stopped")
    if not isinstance(writers_stopped, str) or not writers_stopped.strip():
        raise DomainError("invalid", "writers_stopped must be a bounded non-empty string")

    original_snapshot_raw = payload.get("original_snapshot")
    if not isinstance(original_snapshot_raw, (dict, Mapping)):
        raise DomainError("invalid", "original_snapshot must be a dictionary")
    original_snapshot = _unfreeze_json(original_snapshot_raw)

    if original_snapshot.get("schemaVersion") != 2:
        raise DomainError("invalid", "Only valid v2 snapshot is supported; v1 must be migrated first")

    if original_snapshot.get("taskId") != payload.get("original_task_id"):
        raise DomainError("invalid", "original_task_id does not match snapshot taskId")

    try:
        validate_published_snapshot(original_snapshot)
    except PlanValidationError as error:
        raise DomainError("invalid", f"Invalid legacy snapshot: {error}") from error
    except Exception as error:
        raise DomainError("invalid", f"Malformed snapshot: {error}") from error

    canonical_snapshot_bytes = plans.canonical_bytes(original_snapshot)
    if len(canonical_snapshot_bytes) > plans.MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Snapshot exceeds the 10MB limit")
    content_digest = hashlib.sha256(canonical_snapshot_bytes).hexdigest()

    # Resolve source directory ID
    if s_dir_claim is not None:
        if s_dir_claim.kind != "workspace":
            raise DomainError("invalid", "Source directory claim must be workspace kind")
        if s_dir_claim.host_id != host_id:
            raise DomainError("invalid", "Source directory claim host_id does not match installation")
        if s_dir_claim.device is None or s_dir_claim.inode is None:
            raise DomainError("invalid", "New source directory claim requires physical device and inode")
        resolved_dir_id = resources.register_claim(connection, s_dir_claim)
    else:
        row = connection.execute("SELECT host_id, kind FROM resources WHERE resource_id = ?", (s_dir_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"Source directory resource {s_dir_id!r} not found")
        if row[0] != host_id or row[1] != "workspace":
            raise DomainError("invalid", "Source directory resource must be a workspace kind on current host")
        resolved_dir_id = s_dir_id

    canonical_dir_id = resources.resolve_resource_id(connection, resolved_dir_id)

    # Check for existing imports matching (host, canonical_dir, original_task_id)
    existing_rows = connection.execute(
        "SELECT import_id, source_directory_id, plan_id, content_digest FROM imports WHERE host_id = ? AND original_task_id = ?",
        (host_id, payload["original_task_id"]),
    ).fetchall()

    matching_imports = []
    for r in existing_rows:
        stored_canonical = resources.resolve_resource_id(connection, r[1])
        if stored_canonical == canonical_dir_id:
            matching_imports.append(r)

    if len(matching_imports) > 1:
        distinct_plans = {r[2] for r in matching_imports}
        if len(distinct_plans) > 1:
            raise DomainError("handoff_conflict", "Ambiguous multiple mappings collapsed to canonical key")

    if matching_imports:
        existing = matching_imports[0]
        existing_plan_id, existing_digest = existing[2], existing[3]
        if existing_digest != content_digest:
            raise DomainError("handoff_conflict", "Snapshot content has changed after handoff")

        evt = connection.execute(
            "SELECT sequence, payload FROM events WHERE plan_id = ? AND kind = 'import_plan'",
            (existing_plan_id,),
        ).fetchone()
        if evt is None:
            raise DomainError("invalid", "Missing origin import_plan event for existing imported plan")
        origin_payload = json.loads(evt[1])
        req_epoch = origin_payload.get("request_epoch")
        req_key = origin_payload.get("request_key")
        req_row = connection.execute(
            "SELECT response_payload FROM requests WHERE epoch = ? AND key = ?",
            (req_epoch, req_key),
        ).fetchone()
        if req_row is None:
            raise DomainError("invalid", "Stored original request receipt is missing")
        committed, committed_metadata, private = plans.decode_response(req_row[0])
        current_meta = plans._metadata(connection, existing_plan_id)
        current_grant = current_private(connection, existing_plan_id, private)
        return MutationReceipt(committed, committed_metadata, current_meta, command.key == req_key, current_grant)

    # Validate known_contexts
    known_contexts = payload.get("known_contexts", {})
    if not isinstance(known_contexts, (dict, Mapping)):
        raise DomainError("invalid", "known_contexts must be a dictionary")

    in_progress_step_ids = {
        s["id"] for s in original_snapshot.get("steps", []) if s.get("status") == "in-progress"
    }
    for k in known_contexts:
        if k not in in_progress_step_ids:
            raise DomainError("invalid", f"Irrelevant known context key {k!r}")

    # Build new plan
    plan_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    ts = plans._timestamp()
    import_id = str(uuid.uuid4())
    source_revision = original_snapshot.get("revision", 0)

    scope_info = original_snapshot.get("scope", {})
    since_revision = scope_info.get("sinceRevision", 0)
    mode = scope_info.get("mode", original_snapshot.get("executionMode", "first-step"))
    baseline = tuple(scope_info.get("baseline", ()))
    claimed = scope_info.get("claimed")
    interval = "legacy:" + hashlib.sha256(
        plans.canonical_bytes([host_id, resolved_dir_id, original_snapshot["taskId"], original_snapshot.get("generation"), since_revision])
    ).hexdigest()

    note = ""
    for entry in original_snapshot.get("history", []):
        if entry.get("revision") == since_revision and entry.get("authorizationNote"):
            note = entry["authorizationNote"]
            break

    authorization = Authorization(
        mode=mode,
        interval=interval,
        since_revision=since_revision,
        note=note,
        baseline=baseline,
        claimed=claimed,
    )

    # Step definitions
    step_defs: list[tuple[StepDefinition, dict[str, JSONValue], int]] = []
    for ordinal, s_dict in enumerate(original_snapshot.get("steps", [])):
        s_id = s_dict["id"]
        def_ver = "legacy:" + hashlib.sha256(
            plans.canonical_bytes([host_id, resolved_dir_id, payload["original_task_id"], original_snapshot.get("generation"), source_revision, s_id])
        ).hexdigest()
        s_def = StepDefinition(
            step_id=s_id,
            title=s_dict.get("title", ""),
            dependencies=tuple(s_dict.get("dependsOn", ())),
            completion_check=s_dict.get("check", ""),
            document=None,
            definition_version=def_ver,
            review_required=False,
            final_gate=False,
        )
        step_defs.append((s_def, s_dict, ordinal))

    # Next check
    next_step_id = original_snapshot.get("nextStepId")
    next_check = ""
    if next_step_id:
        for s_def, s_dict, _ in step_defs:
            if s_dict["id"] == next_step_id:
                next_check = s_dict.get("check", "")
                break

    snap_status = original_snapshot.get("status")
    stop_reason = "Imported legacy stopped status" if snap_status == "stopped" else None
    block_reason = "Imported legacy blocked status" if snap_status == "blocked" else None

    plan_draft = PlanDraft(
        objective=original_snapshot["objective"],
        acceptance_criteria=(),
        constraints=(),
        facts=(),
        assumptions=(),
        next_check=next_check,
        authorization=authorization,
        steps=tuple(sd for sd, _, _ in step_defs),
        documents=(payload["provenance"],) if payload.get("provenance") else (),
        workspace_refs=(),
        workflow=None,
        horizon=10,
        stop_reason=stop_reason,
        block_reason=block_reason,
    )

    connection.execute(
        "INSERT INTO plans (plan_id, generation, revision, status, title, ownership_version, created_at, updated_at, payload) "
        "VALUES (?, ?, ?, 'planning', ?, 1, ?, ?, ?)",
        (
            plan_id,
            generation,
            source_revision,
            plan_draft.objective,
            ts,
            ts,
            json.dumps({"draft": plans._draft_json(plan_draft, authorization)}, sort_keys=True, ensure_ascii=False),
        ),
    )

    for s_def, s_dict, ordinal in step_defs:
        s_state = s_dict.get("status", "pending")
        evidence = s_dict.get("evidence", [])
        retired_reason = s_dict.get("retiredReason")
        step_obj = plans._Step(s_def.step_id, s_state, plans.to_jsonable(s_def), list(evidence), retired_reason, ordinal)
        connection.execute(
            "INSERT INTO steps (plan_id, step_id, title, state, definition_version, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, s_def.step_id, s_def.title, s_state, s_def.definition_version, plans._step_payload(step_obj)),
        )

    # In-progress unresolved attempts
    for s_def, s_dict, _ in step_defs:
        if s_dict.get("status") == "in-progress":
            raw_ctx = known_contexts.get(s_def.step_id)
            ctx = resolve_context_claims(connection, raw_ctx) if raw_ctx is not None else None
            recovery.create_unresolved_attempt(connection, plan_id, s_def.step_id, ctx, source=f"import:{import_id}")

    grant = issue_orchestrator(connection, plan_id, command.actor)

    event_payload = {
        "import_id": import_id,
        "request_epoch": command.epoch,
        "request_key": command.key,
        "host_id": host_id,
        "source_directory_id": resolved_dir_id,
        "original_task_id": payload["original_task_id"],
        "content_digest": content_digest,
        "original_snapshot": original_snapshot,
        "provenance": plans.to_jsonable(payload["provenance"]),
        "writers_stopped": writers_stopped,
        "authorization_renewed": False,
    }
    if len(plans.canonical_bytes(event_payload)) > plans.MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Import origin event exceeds 10MB limit")
    plans._insert_event(connection, plan_id, source_revision, "import_plan", command.actor, ts, event_payload)

    connection.execute(
        "INSERT INTO imports (import_id, host_id, source_directory_id, original_task_id, plan_id, content_digest, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (import_id, host_id, resolved_dir_id, payload["original_task_id"], plan_id, content_digest, ts),
    )

    view = plans._commit_projection(connection, plan_id, None)
    committed = {"source_directory_id": resolved_dir_id}
    return MutationReceipt(committed, view.metadata, view.metadata, False, grant)
