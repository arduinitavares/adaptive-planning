"""Read-only links from optional measurements to the canonical service."""
from __future__ import annotations

import json
import logging
import sqlite3

from adaptive_planning import measurements
from adaptive_planning.ownership import hash_token, _require_attempt_capability
from adaptive_planning.types import DomainError


def ledger(service):
    if getattr(service, "_measurement_ledger", None) is None:
        service._measurement_ledger = measurements.Ledger(service._store.path.parent / "measurements.sqlite")
        service._measurement_version = measurements.fingerprint()
    return service._measurement_ledger


def record(service, observation, orchestrator_token=None, attempt_token=None):
    measurements.validate_observation(observation)
    if orchestrator_token is not None and attempt_token is not None:
        raise DomainError("invalid", "Supply one attribution capability")

    def authorize(db):
        epoch = db.execute("SELECT epoch FROM installation WHERE id=1").fetchone()[0]
        if observation["epoch"] != epoch:
            raise DomainError("stale_epoch", "Installation epoch changed")
        plan_id = observation["plan_id"]
        plan = db.execute("SELECT generation,revision FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise DomainError("not_found", "Plan not found")
        step_id, attempt_id = observation["step_id"], observation["attempt_id"]
        if step_id and db.execute("SELECT 1 FROM steps WHERE plan_id=? AND step_id=?", (plan_id, step_id)).fetchone() is None:
            raise DomainError("not_found", "Step not found")
        attempt = None
        if attempt_id:
            attempt = db.execute("SELECT step_id,assignee,state,payload FROM attempts WHERE plan_id=? AND attempt_id=?", (plan_id, attempt_id)).fetchone()
            if attempt is None or step_id is not None and attempt[0] != step_id:
                raise DomainError("not_found", "Attempt scope does not match")
        cp = observation["checkpoint_sequence"]
        if cp is not None:
            checkpoint = db.execute("SELECT outcome FROM checkpoints WHERE plan_id=? AND attempt_id=? AND sequence=?", (plan_id, attempt_id, cp)).fetchone()
            if checkpoint is None:
                raise DomainError("not_found", "Checkpoint scope does not match")
        role = "service_bearer_context"
        if orchestrator_token is not None:
            if not isinstance(orchestrator_token, str) or not orchestrator_token:
                raise DomainError("forbidden", "Invalid attribution capability")
            cap = db.execute("SELECT revoked,valid_until FROM capabilities WHERE epoch=? AND plan_id=? AND role='orchestrator' AND secret_hash=?", (epoch, plan_id, hash_token(orchestrator_token))).fetchone()
            if cap is None or cap[1] is not None:
                raise DomainError("forbidden", "Invalid attribution capability")
            if cap[0]:
                raise DomainError("fenced", "Attribution capability was fenced")
            role = "orchestrator"
        elif attempt_token is not None:
            _require_attempt_capability(db, plan_id, plan[0], attempt_id, attempt_token)
            if not step_id or observation["kind"] == "coverage":
                raise DomainError("forbidden", "Worker observations require exact step scope")
            role = "attempt"
        elif observation["kind"] != "context":
            raise DomainError("forbidden", "An attribution capability is required")
        if observation["kind"] == "outcome" and observation["data"]["status"] == "verified":
            data = observation["data"]
            accepted = json.loads(attempt[3]) if attempt else {}
            state = db.execute("SELECT state FROM steps WHERE plan_id=? AND step_id=?", (plan_id, step_id)).fetchone()[0]
            if (role != "orchestrator" or attempt[2] != "accepted" or state != "complete" or checkpoint[0] != "passed"
                    or accepted.get("checkpoint_sequence") != cp or accepted.get("result_version") != data["result_version"]
                    or data["verifier"] == attempt[1]):
                raise DomainError("invalid", "Independent verification must match the current accepted output")
        return {"role": role, "checked_plan_revision": plan[1]}

    provenance = service._run_read(authorize)
    try:
        store = ledger(service)
        return store.record(observation, provenance, service._clock().isoformat(), service._measurement_version)
    except sqlite3.OperationalError:
        raise DomainError("busy", "Measurement ledger unavailable") from None


def read(service, plan_id=None, after=0, limit=100):
    if (plan_id is not None and (not isinstance(plan_id, str) or not plan_id.strip())
            or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100):
        raise DomainError("invalid", "Invalid measurement page")

    def canonical(db):
        epoch = db.execute("SELECT epoch FROM installation WHERE id=1").fetchone()[0]
        sql = "SELECT plan_id,title,status,created_at FROM plans"
        rows = db.execute(sql + (" WHERE plan_id=?" if plan_id else "") + " ORDER BY plan_id LIMIT 101", (plan_id,) if plan_id else ()).fetchall()
        if plan_id and not rows:
            raise DomainError("not_found", "Plan not found")
        if len(rows) > 100:
            raise DomainError("limit_exceeded", "Select one plan for the report")
        cases = []
        for pid, title, status, created_at in rows:
            steps = []
            for sid, state in db.execute("SELECT step_id,state FROM steps WHERE plan_id=? ORDER BY step_id", (pid,)):
                current = db.execute("SELECT attempt_id,payload FROM attempts WHERE plan_id=? AND step_id=? ORDER BY rowid DESC LIMIT 1", (pid, sid)).fetchone()
                data = json.loads(current[1]) if current else {}
                version = data.get("result_version")
                result = db.execute("SELECT created_at FROM result_versions WHERE plan_id=? AND result_version=?", (pid, version)).fetchone() if version else None
                steps.append({"step_id": sid, "state": state, "attempt_id": current[0] if current else None,
                              "result_version": version, "accepted_at": result[0] if result else None})
            cases.append({"plan_id": pid, "title": title, "status": status, "created_at": created_at, "steps": steps})
        return epoch, cases, db.execute("SELECT COALESCE(max(sequence),0) FROM events").fetchone()[0]

    epoch, cases, sequence = service._run_read(canonical)
    try:
        store = ledger(service)
        rows = store.rows(epoch, plan_id)
        result = measurements.report(cases, rows, service._measurement_version)
        page = [r for r in rows if r["sequence"] > after][:limit + 1]
        result.update(epoch=epoch, canonical_store_sequence=sequence, observations=page[:limit],
                      next_after=page[limit - 1]["sequence"] if len(page) > limit else None,
                      mcp_calls=store.calls(), collection_health={"dropped_call_traces_since_process_start": getattr(service, "_measurement_drops", 0)})
        return result
    except sqlite3.OperationalError:
        raise DomainError("busy", "Measurement ledger unavailable") from None


def trace(service, tool, duration_ms, outcome):
    try:
        store = ledger(service)
        store.call(tool, duration_ms, outcome, service._measurement_version["code"])
    except Exception:
        service._measurement_drops = getattr(service, "_measurement_drops", 0) + 1
        logging.getLogger(__name__).warning("MCP measurement trace could not be retained")
