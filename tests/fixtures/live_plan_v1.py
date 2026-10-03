#!/usr/bin/env python3
"""
live_plan.py - Publisher and localhost server for the Live Plan dashboard.

Standard-library only (Python 3.10+). No external dependencies.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
from typing import Any

SCHEMA_VERSION = 1
VALID_TASK_STATUSES = {"planning", "running", "stopped", "blocked", "complete"}
VALID_EXECUTION_MODES = {"plan-only", "first-step", "complete-task"}
VALID_STEP_STATUSES = {"pending", "in-progress", "complete", "blocked"}

MAX_STEPS = 200
MAX_HISTORY = 100
MAX_TEXT_LEN = 10000
MAX_EVIDENCE_ITEMS = 50
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB limit


class PlanValidationError(Exception):
    """Raised when a plan snapshot fails schema or graph validation."""
    pass


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
        # First acquire process-local lock
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


def validate_plan(proposed: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    """
    Validate a proposed plan dictionary according to schema and graph rules.
    If existing state is provided, enforce consistency constraints between revisions.
    """
    if not isinstance(proposed, dict):
        raise PlanValidationError("Plan payload must be a JSON object")

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

    # Validate step items
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
        if not isinstance(s_status, str) or s_status not in VALID_STEP_STATUSES:
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

    # Prerequisite status check: in-progress and complete steps require completed prerequisites
    for sid, step in steps_by_id.items():
        if step["status"] in ("in-progress", "complete"):
            for dep in step["dependsOn"]:
                dep_step = steps_by_id[dep]
                if dep_step["status"] != "complete":
                    raise PlanValidationError(
                        f"Step '{sid}' is '{step['status']}' but its prerequisite '{dep}' is not complete (status: '{dep_step['status']}')"
                    )

    # Task status and step status consistency
    if task_status == "complete":
        if not steps:
            raise PlanValidationError("Task status is 'complete' but has no steps")
        for sid, step in steps_by_id.items():
            if step["status"] != "complete":
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

    # nextStepId validation
    if "nextStepId" not in proposed:
        raise PlanValidationError("Missing required field 'nextStepId' (must be a step ID string or null)")
    next_step_id = proposed.get("nextStepId")
    if next_step_id is not None:
        if not isinstance(next_step_id, str):
            raise PlanValidationError("Field 'nextStepId' must be a string or null")
        if next_step_id not in id_set:
            raise PlanValidationError(f"Field 'nextStepId' references unknown step '{next_step_id}'")
        if steps_by_id[next_step_id]["status"] == "complete":
            raise PlanValidationError(f"Field 'nextStepId' references already completed step '{next_step_id}'")

    # Multi-revision constraints against existing state
    if existing is not None:
        if proposed["taskId"] != existing.get("taskId"):
            raise PlanValidationError(
                f"Cannot change taskId: existing '{existing.get('taskId')}', proposed '{proposed['taskId']}'"
            )
        if proposed["objective"] != existing.get("objective"):
            raise PlanValidationError(
                f"Cannot change objective: existing '{existing.get('objective')}', proposed '{proposed['objective']}'"
            )

        existing_steps = existing.get("steps", [])
        old_completed_ids = set()

        for old_step in existing_steps:
            old_id = old_step.get("id")
            old_status = old_step.get("status")
            if old_status == "complete":
                old_completed_ids.add(old_id)
                if old_id not in id_set:
                    raise PlanValidationError(f"Previously completed step '{old_id}' cannot be removed")
                new_step = steps_by_id[old_id]
                if new_step["status"] != "complete":
                    raise PlanValidationError(f"Previously completed step '{old_id}' cannot be marked incomplete")

                for field in ("title", "check"):
                    if new_step[field] != old_step[field]:
                        raise PlanValidationError(f"Previously completed step '{old_id}' cannot change '{field}'")
                if set(new_step["dependsOn"]) != set(old_step["dependsOn"]):
                    raise PlanValidationError(f"Previously completed step '{old_id}' cannot change dependencies")

                # Verify evidence was not discarded
                old_evidence = set(old_step.get("evidence", []))
                new_evidence = set(new_step.get("evidence", []))
                if not old_evidence.issubset(new_evidence):
                    missing_ev = old_evidence - new_evidence
                    raise PlanValidationError(f"Previously verified evidence for step '{old_id}' was discarded: {missing_ev}")

        # first-step mode limitation: at most 1 newly completed step per update
        new_completed_ids = {sid for sid, s in steps_by_id.items() if s["status"] == "complete"}
        newly_completed = new_completed_ids - old_completed_ids
        if exec_mode == "first-step" and len(newly_completed) > 1:
            raise PlanValidationError(
                f"first-step mode cannot have >1 newly completed step in a single update (completed: {sorted(newly_completed)})"
            )
    else:
        # Initial creation in first-step mode
        if exec_mode == "first-step":
            completed_count = sum(1 for s in steps if s.get("status") == "complete")
            if completed_count > 1:
                raise PlanValidationError(
                    f"first-step mode cannot initialize with more than 1 completed step (found {completed_count})"
                )


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
        return json.loads(content.decode("utf-8")), content
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


def read_published_snapshot(path: str) -> tuple[dict[str, Any], bytes]:
    """Return only a bounded, schema-valid published snapshot and its original bytes."""
    data, content = _read_bounded_json(path, reject_symlink=True)
    # Compare to itself to validate a snapshot without treating accumulated first-step
    # completions as newly completed work in an initial publication.
    validate_plan(data, data)
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
    try:
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanValidationError("Snapshot cannot be serialized as valid JSON") from exc
    if len(content) > MAX_FILE_BYTES:
        raise PlanValidationError("Serialized snapshot exceeds maximum allowed size")
    return data, content


def publish(state_dir: str, input_file: str, expected_revision: int) -> dict[str, Any]:
    """Publish a validated plan update under an exclusive lock with atomic replacement."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise PlanValidationError("Field 'expected-revision' must be a non-negative integer (>= 0)")

    os.makedirs(state_dir, exist_ok=True)
    canonical_path = os.path.join(state_dir, "live_plan.json")
    lock_path = os.path.join(state_dir, "live_plan.lock")

    if not os.path.isfile(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")

    proposed, _ = _read_bounded_json(input_file)

    with FileLock(lock_path):
        existing: dict[str, Any] | None = None
        current_revision = 0

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
        else:
            if expected_revision != 0:
                raise PlanValidationError(
                    f"Revision conflict: no existing state file found, but expected revision is {expected_revision} (must be 0 for initial publish)."
                )

        # Validate structure, DAG, constraints
        validate_plan(proposed, existing)

        # Build published state
        new_revision = current_revision + 1
        timestamp = datetime.now(timezone.utc)
        if existing is not None:
            timestamp = max(timestamp, _utc_timestamp(existing["updatedAt"]))
        new_updated_at = timestamp.isoformat()
        new_history_entry = {
            "revision": new_revision,
            "timestamp": new_updated_at,
            "summary": proposed["changeSummary"]
        }

        existing_history: list[dict[str, Any]] = existing.get("history", []) if existing else []
        new_history = existing_history + [new_history_entry]
        if len(new_history) > MAX_HISTORY:
            new_history = new_history[-MAX_HISTORY:]

        published_state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "revision": new_revision,
            "updatedAt": new_updated_at,
            "taskId": proposed["taskId"],
            "objective": proposed["objective"],
            "status": proposed["status"],
            "executionMode": proposed["executionMode"],
            "nextStepId": proposed.get("nextStepId"),
            "changeSummary": proposed["changeSummary"],
            "steps": proposed["steps"],
            "history": new_history
        }

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
            os.replace(tmp_path, canonical_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        return published_state


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded localhost HTTP server with daemon threads."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[http.server.BaseHTTPRequestHandler], state_dir: str, assets_dir: str) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.state_dir = os.path.abspath(state_dir)
        self.assets_dir = os.path.abspath(assets_dir)
        self.stop_event = threading.Event()


class LivePlanRequestHandler(http.server.BaseHTTPRequestHandler):
    """Strict localhost request handler serving dashboard assets and live SSE plan events."""
    server_version = "LivePlanDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine console access logs to keep CLI clean
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

        # Whitelist exact paths
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
        # This response owns the connection until it exits; never wait for another
        # HTTP request after a disconnected SSE client or server shutdown.
        self.close_connection = True

        last_sent_key: tuple[str | None, int] = (None, -1)
        state_unavailable = False
        last_heartbeat = time.time()
        canonical_path = os.path.join(self.server.state_dir, "live_plan.json")

        try:
            while not self.server.stop_event.is_set():
                try:
                    data, content = read_published_snapshot(canonical_path)
                except (PlanValidationError, OSError):
                    if not state_unavailable:
                        self.wfile.write(b'event: state-error\ndata: {"message":"Published plan unavailable or invalid; retaining last valid snapshot."}\n\n')
                        self.wfile.flush()
                    state_unavailable = True
                else:
                    cache_key = (data["taskId"], data["revision"])
                    if cache_key != last_sent_key:
                        msg = f"event: plan\nid: {data['revision']}\ndata: ".encode("utf-8") + content + b"\n\n"
                        self.wfile.write(msg)
                        self.wfile.flush()
                        last_sent_key = cache_key
                    elif state_unavailable:
                        self.wfile.write(b'event: state-ok\ndata: {"message":"Published plan available again."}\n\n')
                        self.wfile.flush()
                    state_unavailable = False

                now = time.time()
                if now - last_heartbeat >= 15.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now

                self.server.stop_event.wait(0.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client disconnected
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
        # Default: locate assets/dashboard relative to scripts/
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

    ready_data = {
        "host": "127.0.0.1",
        "port": actual_port,
        "pid": os.getpid(),
        "url": url,
        "stateDir": os.path.abspath(state_dir)
    }

    # Write ready file atomically after successful bind
    ready_tmp = f"{ready_file}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        with open(ready_tmp, "w", encoding="utf-8") as rf:
            json.dump(ready_data, rf, indent=2)
            rf.flush()
            os.fsync(rf.fileno())
        os.replace(ready_tmp, ready_file)
    except Exception as e:
        server.server_close()
        if os.path.exists(ready_tmp):
            try:
                os.remove(ready_tmp)
            except OSError:
                pass
        raise RuntimeError(f"Failed to write ready file {ready_file}: {e}") from e

    # Watchdog thread if stop_event is provided
    if stop_event is not None:
        def _stop_monitor():
            stop_event.wait()
            server.stop_event.set()
            server.shutdown()
        threading.Thread(target=_stop_monitor, daemon=True).start()

    # Register SIGTERM handler if available
    try:
        import signal
        def _sig_term(signum: int, frame: Any) -> None:
            server.stop_event.set()
            # shutdown() must run outside the thread executing serve_forever().
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
        if os.path.exists(ready_file):
            try:
                os.remove(ready_file)
            except OSError:
                pass


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live Plan dashboard publisher and server.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # publish command
    p_pub = subparsers.add_parser("publish", help="Publish a validated plan update.")
    p_pub.add_argument("--state-dir", required=True, help="Directory where canonical plan state is stored.")
    p_pub.add_argument("--input", required=True, help="Path to the proposed plan JSON file.")
    p_pub.add_argument("--expected-revision", type=int, required=True, help="Expected current revision (0 to initialize).")

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
        try:
            result = publish(args.state_dir, args.input, args.expected_revision)
            print(f"Successfully published revision {result['revision']} for task '{result['taskId']}'.")
        except PlanValidationError as e:
            sys.stderr.write(f"Validation error: {e}\n")
            sys.exit(1)
        except Exception as e:
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
