#!/usr/bin/env python3
"""
live_plan.py - Publisher, recovery tool, and localhost server for the Live Plan dashboard.

Standard-library only (Python 3.10+). No external dependencies.
Schema Version: 2
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.server
import json
import os
import secrets
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

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


class RetryableStorageError(Exception):
    """Raised when atomic replacement fails after exhausting bounded retries on transient errors."""

    def __init__(self, message: str, revision: int | None = None, backup_path: str | None = None) -> None:
        super().__init__(message)
        self.revision = revision
        self.backup_path = backup_path


class FileLock:
    """Cross-platform exclusive file lock with bounded timeout and thread safety."""

    _meta_lock = threading.Lock()
    _path_locks: dict[str, threading.RLock] = {}

    def __init__(self, lock_path: str, timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        self.lock_path = os.path.abspath(lock_path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None
        with FileLock._meta_lock:
            if self.lock_path not in FileLock._path_locks:
                FileLock._path_locks[self.lock_path] = threading.RLock()
            self._thread_lock = FileLock._path_locks[self.lock_path]
        self._thread_acquired = False

    def __enter__(self) -> FileLock:
        start_time = time.time()
        acquired = self._thread_lock.acquire(timeout=self.timeout)
        if not acquired:
            raise TimeoutError(f"Could not acquire thread lock on {self.lock_path} within {self.timeout}s")
        self._thread_acquired = True

        remaining = self.timeout - (time.time() - start_time)
        if remaining <= 0:
            self._thread_lock.release()
            self._thread_acquired = False
            raise TimeoutError(f"Lock timeout expired before acquiring OS lock on {self.lock_path}")

        while True:
            try:
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        if os.fstat(self._fd).st_size < 1:
                            os.write(self._fd, b"\x00")
                            os.fsync(self._fd)
                    except OSError:
                        pass
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError, PermissionError) as e:
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
                if time.time() - start_time >= self.timeout:
                    if self._thread_acquired:
                        self._thread_lock.release()
                        self._thread_acquired = False
                    raise TimeoutError(f"Could not acquire lock on {self.lock_path} within {self.timeout}s") from e
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self._fd is not None:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        try:
                            os.lseek(self._fd, 0, os.SEEK_SET)
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl
                        try:
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    os.close(self._fd)
                except OSError:
                    pass
                finally:
                    self._fd = None
        finally:
            if self._thread_acquired:
                self._thread_lock.release()
                self._thread_acquired = False


def _reject_constant(val: str) -> None:
    raise ValueError(f"Constant '{val}' not allowed in JSON")


def _read_bounded_json(path: str, *, reject_symlink: bool = False) -> tuple[Any, bytes]:
    """Read at most the serialized limit, including when a file grows during a read."""
    if reject_symlink and os.path.islink(path):
        raise PermissionError("Symlinked canonical state is forbidden")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if reject_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise PlanValidationError(f"JSON file exceeds maximum allowed size ({MAX_FILE_BYTES} bytes)")
    try:
        return json.loads(content.decode("utf-8"), parse_constant=_reject_constant), content
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanValidationError("Plan state corrupted or unreadable") from exc


def _utc_timestamp(value: Any) -> datetime:
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


def validate_plan_steps_and_graph(proposed: dict[str, Any], *, legacy: bool = False) -> None:
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

    if task_status in ("stopped", "blocked", "planning"):
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


def validate_plan(proposed: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    """
    Validate a proposed plan dictionary according to schema and graph rules.
    If existing state is provided, enforce consistency constraints between revisions.
    """
    validate_proposal_schema(proposed)
    validate_plan_steps_and_graph(proposed)

    if existing is not None:
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


def read_published_snapshot(path: str) -> tuple[dict[str, Any], bytes]:
    """Return a bounded, schema-valid v1/v2 snapshot and deterministic served JSON."""
    data, _ = _read_bounded_json(path, reject_symlink=True)
    return validate_published_snapshot(data)


def validate_published_snapshot(data: Any) -> tuple[dict[str, Any], bytes]:
    """Validate a complete snapshot for reading or before any persistence side effect."""
    if not isinstance(data, dict):
        raise PlanValidationError("Published snapshot must be a JSON object")

    schema_version = data.get("schemaVersion")
    if type(schema_version) is not int or schema_version not in (1, SCHEMA_VERSION):
        raise PlanValidationError(f"Unsupported schemaVersion: {schema_version!r}")
    # Validate common fields without applying proposal metadata rules to a snapshot.
    proposal_keys = {"schemaVersion", "taskId", "objective", "status", "executionMode",
                     "nextStepId", "changeSummary", "steps"}
    if schema_version == SCHEMA_VERSION:
        unknown = set(data) - proposal_keys - PUBLISHER_OWNED_FIELDS
        if unknown:
            raise PlanValidationError(f"Published snapshot contains unknown fields: {sorted(unknown)}")
    common = {key: data[key] for key in proposal_keys if key in data}
    common["schemaVersion"] = SCHEMA_VERSION
    validate_proposal_schema(common)
    # Force UTF-8 encoding too: escaped lone surrogates must never reach SSE/HTTP.
    try:
        wire = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanValidationError("Snapshot cannot be serialized as valid UTF-8 JSON") from exc
    if len(wire) > MAX_FILE_BYTES:
        raise PlanValidationError("Serialized snapshot exceeds maximum allowed size")
    if schema_version == 1:
        # Legacy read semantics permit blocked next targets and accumulated completions.
        validate_plan_steps_and_graph(data, legacy=True)
        revision = data.get("revision")
        if type(revision) is not int or revision < 1:
            raise PlanValidationError("Published revision must be a positive integer")
        updated_at = _utc_timestamp(data.get("updatedAt"))
        history = data.get("history")
        if not isinstance(history, list) or len(history) != min(revision, MAX_HISTORY):
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

    # Graph and step structure validation
    validate_plan_steps_and_graph(data)

    # Validate publisher-maintained scope
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
        observed = {s["id"] for s in data["steps"]
                    if s["id"] not in baseline and s["status"] in ("in-progress", "complete")}
        if observed - {claimed}:
            raise PlanValidationError("Published first-step work must match its persistent claimed ID")
    elif claimed is not None:
        raise PlanValidationError("Published scope.claimed must be null outside first-step mode")

    # Validate history
    history = data.get("history")
    if not isinstance(history, list) or len(history) != min(revision, MAX_HISTORY):
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


def _atomic_replace_with_retry(
    tmp_path: str,
    canonical_path: str,
    timeout: float = 2.0,
    current_revision: int | None = None,
    is_recovery: bool = False,
) -> None:
    """Atomic replacement under lock with bounded retry for transient Windows errors."""
    start_time = time.monotonic()
    delay = 0.02
    while True:
        try:
            os.replace(tmp_path, canonical_path)
            return
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            if sys.platform == "win32" and winerror in (5, 32):
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                    if is_recovery:
                        raise RetryableStorageError("retryable; canonical state unchanged") from exc
                    else:
                        rev_str = f" at revision {current_revision}" if current_revision is not None else ""
                        raise RetryableStorageError(f"retryable; state unchanged{rev_str}", revision=current_revision) from exc
                time.sleep(delay)
                delay = min(0.2, delay * 1.5)
            else:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise


def publish(
    state_dir: str,
    input_file: str,
    expected_revision: int,
    expected_generation: str | None = None,
    authorization_note: str | None = None,
) -> dict[str, Any]:
    """Publish a validated plan update under an exclusive lock with atomic replacement."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise PlanValidationError("Field 'expected-revision' must be a non-negative integer (>= 0)")

    if authorization_note is not None:
        if not isinstance(authorization_note, str) or not authorization_note.strip() or len(authorization_note) > MAX_TEXT_LEN:
            raise PlanValidationError("authorizationNote must be a bounded non-empty string <= 10000 characters")

    os.makedirs(state_dir, exist_ok=True)
    canonical_path = os.path.join(state_dir, "live_plan.json")
    lock_path = os.path.join(state_dir, "live_plan.lock")

    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    proposed, _ = _read_bounded_json(input_file)

    with FileLock(lock_path):
        existing: dict[str, Any] | None = None
        current_revision = 0
        is_migration = False

        if os.path.lexists(canonical_path):
            try:
                existing, _ = read_published_snapshot(canonical_path)
                current_revision = existing["revision"]
            except Exception as e:
                raise RuntimeError(f"Failed to read existing state at {canonical_path}: {e}") from e

            if expected_revision == 0:
                raise PlanValidationError(
                    f"Revision conflict: state already exists at revision {current_revision}, but expected revision 0 (create-only)."
                )
            if expected_revision != current_revision:
                raise PlanValidationError(
                    f"Revision conflict: current revision is {current_revision}, but expected revision is {expected_revision}."
                )

            if existing.get("schemaVersion") == 1:
                # v1 migration
                is_migration = True
                if expected_generation != "none":
                    raise PlanValidationError("For v1 migration require literal --expected-generation none")
                if not authorization_note or not authorization_note.strip():
                    raise PlanValidationError("v1 migration requires an authorizationNote")
            else:
                # Regular v2 update
                if expected_generation is None:
                    raise PlanValidationError("Field '--expected-generation' is required for updates to existing v2 state")
                if expected_generation != existing["generation"]:
                    raise PlanValidationError(
                        f"Generation conflict: current is '{existing['generation']}', but expected generation is '{expected_generation}'."
                    )
        else:
            if expected_revision != 0:
                raise PlanValidationError(
                    f"Revision conflict: no existing state file found, but expected revision is {expected_revision} (must be 0 for initial publish)."
                )
            if expected_generation is not None:
                raise PlanValidationError("Expected generation must not be specified for initial creation (revision 0)")
            if not authorization_note or not authorization_note.strip():
                raise PlanValidationError("Initial creation requires an authorizationNote")

        # Validate proposal schema and cross-revision rules
        validate_plan(proposed, existing)

        proposed_mode = proposed["executionMode"]
        new_revision = current_revision + 1
        now_utc = datetime.now(timezone.utc)
        if existing is not None:
            now_utc = max(now_utc, _utc_timestamp(existing["updatedAt"]))
        new_updated_at = now_utc.isoformat()

        # Compute Scope & Generation
        if existing is None:
            # Creation: baseline = imported completed IDs
            new_generation = str(uuid.uuid4())
            baseline = [s["id"] for s in proposed["steps"] if s["status"] == "complete"]
            if proposed_mode == "plan-only":
                claimed = None
            elif proposed_mode == "first-step":
                active_or_complete = [
                    s["id"] for s in proposed["steps"]
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
                "claimed": claimed
            }

        elif is_migration:
            # Migration: baseline = completed IDs in previous canonical snapshot
            new_generation = str(uuid.uuid4())
            baseline = [s["id"] for s in existing["steps"] if s["status"] == "complete"]
            if proposed_mode == "plan-only":
                active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]
                if active:
                    raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]
                if new_complete:
                    raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                claimed = None
            elif proposed_mode == "first-step":
                active_or_complete = [
                    s["id"] for s in proposed["steps"]
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
                "claimed": claimed
            }

        else:
            # Regular v2 update
            new_generation = existing["generation"]
            existing_scope = existing["scope"]
            has_auth_note = bool(authorization_note and authorization_note.strip())

            if proposed_mode != existing_scope["mode"]:
                if not has_auth_note:
                    raise PlanValidationError("Execution mode change requires an authorizationNote")
                # Interval renewed for new mode
                baseline = [s["id"] for s in existing["steps"] if s["status"] == "complete"]
                if proposed_mode == "plan-only":
                    active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]
                    if active:
                        raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                    new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]
                    if new_complete:
                        raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                    claimed = None
                elif proposed_mode == "first-step":
                    active_or_complete = [
                        s["id"] for s in proposed["steps"]
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
                    "claimed": claimed
                }

            elif has_auth_note:
                # Same-mode authorizationNote renews interval
                baseline = [s["id"] for s in existing["steps"] if s["status"] == "complete"]
                if proposed_mode == "plan-only":
                    active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]
                    if active:
                        raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                    new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]
                    if new_complete:
                        raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                    claimed = None
                elif proposed_mode == "first-step":
                    active_or_complete = [
                        s["id"] for s in proposed["steps"]
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
                    "claimed": claimed
                }

            else:
                # No note: carry scope unchanged except first claim
                since_rev = existing_scope["sinceRevision"]
                baseline = existing_scope["baseline"]
                claimed = existing_scope["claimed"]

                if proposed_mode == "plan-only":
                    active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]
                    if active:
                        raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
                    new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]
                    if new_complete:
                        raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
                    claimed = None

                elif proposed_mode == "first-step":
                    active_or_complete = [
                        s["id"] for s in proposed["steps"]
                        if s["id"] not in baseline and s["status"] in ("in-progress", "complete")
                    ]
                    if len(active_or_complete) > 1:
                        raise PlanValidationError(
                            f"first-step mode cannot have >1 active or completed step outside baseline: {sorted(active_or_complete)}"
                        )

                    if claimed is None:
                        # First claim latch
                        claimed = active_or_complete[0] if active_or_complete else None
                    else:
                        # Claimed step already fixed: any nonbaseline active or complete step must be claimed
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
                    "claimed": claimed
                }

        # Build history entry
        new_history_entry: dict[str, Any] = {
            "revision": new_revision,
            "timestamp": new_updated_at,
            "summary": proposed["changeSummary"]
        }
        if authorization_note and authorization_note.strip():
            new_history_entry["authorizationNote"] = authorization_note.strip()

        existing_history: list[dict[str, Any]] = existing.get("history", []) if existing else []
        new_history = existing_history + [new_history_entry]
        if len(new_history) > MAX_HISTORY:
            new_history = new_history[-MAX_HISTORY:]

        published_state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "generation": new_generation,
            "revision": new_revision,
            "updatedAt": new_updated_at,
            "taskId": proposed["taskId"],
            "objective": proposed["objective"],
            "status": proposed["status"],
            "executionMode": proposed["executionMode"],
            "scope": scope,
            "nextStepId": proposed.get("nextStepId"),
            "changeSummary": proposed["changeSummary"],
            "steps": proposed["steps"],
            "history": new_history
        }

        # Migration retains history verbatim. Refuse incompatible legacy extensions
        # before writing rather than committing a v2 state our own reader rejects.
        validate_published_snapshot(published_state)
        try:
            serialized = json.dumps(published_state, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PlanValidationError("Snapshot cannot be serialized as valid UTF-8 JSON") from exc
        if len(serialized) > MAX_FILE_BYTES:
            raise PlanValidationError(f"Published snapshot exceeds maximum allowed size ({MAX_FILE_BYTES} bytes)")

        # Atomic commit via temporary file in the same directory
        tmp_path = os.path.join(state_dir, f"live_plan.json.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            _atomic_replace_with_retry(tmp_path, canonical_path, timeout=2.0, current_revision=current_revision)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        return published_state


def recover(
    state_dir: str,
    input_file: str,
    recovery_note: str,
    authorization_note: str | None = None,
) -> dict[str, Any]:
    """Recover from a corrupt or unreadable canonical plan under lock."""
    if not isinstance(recovery_note, str) or not recovery_note.strip() or len(recovery_note) > MAX_TEXT_LEN:
        raise PlanValidationError("Recovery requires a non-empty recoveryNote (<= 10000 characters)")

    if authorization_note is not None:
        if not isinstance(authorization_note, str) or not authorization_note.strip() or len(authorization_note) > MAX_TEXT_LEN:
            raise PlanValidationError("authorizationNote must be a bounded non-empty string <= 10000 characters")

    os.makedirs(state_dir, exist_ok=True)
    canonical_path = os.path.join(state_dir, "live_plan.json")
    lock_path = os.path.join(state_dir, "live_plan.lock")

    with FileLock(lock_path):
        # 1. Refuse absent canonical
        if not os.path.lexists(canonical_path):
            raise PlanValidationError(f"Cannot recover: canonical state file does not exist at {canonical_path}")

        # 2. Refuse symlinked canonical
        if os.path.islink(canonical_path):
            raise PermissionError("Cannot recover: symlinked canonical state is forbidden")

        # 3. Refuse valid canonical
        is_valid = False
        try:
            read_published_snapshot(canonical_path)
            is_valid = True
        except PlanValidationError:
            is_valid = False
        if is_valid:
            raise PlanValidationError("Cannot recover: canonical state is valid; recovery is only allowed on corrupt or invalid states")

        # 4. Validate/serialize replacement first (No backup on invalid input)
        if not os.path.isfile(input_file):
            raise FileNotFoundError(f"Input file not found: {input_file}")

        proposed, _ = _read_bounded_json(input_file)
        validate_proposal_schema(proposed)
        validate_plan_steps_and_graph(proposed)

        exec_mode = proposed["executionMode"]
        if exec_mode in ("first-step", "complete-task"):
            if not authorization_note or not authorization_note.strip():
                raise PlanValidationError(f"Recovery in executing mode '{exec_mode}' requires an authorizationNote")

        # Build fresh generation / rev 1 / scope
        new_generation = str(uuid.uuid4())
        new_revision = 1
        now_utc = datetime.now(timezone.utc)
        new_updated_at = now_utc.isoformat()

        baseline = [s["id"] for s in proposed["steps"] if s["status"] == "complete"]
        if exec_mode == "plan-only":
            active = [s["id"] for s in proposed["steps"] if s["status"] == "in-progress"]
            if active:
                raise PlanValidationError(f"plan-only mode cannot have in-progress steps: {active}")
            new_complete = [s["id"] for s in proposed["steps"] if s["id"] not in baseline and s["status"] == "complete"]
            if new_complete:
                raise PlanValidationError(f"plan-only mode cannot have newly completed steps outside baseline: {new_complete}")
            claimed = None
        elif exec_mode == "first-step":
            active_or_complete = [
                s["id"] for s in proposed["steps"]
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
            "mode": exec_mode,
            "sinceRevision": 1,
            "baseline": baseline,
            "claimed": claimed
        }

        hist_entry: dict[str, Any] = {
            "revision": 1,
            "timestamp": new_updated_at,
            "summary": proposed["changeSummary"],
            "recoveryNote": recovery_note.strip()
        }
        if authorization_note and authorization_note.strip():
            hist_entry["authorizationNote"] = authorization_note.strip()

        recovered_state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "generation": new_generation,
            "revision": 1,
            "updatedAt": new_updated_at,
            "taskId": proposed["taskId"],
            "objective": proposed["objective"],
            "status": proposed["status"],
            "executionMode": proposed["executionMode"],
            "scope": scope,
            "nextStepId": proposed.get("nextStepId"),
            "changeSummary": proposed["changeSummary"],
            "steps": proposed["steps"],
            "history": [hist_entry]
        }

        # Validate the assembled snapshot before even creating a corrupt backup.
        validate_published_snapshot(recovered_state)
        try:
            serialized = json.dumps(recovered_state, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PlanValidationError("Snapshot cannot be serialized as valid UTF-8 JSON") from exc
        if len(serialized) > MAX_FILE_BYTES:
            raise PlanValidationError(f"Recovered snapshot exceeds maximum allowed size ({MAX_FILE_BYTES} bytes)")

        # Create byte-for-byte corrupt backup file with O_CREAT | O_EXCL
        utc_str = now_utc.strftime("%Y%m%dT%H%M%SZ")
        backup_name = None
        for _ in range(50):
            rnd = secrets.token_hex(4)
            candidate_name = f"live_plan.corrupt.{utc_str}.{rnd}.json"
            candidate_path = os.path.join(state_dir, candidate_name)
            try:
                b_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                fd = os.open(candidate_path, b_flags)
                try:
                    with os.fdopen(fd, "wb") as bf:
                        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                        with os.fdopen(os.open(canonical_path, flags), "rb") as original:
                            shutil.copyfileobj(original, bf, length=64 * 1024)
                        bf.flush()
                        os.fsync(bf.fileno())
                    backup_name = candidate_name
                    break
                except Exception as exc:
                    if os.path.exists(candidate_path):
                        try:
                            os.remove(candidate_path)
                        except OSError as cleanup_error:
                            raise RuntimeError(
                                f"Recovery backup incomplete at {candidate_path}; canonical state unchanged; "
                                f"partial backup cleanup failed: {cleanup_error}"
                            ) from exc
                    raise RuntimeError("Recovery backup failed; canonical state unchanged; no completed backup") from exc
            except FileExistsError:
                continue

        if not backup_name:
            raise RuntimeError("Failed to create unique backup file for corrupt state")

        # Atomic replacement with same bounded retry
        tmp_path = os.path.join(state_dir, f"live_plan.json.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            _atomic_replace_with_retry(tmp_path, canonical_path, timeout=2.0, is_recovery=True)
        except RetryableStorageError as exc:
            backup_path = os.path.join(state_dir, backup_name)
            raise RetryableStorageError(
                f"{exc}; completed backup preserved at {backup_path}", backup_path=backup_path
            ) from exc
        except Exception as exc:
            # Failure before commit canonical unchanged; completed backup may remain and must be reported
            raise RuntimeError(f"Recovery commit failed (backup preserved at {backup_name}): {exc}") from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        recovered_state["_corruptBackup"] = backup_name
        return recovered_state


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded localhost HTTP server with daemon threads."""
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[http.server.BaseHTTPRequestHandler], state_dir: str, assets_dir: str) -> None:
        if sys.platform == "win32":
            self.allow_reuse_address = False
        else:
            self.allow_reuse_address = True
        super().__init__(server_address, RequestHandlerClass)
        self.state_dir = os.path.abspath(state_dir)
        self.assets_dir = os.path.abspath(assets_dir)
        self.stop_event = threading.Event()
        self.poll_hook: Callable[[], None] | None = None

    def server_bind(self) -> None:
        if sys.platform == "win32":
            import socket
            SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
            self.socket.setsockopt(socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LivePlanRequestHandler(http.server.BaseHTTPRequestHandler):
    """Strict localhost request handler serving dashboard assets and live SSE plan events."""
    server_version = "LivePlanDashboard/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _validate_host(self) -> bool:
        host_hdr = self.headers.get("Host", "").strip().lower()
        port = self.server.server_address[1]
        allowed = {
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        }
        if host_hdr not in allowed:
            self.send_error(400, "Bad Request: Invalid Host header")
            return False
        return True

    def _validate_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        port = self.server.server_address[1]
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        if origin.rstrip("/").lower() not in allowed_origins:
            self.send_error(403, "Forbidden: Cross-origin request blocked")
            return False
        return True

    def _send_security_headers(self, content_type: str, cache_control: str = "no-store, no-cache, must-revalidate") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none';"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def do_HEAD(self) -> None:
        if not self._validate_host() or not self._validate_origin():
            return
        self._route_request(send_body=False)

    def do_GET(self) -> None:
        if not self._validate_host() or not self._validate_origin():
            return
        self._route_request(send_body=True)

    def do_POST(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def do_PATCH(self) -> None:
        self.send_error(405, "Method Not Allowed")

    def _route_request(self, send_body: bool = True) -> None:
        url_parts = urllib.parse.urlsplit(self.path)
        path = url_parts.path

        if path in ("/", "/index.html"):
            self._serve_asset("index.html", "text/html; charset=utf-8", send_body)
        elif path == "/style.css":
            self._serve_asset("style.css", "text/css; charset=utf-8", send_body)
        elif path == "/app.js":
            self._serve_asset("app.js", "application/javascript; charset=utf-8", send_body)
        elif path == "/api/plan":
            self._serve_api_plan(send_body)
        elif path == "/events":
            self._serve_events(send_body)
        else:
            self.send_error(404, "Not Found")

    def _serve_asset(self, filename: str, content_type: str, send_body: bool) -> None:
        file_path = os.path.join(self.server.assets_dir, filename)
        asset_root = os.path.realpath(self.server.assets_dir)
        resolved_path = os.path.realpath(file_path)
        try:
            contained = os.path.commonpath((asset_root, resolved_path)) == asset_root
        except ValueError:
            contained = False
        if os.path.islink(file_path) or not contained:
            self.send_error(403, "Forbidden")
            return
        if not os.path.isfile(file_path):
            self.send_error(404, f"Asset '{filename}' not found")
            return

        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError:
            self.send_error(500, f"Error reading asset '{filename}'")
            return

        self.send_response(200)
        self._send_security_headers(content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if send_body:
            self.wfile.write(content)

    def _serve_api_plan(self, send_body: bool) -> None:
        canonical_path = os.path.join(self.server.state_dir, "live_plan.json")
        try:
            _, content = read_published_snapshot(canonical_path)
        except FileNotFoundError:
            self.send_error(404, "No published plan found")
            return
        except PermissionError:
            self.send_error(403, "Forbidden")
            return
        except (PlanValidationError, OSError):
            self.send_error(500, "Plan state corrupted or unreadable")
            return

        self.send_response(200)
        self._send_security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if send_body:
            self.wfile.write(content)

    def _serve_events(self, send_body: bool) -> None:
        if not send_body:
            self.send_response(200)
            self._send_security_headers("text/event-stream", "no-cache, no-transform")
            self.end_headers()
            return

        self.send_response(200)
        self._send_security_headers("text/event-stream", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        last_sent_bytes: bytes | None = None
        state_unavailable = False
        last_heartbeat = time.time()
        canonical_path = os.path.join(self.server.state_dir, "live_plan.json")

        try:
            while not self.server.stop_event.is_set():
                if self.server.poll_hook is not None:
                    try:
                        self.server.poll_hook()
                    except Exception:
                        pass

                try:
                    data, content = read_published_snapshot(canonical_path)
                except (PlanValidationError, OSError):
                    if not state_unavailable:
                        self.wfile.write(b'event: state-error\ndata: {"message":"Published plan unavailable or invalid; retaining last valid snapshot."}\n\n')
                        self.wfile.flush()
                        state_unavailable = True
                else:
                    # Send when bytes change, OR when recovering from invalid/unavailable even if same content hash
                    if state_unavailable or content != last_sent_bytes:
                        # Preserve byte-change detection while framing every JSON line.
                        payload = b"\n".join(b"data: " + line for line in content.splitlines())
                        msg = f"event: plan\nid: {data['revision']}\n".encode("utf-8") + payload + b"\n\n"
                        self.wfile.write(msg)
                        self.wfile.flush()
                        last_sent_bytes = content
                        state_unavailable = False

                now = time.time()
                if now - last_heartbeat >= 15.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now

                self.server.stop_event.wait(0.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


def serve(
    state_dir: str,
    port: int,
    ready_file: str,
    assets_dir: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Start the localhost server and write ready-file upon successful bind."""
    if assets_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(script_dir)
        assets_dir = os.path.join(repo_root, "assets", "dashboard")

    os.makedirs(state_dir, exist_ok=True)
    ready_dir = os.path.dirname(os.path.abspath(ready_file))
    if ready_dir:
        os.makedirs(ready_dir, exist_ok=True)

    server = ThreadedHTTPServer(("127.0.0.1", port), LivePlanRequestHandler, state_dir, assets_dir)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    token = secrets.token_hex(16)

    ready_data = {
        "host": "127.0.0.1",
        "port": actual_port,
        "pid": os.getpid(),
        "token": token,
        "url": url,
        "stateDir": os.path.abspath(state_dir)
    }

    ready_lock_path = os.path.join(ready_dir, "server.json.lock") if ready_dir else "server.json.lock"

    # Write ready file atomically under ready lock
    with FileLock(ready_lock_path):
        ready_tmp = f"{ready_file}.tmp.{os.getpid()}.{time.time_ns()}"
        try:
            with open(ready_tmp, "w", encoding="utf-8") as rf:
                json.dump(ready_data, rf, indent=2)
                rf.flush()
                os.fsync(rf.fileno())
            _atomic_replace_with_retry(ready_tmp, ready_file, timeout=2.0)
        except Exception as e:
            server.server_close()
            if os.path.exists(ready_tmp):
                try:
                    os.remove(ready_tmp)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write ready file {ready_file}: {e}") from e

    if stop_event is not None:
        def _stop_monitor():
            stop_event.wait()
            server.stop_event.set()
            server.shutdown()
        threading.Thread(target=_stop_monitor, daemon=True).start()

    try:
        import signal
        def _sig_term(signum: int, frame: Any) -> None:
            server.stop_event.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, _sig_term)
    except (ValueError, AttributeError):
        pass

    print(f"Live plan server listening on {url} (PID: {os.getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop_event.set()
        server.server_close()
        # Clean up ready file only if it still belongs to this server instance
        try:
            with FileLock(ready_lock_path):
                if os.path.exists(ready_file):
                    try:
                        with open(ready_file, "r", encoding="utf-8") as f:
                            stored_rf = json.load(f)
                        if stored_rf.get("token") == token:
                            os.remove(ready_file)
                    except Exception:
                        pass
        except Exception:
            pass


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live Plan dashboard publisher, recovery tool, and server.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # publish command
    p_pub = subparsers.add_parser("publish", help="Publish a validated plan update.")
    p_pub.add_argument("--state-dir", required=True, help="Directory where canonical plan state is stored.")
    p_pub.add_argument("--input", required=True, help="Path to the proposed plan JSON file.")
    p_pub.add_argument("--expected-revision", type=int, required=True, help="Expected current revision (0 to initialize).")
    p_pub.add_argument("--expected-generation", default=None, help="Expected generation UUID (or 'none' for v1 migration; omitted for revision 0).")
    p_pub.add_argument("--authorization-note", default=None, help="Authorization note quoting or paraphrasing user instructions.")

    # recover command
    p_rec = subparsers.add_parser("recover", help="Recover from corrupted or unreadable canonical plan state.")
    p_rec.add_argument("--state-dir", required=True, help="Directory where canonical plan state is stored.")
    p_rec.add_argument("--input", required=True, help="Path to the replacement plan JSON file.")
    p_rec.add_argument("--recovery-note", required=True, help="Explanation of imported, reverified, or omitted steps.")
    p_rec.add_argument("--authorization-note", default=None, help="Authorization note required for executing modes.")

    # serve command
    p_srv = subparsers.add_parser("serve", help="Start the localhost dashboard server.")
    p_srv.add_argument("--state-dir", required=True, help="Directory where canonical plan state is stored.")
    p_srv.add_argument("--port", type=int, default=0, help="Port to bind (default 0 selects ephemeral port).")
    p_srv.add_argument("--ready-file", required=True, help="Path to write readiness JSON after successful bind.")
    p_srv.add_argument("--assets-dir", default=None, help="Optional directory containing dashboard web assets.")

    return parser


def main() -> None:
    parser = build_cli()
    args = parser.parse_args()

    if args.command == "publish":
        result = None
        try:
            result = publish(
                state_dir=args.state_dir,
                input_file=args.input,
                expected_revision=args.expected_revision,
                expected_generation=args.expected_generation,
                authorization_note=args.authorization_note,
            )
            print(f"Successfully published revision {result['revision']} (generation {result['generation']}) for task '{result['taskId']}'.")
        except RetryableStorageError as e:
            sys.stderr.write(f"{e}\n")
            sys.exit(75)
        except PlanValidationError as e:
            sys.stderr.write(f"Validation error: {e}\n")
            sys.exit(1)
        except Exception as e:
            if result is not None:
                sys.stderr.write(f"Committed revision {result['revision']} (generation {result['generation']}); success reporting failed: {e}\n")
            else:
                sys.stderr.write(f"Error: {e}\n")
            sys.exit(1)

    elif args.command == "recover":
        result = None
        try:
            result = recover(
                state_dir=args.state_dir,
                input_file=args.input,
                recovery_note=args.recovery_note,
                authorization_note=args.authorization_note,
            )
            print(f"Successfully recovered to revision {result['revision']} (generation {result['generation']}). Corrupt backup: {result.get('_corruptBackup')}")
        except RetryableStorageError as e:
            sys.stderr.write(f"{e}\n")
            sys.exit(75)
        except PlanValidationError as e:
            sys.stderr.write(f"Validation error: {e}\n")
            sys.exit(1)
        except Exception as e:
            if result is not None:
                sys.stderr.write(f"Committed recovery revision {result['revision']} (generation {result['generation']}); completed backup: {result.get('_corruptBackup')}; success reporting failed: {e}\n")
            else:
                sys.stderr.write(f"Error: {e}\n")
            sys.exit(1)

    elif args.command == "serve":
        try:
            serve(args.state_dir, args.port, args.ready_file, args.assets_dir)
        except Exception as e:
            sys.stderr.write(f"Server error: {e}\n")
            sys.exit(1)


if __name__ == "__main__":
    main()
