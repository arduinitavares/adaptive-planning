"""Optional observations. This module never writes planning state or grants authority."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone

from adaptive_planning.types import DomainError

METRICS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "tool_calls",
           "worker_seconds", "human_seconds", "reviewer_seconds")
KINDS = {"context", "outcome", "defect", "effort", "coverage"}
VERSION_FIELDS = {"workflow_version", "skill_version", "model_id", "provider", "effort"}
MAX_ROWS = 20000


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise DomainError("invalid", "A timezone-aware timestamp is required") from None


def _text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise DomainError("invalid", "A bounded nonblank string is required")


def _number(value):
    if type(value) not in (float, int) or value < 0 or value > 1e15 or not math.isfinite(value):
        raise DomainError("invalid", "A finite nonnegative measured value is required")


def validate_observation(value):
    allowed = {"source_record_id", "epoch", "plan_id", "kind", "observed_at", "step_id", "attempt_id",
               "checkpoint_sequence", "data"}
    if (not isinstance(value, dict) or set(value) != allowed or not isinstance(value["kind"], str)
            or value["kind"] not in KINDS):
        raise DomainError("invalid", "Unsupported observation fields or kind")
    for name in ("source_record_id", "epoch", "plan_id"):
        _text(value[name], 160)
    for name in ("step_id", "attempt_id"):
        if value[name] is not None:
            _text(value[name], 160)
    if value["checkpoint_sequence"] is not None and (type(value["checkpoint_sequence"]) is not int or value["checkpoint_sequence"] < 1):
        raise DomainError("invalid", "Checkpoint sequence must be a positive integer")
    if value["checkpoint_sequence"] is not None and value["attempt_id"] is None:
        raise DomainError("invalid", "Checkpoint attribution requires its attempt")
    timestamp(value["observed_at"])
    data, kind = value["data"], value["kind"]
    fields = {
        "context": VERSION_FIELDS | {"case_kind", "evidence"},
        "outcome": {"status", "independent", "verifier", "evidence", "result_version"},
        "defect": {"defect_id", "stage", "status", "cause", "evidence"},
        "effort": {"activity_kind", "corrective_rework", "cause_id", "metrics", "source", "evidence",
                   "started_at", "ended_at", "time_kind", "billing_amount", "estimated_amount", "currency", "billing_scope"},
        "coverage": {"metrics", "evidence"},
    }[kind]
    if not isinstance(data, dict) or not data or set(data) - fields:
        raise DomainError("invalid", "Unsupported measurement data")
    if len(json.dumps(data, allow_nan=True)) > 12000:
        raise DomainError("limit_exceeded", "Observation is too large")
    if kind == "context":
        for name, item in data.items():
            _text(item, 500)
        if "case_kind" in data and (data["case_kind"] not in ("operational", "fixture", "unknown") or value["attempt_id"] is not None):
            raise DomainError("invalid", "Classification is plan scoped")
    else:
        _text(data.get("evidence"))
    if kind == "outcome":
        if not value["step_id"] or data.get("status") not in ("verified", "failed", "blocked", "unresolved"):
            raise DomainError("invalid", "Outcome requires a step and an explicit status")
        if "independent" in data and type(data["independent"]) is not bool:
            raise DomainError("invalid", "Independence must be explicit")
        if data["status"] == "verified":
            if not data.get("independent") or not value["attempt_id"] or not value["checkpoint_sequence"]:
                raise DomainError("invalid", "Verified outcomes require independent evidence and exact checkpoint")
            _text(data.get("verifier"), 500)
            _text(data.get("result_version"), 160)
    if kind == "defect":
        for name in ("defect_id", "cause"):
            _text(data.get(name))
        if data.get("stage") not in ("caught", "escaped") or data.get("status") not in ("open", "resolved"):
            raise DomainError("invalid", "Defect stage and status are required")
    if kind == "coverage":
        metrics = data.get("metrics")
        if (not isinstance(metrics, list) or not metrics or any(not isinstance(m, str) for m in metrics)
                or len(set(metrics)) != len(metrics) or set(metrics) - set(METRICS) - {"regressions"}):
            raise DomainError("invalid", "Coverage must name supported metrics")
        if value["step_id"] or value["attempt_id"]:
            raise DomainError("invalid", "Complete coverage is a plan-level attestation")
    if kind == "effort":
        if data.get("activity_kind") not in ("plan", "implement", "verify", "review", "debug", "recover"):
            raise DomainError("invalid", "Activity category is required")
        if type(data.get("corrective_rework")) is not bool:
            raise DomainError("invalid", "Corrective rework must be explicit")
        if data["corrective_rework"]:
            _text(data.get("cause_id"), 2000)
        elif data.get("cause_id") is not None:
            raise DomainError("invalid", "A rework cause requires corrective rework")
        if data.get("source") not in ("provider_receipt", "client_measured", "estimate"):
            raise DomainError("invalid", "Measurement source is required")
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict) or set(metrics) - set(METRICS):
            raise DomainError("invalid", "Unsupported resource metric")
        for metric, amount in metrics.items():
            _number(amount)
            if metric.endswith("tokens") or metric == "tool_calls":
                if type(amount) is not int:
                    raise DomainError("invalid", "Token and call counts must be integers")
        for subset, whole in (("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")):
            if subset in metrics and whole in metrics and metrics[subset] > metrics[whole]:
                raise DomainError("invalid", "Subset token counts exceed their total")
        for name in ("billing_amount", "estimated_amount"):
            if name in data:
                _number(data[name])
                currency = data.get("currency")
                if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isupper() or not currency.isalpha():
                    raise DomainError("invalid", "Use a three-letter currency code")
                if data.get("billing_scope") not in ("isolated", "shared"):
                    raise DomainError("invalid", "Billing allocation must be explicit")
                if name == "billing_amount" and data["source"] != "provider_receipt":
                    raise DomainError("invalid", "Actual billing requires a provider receipt")
        spans = [name in data for name in ("started_at", "ended_at", "time_kind")]
        if any(spans):
            if not all(spans) or data["time_kind"] not in ("active", "wait", "blocked"):
                raise DomainError("invalid", "A complete measured interval is required")
            if timestamp(data["ended_at"]) < timestamp(data["started_at"]):
                raise DomainError("invalid", "Interval end precedes its start")
        if not metrics and not any(spans) and "billing_amount" not in data and "estimated_amount" not in data:
            raise DomainError("invalid", "Effort requires a measured quantity")


def fingerprint():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {"code": digest.hexdigest(), "measurement_schema": 1, "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version}


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        for candidate in (self.path, Path(str(self.path) + "-journal"), Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if candidate.is_symlink():
                raise DomainError("invalid", "Measurement paths cannot be symbolic links")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DomainError("invalid", "Measurement database must be an unlinked regular file")
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DomainError("schema_newer", "Measurement schema is newer")
            db.execute("CREATE TABLE IF NOT EXISTS observations(sequence INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL, plan_id TEXT NOT NULL, source_record_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(epoch,source_record_id))")
            db.execute("CREATE INDEX IF NOT EXISTS observation_plan ON observations(epoch,plan_id,sequence)")
            db.execute("CREATE TABLE IF NOT EXISTS calls(sequence INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL, tool TEXT NOT NULL, duration_ms REAL NOT NULL, outcome TEXT NOT NULL, service_code TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, value, provenance, recorded_at, service_version):
        validate_observation(value)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT sequence,digest FROM observations WHERE epoch=? AND source_record_id=?",
                                  (value["epoch"], value["source_record_id"])).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise DomainError("conflict", "Source record ID already has different content")
                return {"sequence": previous["sequence"], "replayed": True}
            if value["kind"] in ("effort", "defect"):
                history = [json.loads(r[0]) for r in db.execute("SELECT payload FROM observations WHERE epoch=? AND plan_id=?", (value["epoch"], value["plan_id"]))]
                data = value["data"]
                if value["kind"] == "effort" and data.get("corrective_rework"):
                    if not any(r["kind"] == "defect" and r["data"]["defect_id"] == data["cause_id"] for r in history):
                        raise DomainError("invalid", "Rework must link a recorded defect in this plan")
                if value["kind"] == "defect":
                    previous_defects = [r for r in history if r["kind"] == "defect" and r["data"]["defect_id"] == data["defect_id"]]
                    if previous_defects and any(previous_defects[-1]["data"][f] != data[f] for f in ("cause", "stage")):
                        raise DomainError("conflict", "A defect's cause and stage are immutable")
            payload = {**value, "provenance": provenance, "recorded_at": recorded_at, "service_version": service_version}
            cursor = db.execute("INSERT INTO observations(epoch,plan_id,source_record_id,digest,payload) VALUES(?,?,?,?,?)",
                                (value["epoch"], value["plan_id"], value["source_record_id"], digest, json.dumps(payload, allow_nan=False)))
            return {"sequence": cursor.lastrowid, "replayed": False}

    def rows(self, epoch, plan_id=None):
        with self._connect() as db:
            sql = "SELECT sequence,payload FROM observations WHERE epoch=?"
            args = [epoch]
            if plan_id is not None:
                sql += " AND plan_id=?"
                args.append(plan_id)
            rows = db.execute(sql + " ORDER BY sequence LIMIT ?", (*args, MAX_ROWS + 1)).fetchall()
            if len(rows) > MAX_ROWS:
                raise DomainError("limit_exceeded", "Select one plan to bound measurement aggregation")
            return [{**json.loads(r["payload"]), "sequence": r["sequence"]} for r in rows]

    def call(self, tool, duration_ms, outcome, service_code):
        with self._connect() as db:
            db.execute("INSERT INTO calls(observed_at,tool,duration_ms,outcome,service_code) VALUES(?,?,?,?,?)",
                       (datetime.now(timezone.utc).isoformat(), tool, duration_ms, outcome, service_code))

    def calls(self, limit=20):
        with self._connect() as db:
            total = db.execute("SELECT count(*) FROM calls").fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY sequence DESC LIMIT ?", (limit,))]
            return {"count": total, "recent": rows, "scope": "MCP tool calls observed by this server only"}


def union_seconds(intervals):
    points = sorted((timestamp(a), timestamp(b)) for a, b in intervals)
    total, end = 0.0, None
    for start, stop in points:
        if end is None or start > end:
            total += (stop - start).total_seconds()
        elif stop > end:
            total += (stop - end).total_seconds()
        end = max(end, stop) if end is not None else stop
    return total


def report(cases, observations, service_version):
    results = []
    for case in cases:
        rows = [r for r in observations if r["plan_id"] == case["plan_id"]]
        contexts = [r for r in rows if r["kind"] == "context"]
        classification = next((r["data"]["case_kind"] for r in reversed(contexts) if "case_kind" in r["data"]), "unknown")
        efforts = [r for r in rows if r["kind"] == "effort"]
        covered = {}
        for r in rows:
            if r["kind"] == "coverage":
                for name in r["data"]["metrics"]:
                    covered[name] = r["sequence"]
        def quantities(rework=False):
            result = {}
            for name in METRICS:
                items = [r for r in efforts if name in r["data"].get("metrics", {}) and r["data"]["source"] != "estimate"]
                complete = name in covered and all(r["sequence"] < covered[name] for r in items)
                chosen = [r for r in items if not rework or r["data"]["corrective_rework"]]
                known = sum(r["data"]["metrics"][name] for r in chosen) if items or complete else None
                result[name] = {"known_sum": known, "coverage": "client_attested_complete" if complete else "partial" if items else "none"}
            return result
        defects = {}
        for r in rows:
            if r["kind"] == "defect":
                defects[r["data"]["defect_id"]] = r["data"]
        counts = dict(verified=0, accepted_without_attestation=0, failed=0, blocked=0, unresolved=0)
        elapsed = []
        for step in case["steps"]:
            eligible = []
            for row in rows:
                if row["kind"] != "outcome" or row["step_id"] != step["step_id"]:
                    continue
                data = row["data"]
                if step.get("attempt_id") and row["attempt_id"] is not None and row["attempt_id"] != step["attempt_id"]:
                    continue
                if step["state"] == "complete":
                    provenance = row.get("provenance", {})
                    role = provenance.get("role") if isinstance(provenance, dict) else provenance
                    if role != "orchestrator":
                        continue
                    if data["status"] == "verified":
                        if data["result_version"] != step.get("result_version"):
                            continue
                    elif (step.get("accepted_at") and timestamp(row["observed_at"]) < timestamp(step["accepted_at"])):
                        continue
                    elif data.get("result_version") is not None and data["result_version"] != step.get("result_version"):
                        continue
                elif data["status"] == "verified":
                    continue
                eligible.append(row)
            attestation = max(eligible, key=lambda r: (timestamp(r["observed_at"]), r["sequence"]), default=None)
            valid = attestation and attestation["data"]["status"] == "verified" and step["state"] == "complete" and attestation["data"].get("result_version") == step.get("result_version")
            explicit = attestation and attestation["data"]["status"] != "verified" and (not step.get("accepted_at") or timestamp(attestation["observed_at"]) >= timestamp(step["accepted_at"]))
            status = "verified" if valid else attestation["data"]["status"] if explicit else "accepted_without_attestation" if step["state"] == "complete" else "blocked" if step["state"] == "blocked" else "unresolved"
            counts[status] += 1
            if valid and step.get("accepted_at"):
                elapsed.append({"step_id": step["step_id"], "seconds": max(0, (timestamp(step["accepted_at"]) - timestamp(case["created_at"])).total_seconds())})
        money = {}
        for r in efforts:
            d = r["data"]
            for field in ("billing_amount", "estimated_amount"):
                if field in d:
                    key = f'{field}:{d["currency"]}:{d["billing_scope"]}'
                    bucket = money.setdefault(key, {"known_sum": 0, "rework_known_sum": 0, "coverage": "partial"})
                    bucket["known_sum"] += d[field]
                    if d["corrective_rework"]:
                        bucket["rework_known_sum"] += d[field]
        regression_complete = "regressions" in covered and all(r["sequence"] < covered["regressions"] for r in rows if r["kind"] == "defect")
        results.append({"plan_id": case["plan_id"], "title": case.get("title", ""), "status": case["status"], "case_kind": classification,
                        "versions": [{"attempt_id": r["attempt_id"], "observed_at": r["observed_at"], **r["data"]} for r in contexts],
                        "outcomes": {**counts, "denominator_steps": len(case["steps"]), "verification_basis": "canonical acceptance plus client independent-verification attestation"},
                        "regressions": {"caught": sum(d["stage"] == "caught" for d in defects.values()) if defects or regression_complete else None,
                                        "escaped": sum(d["stage"] == "escaped" for d in defects.values()) if defects or regression_complete else None,
                                        "open": sum(d["status"] == "open" for d in defects.values()) if defects or regression_complete else None,
                                        "coverage": "client_attested_complete" if regression_complete else "partial" if defects else "none"},
                        "resources": quantities(), "rework": quantities(True), "money": money or None,
                        "estimated_resources": {name: sum(r["data"]["metrics"][name] for r in efforts if r["data"]["source"] == "estimate" and name in r["data"].get("metrics", {})) for name in METRICS if any(r["data"]["source"] == "estimate" and name in r["data"].get("metrics", {}) for r in efforts)} or None,
                        "time": {"creation_to_accepted_seconds": elapsed, "censored_steps": len(case["steps"]) - counts["verified"],
                                 "measured_wall_seconds": {kind: union_seconds([(r["data"]["started_at"], r["data"]["ended_at"]) for r in efforts if r["data"].get("time_kind") == kind and r["data"]["source"] != "estimate"]) if any(r["data"].get("time_kind") == kind and r["data"]["source"] != "estimate" for r in efforts) else None for kind in ("active", "wait", "blocked")},
                                 "coverage": "reported intervals only; different time categories may overlap"},
                        "observation_count": len(rows)})
    return {"measurement_schema": 1, "service_version": service_version, "plans": results,
            "cohort": {"total_plans": len(results), **{f"{kind}_plans": sum(r["case_kind"] == kind for r in results) for kind in ("operational", "fixture", "unknown")}},
            "limitations": ["Missing is unknown. Partial sums are lower bounds.", "Client versions and independent verification are attestations, not server detection.",
                            "Compare operational cases with matching scope and versions; observational comparisons do not prove causation.",
                            "Token subsets overlap their totals; person-seconds and elapsed seconds are distinct."]}
