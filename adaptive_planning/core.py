"""Pure planning domain rules, graph validators, scope decisions, and publication assembly.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
No I/O in core.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from typing import Any, Callable, Sequence
import uuid

from adaptive_planning.types import (
    Authorization,
    AuthorizationChange,
    CoordinationView,
    Generation,
    JSONValue,
    StepId,
    parse_command,
)


SCHEMA_VERSION = 2
VALID_TASK_STATUSES = {"planning", "running", "stopped", "blocked", "complete"}
VALID_EXECUTION_MODES = {"plan-only", "first-step", "complete-task"}
VALID_STEP_STATUSES = {"pending", "in-progress", "complete", "blocked", "retired"}

MAX_STEPS = 200
MAX_HISTORY = 100
MAX_TEXT_LEN = 10000
MAX_EVIDENCE_ITEMS = 50
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB limit

PUBLISHER_OWNED_FIELDS = {"scope", "generation", "revision", "history", "updatedAt"}


class PlanValidationError(Exception):
    """Raised when a plan snapshot fails schema or graph validation."""
    pass


def _utc_timestamp(value: Any) -> datetime:
    """Parse and validate UTC ISO timestamp string."""
    if not isinstance(value, str) or len(value) > 128:
        raise PlanValidationError("Published timestamps must be UTC ISO strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanValidationError("Invalid published timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PlanValidationError("Published timestamps must include UTC timezone")
    return parsed


def validate_proposal_schema(proposed: dict[str, Any]) -> None:
    """Validate that a proposal contains valid v2 schema fields and no publisher-owned fields."""
    if not isinstance(proposed, dict):
        raise PlanValidationError("Plan payload must be a JSON object")

    # Reject publisher-owned fields in proposed plan
    for field in PUBLISHER_OWNED_FIELDS:
        if field in proposed:
            raise PlanValidationError(f"Proposed plan must not contain publisher-managed field '{field}'")

    allowed_keys = {
        "schemaVersion", "taskId", "objective", "status", "executionMode",
        "nextStepId", "changeSummary", "steps"
    }
    unknown_keys = set(proposed.keys()) - allowed_keys
    if unknown_keys:
        raise PlanValidationError(f"Proposed plan contains unknown field(s): {sorted(unknown_keys)}")

    # schemaVersion
    if type(proposed.get("schemaVersion")) is not int or proposed.get("schemaVersion") != SCHEMA_VERSION:
        raise PlanValidationError(f"Invalid schemaVersion: expected {SCHEMA_VERSION}, got {proposed.get('schemaVersion')}")

    # taskId
    task_id = proposed.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        raise PlanValidationError("Field 'taskId' must be a non-empty string")
    if len(task_id) > MAX_TEXT_LEN:
        raise PlanValidationError("Field 'taskId' exceeds maximum length")

    # objective
    objective = proposed.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise PlanValidationError("Field 'objective' must be a non-empty string")
    if len(objective) > MAX_TEXT_LEN:
        raise PlanValidationError("Field 'objective' exceeds maximum length")

    # status
    task_status = proposed.get("status")
    if not isinstance(task_status, str) or task_status not in VALID_TASK_STATUSES:
        raise PlanValidationError(f"Invalid task status '{task_status}'. Allowed: {sorted(VALID_TASK_STATUSES)}")

    # executionMode
    exec_mode = proposed.get("executionMode")
    if not isinstance(exec_mode, str) or exec_mode not in VALID_EXECUTION_MODES:
        raise PlanValidationError(f"Invalid executionMode '{exec_mode}'. Allowed: {sorted(VALID_EXECUTION_MODES)}")

    # changeSummary
    change_summary = proposed.get("changeSummary")
    if not isinstance(change_summary, str) or not change_summary.strip():
        raise PlanValidationError("Field 'changeSummary' must be a non-empty string")
    if len(change_summary) > MAX_TEXT_LEN:
        raise PlanValidationError("Field 'changeSummary' exceeds maximum length")

    # steps
    steps = proposed.get("steps")
    if not isinstance(steps, list):
        raise PlanValidationError("Field 'steps' must be a list")
    if len(steps) > MAX_STEPS:
        raise PlanValidationError(f"Field 'steps' exceeds maximum limit of {MAX_STEPS} steps")


def validate_plan_steps_and_graph(
    proposed: dict[str, Any],
    *,
    legacy: bool = False,
    allow_service_in_progress: bool = False,
) -> None:
    """Validate step structures, DAG cycles, prerequisites, retirement rules, and nextStepId."""
    steps = proposed.get("steps", [])
    step_ids: list[str] = []
    steps_by_id: dict[str, dict[str, Any]] = {}

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise PlanValidationError(f"Step at index {idx} must be a JSON object")

        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise PlanValidationError(f"Step at index {idx} has invalid or empty 'id'")
        if len(sid) > 128:
            raise PlanValidationError(f"Step '{sid}' id exceeds maximum length of 128 characters")

        title = step.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PlanValidationError(f"Step '{sid}' has invalid or empty 'title'")
        if len(title) > MAX_TEXT_LEN:
            raise PlanValidationError(f"Step '{sid}' title exceeds maximum length")

        s_status = step.get("status")
        allowed_statuses = VALID_STEP_STATUSES - {"retired"} if legacy else VALID_STEP_STATUSES
        if not isinstance(s_status, str) or s_status not in allowed_statuses:
            raise PlanValidationError(f"Step '{sid}' has invalid status '{s_status}'. Allowed: {sorted(VALID_STEP_STATUSES)}")

        deps = step.get("dependsOn")
        if not isinstance(deps, list):
            raise PlanValidationError(f"Step '{sid}' field 'dependsOn' must be a list of step IDs")
        for dep in deps:
            if not isinstance(dep, str):
                raise PlanValidationError(f"Step '{sid}' has non-string dependency '{dep}'")
        if len(deps) != len(set(deps)):
            raise PlanValidationError(f"Step '{sid}' has duplicate dependencies in 'dependsOn'")
        if sid in deps:
            raise PlanValidationError(f"Step '{sid}' cannot depend on itself")

        check = step.get("check")
        if not isinstance(check, str):
            raise PlanValidationError(f"Step '{sid}' field 'check' must be a string")
        if len(check) > MAX_TEXT_LEN:
            raise PlanValidationError(f"Step '{sid}' check exceeds maximum length")

        evidence = step.get("evidence")
        if not isinstance(evidence, list):
            raise PlanValidationError(f"Step '{sid}' field 'evidence' must be a list of strings")
        if len(evidence) > MAX_EVIDENCE_ITEMS:
            raise PlanValidationError(f"Step '{sid}' evidence exceeds maximum limit of {MAX_EVIDENCE_ITEMS} items")
        for ev in evidence:
            if not isinstance(ev, str):
                raise PlanValidationError(f"Step '{sid}' has non-string evidence item '{ev}'")
            if len(ev) > MAX_TEXT_LEN:
                raise PlanValidationError(f"Step '{sid}' evidence item exceeds maximum length")

        # Retirement requires bounded non-empty retiredReason
        if s_status == "retired":
            ret_reason = step.get("retiredReason")
            if not isinstance(ret_reason, str) or not ret_reason.strip() or len(ret_reason) > MAX_TEXT_LEN:
                raise PlanValidationError(f"Step '{sid}' is marked 'retired' but has missing or invalid 'retiredReason'")

        # Optional summary and details validation
        if "summary" in step and step["summary"] is not None:
            if not isinstance(step["summary"], str) or len(step["summary"]) > MAX_TEXT_LEN:
                raise PlanValidationError(f"Step '{sid}' field 'summary' must be a string <= {MAX_TEXT_LEN} characters")
        if "details" in step and step["details"] is not None:
            if not isinstance(step["details"], str) or len(step["details"]) > MAX_TEXT_LEN:
                raise PlanValidationError(f"Step '{sid}' field 'details' must be a string <= {MAX_TEXT_LEN} characters")

        # Step completeness requires non-empty evidence
        if s_status == "complete":
            if not evidence or not any(ev.strip() for ev in evidence):
                raise PlanValidationError(f"Step '{sid}' is marked 'complete' but has no non-empty evidence")

        step_ids.append(sid)
        steps_by_id[sid] = step

    # Duplicate IDs check
    if len(step_ids) != len(set(step_ids)):
        raise PlanValidationError("Duplicate step IDs found in 'steps'")

    id_set = set(step_ids)

    # Missing dependencies check
    for sid, step in steps_by_id.items():
        for dep in step["dependsOn"]:
            if dep not in id_set:
                raise PlanValidationError(f"Step '{sid}' depends on unknown step '{dep}'")

    # DAG Cycle Detection (DFS)
    white, gray, black = 0, 1, 2
    colors = {sid: white for sid in id_set}

    def check_cycle(u: str) -> None:
        colors[u] = gray
        for dep in steps_by_id[u]["dependsOn"]:
            if colors[dep] == gray:
                raise PlanValidationError(f"Cycle detected in step dependencies involving '{u}' and '{dep}'")
            if colors[dep] == white:
                check_cycle(dep)
        colors[u] = black

    for sid in id_set:
        if colors[sid] == white:
            check_cycle(sid)

    # Prerequisites & Retirement dependency rules
    for sid, step in steps_by_id.items():
        s_status = step["status"]
        if s_status != "retired":
            for dep in step["dependsOn"]:
                dep_step = steps_by_id[dep]
                if dep_step["status"] == "retired":
                    raise PlanValidationError(f"Non-retired step '{sid}' cannot depend on retired step '{dep}'")

        if s_status in ("in-progress", "complete"):
            for dep in step["dependsOn"]:
                dep_step = steps_by_id[dep]
                if dep_step["status"] != "complete":
                    raise PlanValidationError(
                        f"Step '{sid}' is '{s_status}' but its prerequisite '{dep}' is not complete (status: '{dep_step['status']}')"
                    )

    task_status = proposed.get("status")
    exec_mode = proposed.get("executionMode")

    # nextStepId validation
    if "nextStepId" not in proposed:
        raise PlanValidationError("Missing required field 'nextStepId' (must be a step ID string or null)")
    next_step_id = proposed.get("nextStepId")
    if next_step_id is not None:
        if not isinstance(next_step_id, str):
            raise PlanValidationError("Field 'nextStepId' must be a string or null")
        if next_step_id not in id_set:
            raise PlanValidationError(f"Field 'nextStepId' references unknown step '{next_step_id}'")
        target_next = steps_by_id[next_step_id]
        forbidden_next = ("complete",) if legacy else ("complete", "retired", "blocked")
        if target_next["status"] in forbidden_next:
            raise PlanValidationError(
                f"Field 'nextStepId' cannot reference '{target_next['status']}' step '{next_step_id}'"
            )
        for dep in ([] if legacy else target_next["dependsOn"]):
            if steps_by_id[dep]["status"] != "complete":
                raise PlanValidationError(
                    f"nextStepId '{next_step_id}' prerequisite '{dep}' is not complete"
                )

    # Task status and step status consistency
    if task_status == "complete":
        if not steps:
            raise PlanValidationError("Task status is 'complete' but has no steps")
        for sid, step in steps_by_id.items():
            if step["status"] != "retired" and step["status"] != "complete":
                raise PlanValidationError(
                    f"Task is marked 'complete' but step '{sid}' is '{step['status']}'"
                )
        if proposed.get("nextStepId") is not None:
            raise PlanValidationError("Task is marked 'complete' but 'nextStepId' is not null")

    if not allow_service_in_progress and task_status in ("stopped", "blocked", "planning"):
        for sid, step in steps_by_id.items():
            if step["status"] == "in-progress":
                raise PlanValidationError(
                    f"Task status is '{task_status}' but step '{sid}' is 'in-progress'"
                )

    if exec_mode == "plan-only" and not legacy:
        if task_status not in ("planning", "blocked"):
            raise PlanValidationError(f"plan-only mode task status must be 'planning' or 'blocked', got '{task_status}'")
        for sid, step in steps_by_id.items():
            if step["status"] == "in-progress":
                raise PlanValidationError(f"plan-only mode cannot have in-progress step '{sid}'")


def _validate_cross_revision(proposed: dict[str, Any], existing: dict[str, Any]) -> None:
    """Enforce consistency constraints between revisions."""
    if proposed["taskId"] != existing.get("taskId"):
        raise PlanValidationError(
            f"Cannot change taskId: existing '{existing.get('taskId')}', proposed '{proposed['taskId']}'"
        )
    if proposed["objective"] != existing.get("objective"):
        raise PlanValidationError(
            f"Cannot change objective: existing '{existing.get('objective')}', proposed '{proposed['objective']}'"
        )

    steps_by_id = {s["id"]: s for s in proposed.get("steps", [])}
    existing_steps = existing.get("steps", [])

    for old_step in existing_steps:
        old_id = old_step.get("id")
        old_status = old_step.get("status")
        old_evidence = old_step.get("evidence", [])

        # Previously completed steps are immutable
        if old_status == "complete":
            if old_id not in steps_by_id:
                raise PlanValidationError(f"Previously completed step '{old_id}' cannot be removed")
            new_step = steps_by_id[old_id]
            if new_step["status"] != "complete":
                raise PlanValidationError(f"Previously completed step '{old_id}' cannot be marked incomplete")
            for field in ("title", "check"):
                if new_step[field] != old_step[field]:
                    raise PlanValidationError(f"Previously completed step '{old_id}' cannot change '{field}'")
            if new_step["dependsOn"] != old_step["dependsOn"]:
                raise PlanValidationError(f"Previously completed step '{old_id}' cannot change dependencies")

        # Terminal retired steps cannot be deleted or reactivated
        if old_status == "retired":
            if old_id not in steps_by_id:
                raise PlanValidationError(f"Terminal retired step '{old_id}' cannot be removed")
            new_step = steps_by_id[old_id]
            if new_step["status"] != "retired":
                raise PlanValidationError(f"Terminal retired step '{old_id}' cannot change status from retired")

        # Evidence-bearing steps cannot disappear
        if old_evidence:
            if old_id not in steps_by_id:
                raise PlanValidationError(f"Evidence-bearing step '{old_id}' cannot be removed")
            new_step = steps_by_id[old_id]
            # Once a step has evidence, check is fixed
            if new_step["check"] != old_step["check"]:
                raise PlanValidationError(f"Step '{old_id}' has evidence; its check is fixed and cannot be changed")

        # Evidence must be ordered prefix append-only on every surviving step
        if old_id in steps_by_id:
            new_step = steps_by_id[old_id]
            new_evidence = new_step.get("evidence", [])
            if len(new_evidence) < len(old_evidence) or new_evidence[:len(old_evidence)] != old_evidence:
                raise PlanValidationError(
                    f"Evidence for step '{old_id}' must be an ordered prefix append-only extension"
                )

            # Retirement only from pending or blocked
            if new_step["status"] == "retired" and old_status != "retired":
                if old_status not in ("pending", "blocked"):
                    raise PlanValidationError(
                        f"Step '{old_id}' cannot be retired from status '{old_status}' (only pending or blocked allowed)"
                    )


def validate_plan(proposed: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    """Validate a proposed plan dictionary according to schema and graph rules."""
    validate_proposal_schema(proposed)
    validate_plan_steps_and_graph(proposed)
    if existing is not None:
        _validate_cross_revision(proposed, existing)


def validate_published_snapshot(
    data: Any,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_history: int = MAX_HISTORY,
) -> tuple[dict[str, Any], bytes]:
    """Validate a complete snapshot for reading or before persistence."""
    if not isinstance(data, dict):
        raise PlanValidationError("Published snapshot must be a JSON object")

    schema_version = data.get("schemaVersion")
    if type(schema_version) is not int or schema_version not in (1, SCHEMA_VERSION):
        raise PlanValidationError(f"Unsupported schemaVersion: {schema_version!r}")

    proposal_keys = {
        "schemaVersion", "taskId", "objective", "status", "executionMode",
        "nextStepId", "changeSummary", "steps"
    }
    if schema_version == SCHEMA_VERSION:
        unknown = set(data) - proposal_keys - PUBLISHER_OWNED_FIELDS
        if unknown:
            raise PlanValidationError(f"Published snapshot contains unknown fields: {sorted(unknown)}")
    common = {key: data[key] for key in proposal_keys if key in data}
    common["schemaVersion"] = SCHEMA_VERSION
    validate_proposal_schema(common)

    try:
        wire = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanValidationError("Snapshot cannot be serialized as valid UTF-8 JSON") from exc
    if len(wire) > max_file_bytes:
        raise PlanValidationError("Serialized snapshot exceeds maximum allowed size")

    if schema_version == 1:
        validate_plan_steps_and_graph(data, legacy=True)
        revision = data.get("revision")
        if type(revision) is not int or revision < 1:
            raise PlanValidationError("Published revision must be a positive integer")
        updated_at = _utc_timestamp(data.get("updatedAt"))
        history = data.get("history")
        if not isinstance(history, list) or len(history) != min(revision, max_history):
            raise PlanValidationError("Published history must contain the bounded revision sequence")
        previous_timestamp = None
        for expected, entry in enumerate(history, revision - len(history) + 1):
            if not isinstance(entry, dict) or type(entry.get("revision")) is not int or entry["revision"] != expected:
                raise PlanValidationError("Published history revisions must be consecutive and end at current revision")
            timestamp = _utc_timestamp(entry.get("timestamp"))
            if timestamp > updated_at or (previous_timestamp is not None and timestamp < previous_timestamp):
                raise PlanValidationError("Published history timestamps must be ordered and not newer than updatedAt")
            summary = entry.get("summary")
            if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_TEXT_LEN:
                raise PlanValidationError("Published history summary must be a bounded non-empty string")
            previous_timestamp = timestamp
        if history[-1]["timestamp"] != data["updatedAt"] or history[-1]["summary"] != data["changeSummary"]:
            raise PlanValidationError("Latest history must match updatedAt and changeSummary")
        return data, wire

    if schema_version != SCHEMA_VERSION:
        raise PlanValidationError(f"Invalid schemaVersion: expected 1 or {SCHEMA_VERSION}, got {schema_version}")

    # Validate v2 published snapshot
    generation = data.get("generation")
    if not isinstance(generation, str) or not generation.strip() or len(generation) > 128:
        raise PlanValidationError("Published generation must be a non-empty string <= 128 characters")
    try:
        uuid.UUID(generation)
    except ValueError as exc:
        raise PlanValidationError("Published generation must be a UUID") from exc

    revision = data.get("revision")
    if type(revision) is not int or revision < 1:
        raise PlanValidationError("Published revision must be a positive integer")

    updated_at = _utc_timestamp(data.get("updatedAt"))
    validate_plan_steps_and_graph(data)

    scope = data.get("scope")
    if not isinstance(scope, dict):
        raise PlanValidationError("Published v2 snapshot must contain a 'scope' dictionary")

    expected_scope_keys = {"mode", "sinceRevision", "baseline", "claimed"}
    if set(scope.keys()) != expected_scope_keys:
        raise PlanValidationError(f"Published scope keys must exactly be {expected_scope_keys}, got {set(scope.keys())}")

    if scope.get("mode") != data.get("executionMode"):
        raise PlanValidationError("Published scope.mode must equal executionMode")

    since_rev = scope.get("sinceRevision")
    if type(since_rev) is not int or since_rev < 1 or since_rev > revision:
        raise PlanValidationError(f"Published scope.sinceRevision must be a positive integer <= {revision}")

    baseline = scope.get("baseline")
    if not isinstance(baseline, list):
        raise PlanValidationError("Published scope.baseline must be a list of step IDs")
    for b_id in baseline:
        if not isinstance(b_id, str) or not b_id.strip():
            raise PlanValidationError("Published scope.baseline contains non-string or empty step ID")
    if len(baseline) != len(set(baseline)):
        raise PlanValidationError("Published scope.baseline must contain unique step IDs")

    steps_by_id = {s["id"]: s for s in data.get("steps", [])}
    for b_id in baseline:
        if b_id not in steps_by_id:
            raise PlanValidationError(f"Published scope.baseline step '{b_id}' not found in steps")
        if steps_by_id[b_id]["status"] != "complete":
            raise PlanValidationError(f"Published scope.baseline step '{b_id}' must have status 'complete'")

    claimed = scope.get("claimed")
    if claimed is not None:
        if not isinstance(claimed, str) or not claimed.strip() or len(claimed) > 128:
            raise PlanValidationError("Published scope.claimed must be a non-empty string <= 128 characters or null")
        if claimed in baseline:
            raise PlanValidationError(f"Published scope.claimed '{claimed}' cannot be in baseline")

    if scope.get("mode") == "plan-only":
        if claimed is not None:
            raise PlanValidationError("Published scope.claimed must be null in plan-only mode")
        new_complete = [s["id"] for s in data.get("steps", []) if s["id"] not in baseline and s["status"] == "complete"]
        if new_complete:
            raise PlanValidationError(f"plan-only mode cannot have completed step(s) outside baseline: {new_complete}")
    elif scope["mode"] == "first-step":
        observed = {
            s["id"] for s in data["steps"]
            if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
        }
        if observed - {claimed}:
            raise PlanValidationError("Published first-step work must match its persistent claimed ID")
    elif claimed is not None:
        raise PlanValidationError("Published scope.claimed must be null outside first-step mode")

    history = data.get("history")
    if not isinstance(history, list) or len(history) != min(revision, max_history):
        raise PlanValidationError("Published history must contain the bounded revision sequence")

    previous_timestamp = None
    for expected, entry in enumerate(history, revision - len(history) + 1):
        if not isinstance(entry, dict) or type(entry.get("revision")) is not int or entry["revision"] != expected:
            raise PlanValidationError("Published history revisions must be consecutive and end at current revision")
        timestamp = _utc_timestamp(entry.get("timestamp"))
        if timestamp > updated_at or (previous_timestamp is not None and timestamp < previous_timestamp):
            raise PlanValidationError("Published history timestamps must be ordered and not newer than updatedAt")
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_TEXT_LEN:
            raise PlanValidationError("Published history summary must be a bounded non-empty string")

        if "authorizationNote" in entry:
            auth_note = entry["authorizationNote"]
            if not isinstance(auth_note, str) or not auth_note.strip() or len(auth_note) > MAX_TEXT_LEN:
                raise PlanValidationError("History entry authorizationNote must be a bounded non-empty string")

        if "recoveryNote" in entry:
            rec_note = entry["recoveryNote"]
            if not isinstance(rec_note, str) or not rec_note.strip() or len(rec_note) > MAX_TEXT_LEN:
                raise PlanValidationError("History entry recoveryNote must be a bounded non-empty string")

        if entry["revision"] == since_rev and "authorizationNote" not in entry:
            if not (scope["mode"] == "plan-only" and since_rev == 1 and "recoveryNote" in entry):
                raise PlanValidationError("Retained scope-opening history requires an authorizationNote")

        previous_timestamp = timestamp

    if history[-1]["timestamp"] != data["updatedAt"] or history[-1]["summary"] != data["changeSummary"]:
        raise PlanValidationError("Latest history must match updatedAt and changeSummary")

    return data, wire


def validate_service_graph(
    proposed: dict[str, JSONValue],
    previous: dict[str, JSONValue] | None,
    coordination: CoordinationView,
) -> None:
    """Validate a plan graph within service boundaries, allowing retained in-progress history with derived blocked/stopped status."""
    validate_proposal_schema(proposed)  # type: ignore[arg-type]
    validate_plan_steps_and_graph(proposed, allow_service_in_progress=True)  # type: ignore[arg-type]
    if previous is not None:
        _validate_cross_revision(proposed, previous)  # type: ignore[arg-type]

    task_status = proposed.get("status")
    steps = proposed.get("steps")
    in_progress_steps = [
        s.get("id")
        for s in steps
        if isinstance(s, dict) and s.get("status") == "in-progress"
    ] if isinstance(steps, list) else []

    if task_status == "planning" and in_progress_steps:
        raise PlanValidationError(
            f"Plan status 'planning' cannot have in-progress steps: {in_progress_steps}"
        )

    if task_status in ("blocked", "stopped") and in_progress_steps:
        stale_step_ids = {
            att.step_id
            for att in getattr(coordination, "attempts", ())
            if getattr(att, "state", None) == "stale"
        }
        hold_step_ids = {
            hold.step_id
            for hold in getattr(coordination, "holds", ())
            if getattr(hold, "step_id", None) is not None
        }
        has_general_hold = (
            getattr(coordination, "has_plan_holds", False)
            or getattr(coordination, "store_hold", False)
            or any(getattr(h, "step_id", None) is None for h in getattr(coordination, "holds", ()))
        )
        for sid in in_progress_steps:
            if not (has_general_hold or sid in stale_step_ids or sid in hold_step_ids):
                raise PlanValidationError(
                    f"In-progress step '{sid}' under '{task_status}' status requires backing stale attempt or hold in coordination"
                )


def validate_execution_scope(
    previous: Authorization,
    step_id: StepId | None,
) -> Authorization:
    """Validate execution eligibility and return updated authorization with latching applied."""
    if step_id is None:
        return previous

    if previous.mode == "plan-only":
        raise PlanValidationError("plan-only mode does not authorize step execution")

    if previous.mode == "first-step":
        if step_id in previous.baseline:
            raise PlanValidationError(f"Step '{step_id}' is already completed in baseline")

        if previous.claimed is None:
            # First claim latch
            return Authorization(
                mode=previous.mode,
                interval=previous.interval,
                since_revision=previous.since_revision,
                note=previous.note,
                baseline=previous.baseline,
                claimed=step_id,
            )
        else:
            if step_id != previous.claimed:
                raise PlanValidationError(
                    f"Scope violation: step '{step_id}' cannot be executed; step '{previous.claimed}' was already claimed for this interval"
                )
            return previous

    if previous.mode == "complete-task":
        if step_id in previous.baseline:
            raise PlanValidationError(f"Step '{step_id}' is already completed in baseline")
        return previous

    raise PlanValidationError(f"Unknown authorization mode: {previous.mode!r}")


def renew_authorization(
    previous: Authorization,
    change: AuthorizationChange,
    completed: tuple[StepId, ...],
    claimed_steps: tuple[StepId, ...],
    *,
    revision: int,
    interval: str,
) -> Authorization:
    """Renew execution authorization interval and baseline."""
    if not isinstance(change.note, str) or not change.note.strip() or len(change.note) > MAX_TEXT_LEN:
        raise PlanValidationError("authorizationNote must be a bounded non-empty string <= 10000 characters")

    new_mode = change.mode
    if new_mode not in VALID_EXECUTION_MODES:
        raise PlanValidationError(f"Invalid executionMode '{new_mode}'")

    new_baseline = tuple(completed)
    new_claimed: StepId | None = None

    if new_mode == "plan-only":
        active = [s for s in claimed_steps if s not in new_baseline]
        if active:
            raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {sorted(active)}")
        new_claimed = None
    elif new_mode == "first-step":
        active = [s for s in claimed_steps if s not in new_baseline]
        if len(active) > 1:
            raise PlanValidationError(
                f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active)}"
            )
        new_claimed = active[0] if active else None
    elif new_mode == "complete-task":
        new_claimed = None

    return Authorization(
        mode=new_mode,
        interval=interval,
        since_revision=revision,
        note=change.note.strip(),
        baseline=new_baseline,
        claimed=new_claimed,
    )


def prepare_publication(
    proposal: dict[str, JSONValue],
    previous: dict[str, JSONValue] | None,
    *,
    timestamp: str,
    generation: Generation,
    expected_revision: int,
    expected_generation: str | None,
    authorization_note: str | None,
    migrate_v1: bool = False,
    max_file_bytes: int,
    max_history: int,
    validate_plan_fn: Callable = validate_plan,
    validate_snapshot_fn: Callable = validate_published_snapshot,
) -> dict[str, JSONValue]:
    """Pure preparation and validation of a published snapshot before atomic storage."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise PlanValidationError("Field 'expected-revision' must be a non-negative integer (>= 0)")

    if authorization_note is not None:
        if not isinstance(authorization_note, str) or not authorization_note.strip() or len(authorization_note) > MAX_TEXT_LEN:
            raise PlanValidationError("authorizationNote must be a bounded non-empty string <= 10000 characters")

    is_migration = migrate_v1 or (previous is not None and previous.get("schemaVersion") == 1)

    if previous is None:
        if expected_revision != 0:
            raise PlanValidationError(
                f"Revision conflict: no existing state file found, but expected revision is {expected_revision} (must be 0 for initial publish)."
            )
        if expected_generation is not None:
            raise PlanValidationError("Expected generation must not be specified for initial creation (revision 0)")
        if not authorization_note or not authorization_note.strip():
            raise PlanValidationError("Initial creation requires an authorizationNote")
        current_revision = 0
        new_generation = str(generation)
    else:
        current_revision = previous["revision"]  # type: ignore[index]
        if expected_revision == 0:
            raise PlanValidationError(
                f"Revision conflict: state already exists at revision {current_revision}, but expected revision 0 (create-only)."
            )
        if expected_revision != current_revision:
            raise PlanValidationError(
                f"Revision conflict: current revision is {current_revision}, but expected revision is {expected_revision}."
            )
        if is_migration:
            if expected_generation != "none":
                raise PlanValidationError("For v1 migration require literal --expected-generation none")
            if not authorization_note or not authorization_note.strip():
                raise PlanValidationError("v1 migration requires an authorizationNote")
            new_generation = str(generation)
        else:
            if expected_generation is None:
                raise PlanValidationError("Field '--expected-generation' is required for updates to existing v2 state")
            if expected_generation != previous["generation"]:  # type: ignore[index]
                raise PlanValidationError(
                    f"Generation conflict: current is '{previous['generation']}', but expected generation is '{expected_generation}'."  # type: ignore[index]
                )
            new_generation = previous["generation"]  # type: ignore[index]

    # Validate proposal schema and cross-revision rules without mutating input
    proposed = copy.deepcopy(proposal)
    validate_plan_fn(proposed, previous)

    proposed_mode = proposed["executionMode"]  # type: ignore[index]
    new_revision = current_revision + 1
    now_utc = _utc_timestamp(timestamp)
    if previous is not None:
        now_utc = max(now_utc, _utc_timestamp(previous["updatedAt"]))  # type: ignore[index]
    new_updated_at = now_utc.isoformat()

    # Compute Scope & Generation
    if previous is None:
        baseline = [s["id"] for s in proposed["steps"] if s["status"] == "complete"]  # type: ignore[index]
        if proposed_mode == "plan-only":
            claimed = None
        elif proposed_mode == "first-step":
            active_or_complete = [
                s["id"] for s in proposed["steps"]  # type: ignore[index]
                if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
            ]
            if len(active_or_complete) > 1:
                raise PlanValidationError(
                    f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                )
            claimed = active_or_complete[0] if active_or_complete else None
        else:
            claimed = None

        scope = {
            "mode": proposed_mode,
            "sinceRevision": 1,
            "baseline": baseline,
            "claimed": claimed,
        }

    elif is_migration:
        baseline = [s["id"] for s in previous["steps"] if s["status"] == "complete"]  # type: ignore[index]
        if proposed_mode == "plan-only":
            active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]  # type: ignore[index]
            if active:
                raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
            new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]  # type: ignore[index]
            if new_complete:
                raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
            claimed = None
        elif proposed_mode == "first-step":
            active_or_complete = [
                s["id"] for s in proposed["steps"]  # type: ignore[index]
                if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
            ]
            if len(active_or_complete) > 1:
                raise PlanValidationError(
                    f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                )
            claimed = active_or_complete[0] if active_or_complete else None
        else:
            claimed = None

        scope = {
            "mode": proposed_mode,
            "sinceRevision": new_revision,
            "baseline": baseline,
            "claimed": claimed,
        }

    else:
        existing_scope = previous["scope"]  # type: ignore[index]
        has_auth_note = bool(authorization_note and authorization_note.strip())

        if proposed_mode != existing_scope["mode"]:
            if not has_auth_note:
                raise PlanValidationError("Execution mode change requires an authorizationNote")
            baseline = [s["id"] for s in previous["steps"] if s["status"] == "complete"]  # type: ignore[index]
            if proposed_mode == "plan-only":
                active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]  # type: ignore[index]
                if active:
                    raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]  # type: ignore[index]
                if new_complete:
                    raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                claimed = None
            elif proposed_mode == "first-step":
                active_or_complete = [
                    s["id"] for s in proposed["steps"]  # type: ignore[index]
                    if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
                ]
                if len(active_or_complete) > 1:
                    raise PlanValidationError(
                        f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                    )
                claimed = active_or_complete[0] if active_or_complete else None
            else:
                claimed = None

            scope = {
                "mode": proposed_mode,
                "sinceRevision": new_revision,
                "baseline": baseline,
                "claimed": claimed,
            }

        elif has_auth_note:
            baseline = [s["id"] for s in previous["steps"] if s["status"] == "complete"]  # type: ignore[index]
            if proposed_mode == "plan-only":
                active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]  # type: ignore[index]
                if active:
                    raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]  # type: ignore[index]
                if new_complete:
                    raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                claimed = None
            elif proposed_mode == "first-step":
                active_or_complete = [
                    s["id"] for s in proposed["steps"]  # type: ignore[index]
                    if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
                ]
                if len(active_or_complete) > 1:
                    raise PlanValidationError(
                        f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                    )
                claimed = active_or_complete[0] if active_or_complete else None
            else:
                claimed = None

            scope = {
                "mode": proposed_mode,
                "sinceRevision": new_revision,
                "baseline": baseline,
                "claimed": claimed,
            }

        else:
            since_rev = existing_scope["sinceRevision"]
            baseline = existing_scope["baseline"]
            claimed = existing_scope["claimed"]

            if proposed_mode == "plan-only":
                active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]  # type: ignore[index]
                if active:
                    raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]  # type: ignore[index]
                if new_complete:
                    raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                claimed = None

            elif proposed_mode == "first-step":
                active_or_complete = [
                    s["id"] for s in proposed["steps"]  # type: ignore[index]
                    if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
                ]
                if len(active_or_complete) > 1:
                    raise PlanValidationError(
                        f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                    )

                if claimed is None:
                    claimed = active_or_complete[0] if active_or_complete else None
                else:
                    for sid in active_or_complete:
                        if sid != claimed:
                            raise PlanValidationError(
                                f"Scope violation: step '{sid}' cannot be executed; step '{claimed}' was already claimed for this interval"
                            )
            else:
                claimed = None

            scope = {
                "mode": proposed_mode,
                "sinceRevision": since_rev,
                "baseline": baseline,
                "claimed": claimed,
            }

    # Build history entry
    new_history_entry: dict[str, Any] = {
        "revision": new_revision,
        "timestamp": new_updated_at,
        "summary": proposed["changeSummary"],  # type: ignore[index]
    }
    if authorization_note and authorization_note.strip():
        new_history_entry["authorizationNote"] = authorization_note.strip()

    existing_history: list[dict[str, Any]] = list(previous.get("history", [])) if previous else []  # type: ignore[union-attr]
    new_history = existing_history + [new_history_entry]
    if len(new_history) > max_history:
        new_history = new_history[-max_history:]

    published_state: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "generation": new_generation,
        "revision": new_revision,
        "updatedAt": new_updated_at,
        "taskId": proposed["taskId"],  # type: ignore[index]
        "objective": proposed["objective"],  # type: ignore[index]
        "status": proposed["status"],  # type: ignore[index]
        "executionMode": proposed["executionMode"],  # type: ignore[index]
        "scope": scope,
        "nextStepId": proposed.get("nextStepId"),  # type: ignore[attr-defined]
        "changeSummary": proposed["changeSummary"],  # type: ignore[index]
        "steps": proposed["steps"],  # type: ignore[index]
        "history": new_history,
    }

    validate_snapshot_fn(published_state, max_file_bytes=max_file_bytes, max_history=max_history)
    try:
        serialized = json.dumps(published_state, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanValidationError("Snapshot cannot be serialized as valid UTF-8 JSON") from exc
    if len(serialized) > max_file_bytes:
        raise PlanValidationError(f"Published snapshot exceeds maximum allowed size ({max_file_bytes} bytes)")

    return published_state
