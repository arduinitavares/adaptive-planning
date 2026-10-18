"""Read-only measurements derived from already committed planning evidence."""
from __future__ import annotations

import json

from adaptive_planning import attempts, plans
from adaptive_planning.measurements import timestamp
from adaptive_planning.types import DomainError


def _span(start, end):
    if not start or not end:
        return None
    return max(0, (timestamp(end) - timestamp(start)).total_seconds())


def derive(db, case):
    """Count recorded facts; neither clock spacing nor failed checks imply waste."""
    pid = case["plan_id"]
    counters = dict(db.execute("SELECT kind,count(*) FROM events WHERE plan_id=? GROUP BY kind", (pid,)))
    outcomes = {"passed": 0, "failed": 0, "blocked": 0}
    outcomes.update(dict(db.execute("SELECT outcome,count(*) FROM checkpoints WHERE plan_id=? GROUP BY outcome", (pid,))))
    reassessments = {"material": 0, "unchanged": 0}
    for (raw,) in db.execute("SELECT reassessment FROM checkpoints WHERE plan_id=?", (pid,)):
        value = json.loads(raw) if raw else {}
        disposition = value.get("disposition")
        if disposition in reassessments:
            reassessments[disposition] += 1
    first, last = db.execute("SELECT min(published_at),max(published_at) FROM events WHERE plan_id=?", (pid,)).fetchone()
    trace = []
    for seq, kind, at, raw in db.execute(
            "SELECT sequence,kind,published_at,payload FROM events WHERE plan_id=? ORDER BY sequence DESC LIMIT 40", (pid,)):
        value = json.loads(raw)
        cp = value.get("checkpoint", value)
        reassessment = cp.get("reassessment") or {}
        trace.append({"sequence": seq, "kind": kind, "at": at, "step_id": value.get("step_id"),
                      "outcome": cp.get("outcome"), "disposition": reassessment.get("disposition")})
    closed = []
    count = 0
    for start, stop, raw in db.execute("SELECT started_at,stopped_at,payload FROM attempts WHERE plan_id=?", (pid,)):
        count += 1
        data = json.loads(raw)
        version = data.get("result_version")
        accepted = db.execute("SELECT created_at FROM result_versions WHERE plan_id=? AND result_version=?", (pid, version)).fetchone() if version else None
        seconds = _span(start, accepted[0] if accepted else stop)
        if seconds is not None:
            closed.append(seconds)
    accepted_steps = [s for s in case["steps"] if s["state"] == "complete"]
    linked, unresolved = 0, 0
    plan = plans._load_plan(db, pid) if accepted_steps else None
    for step in accepted_steps:
        if not step.get("result_version"):
            unresolved += 1
            continue
        try:
            # Reuse domain proof validation, including checkpoint/artifact/event pointers.
            _, producer, _, reviews = attempts._accepted_history(db, plan, step["step_id"], step["result_version"])
            if producer != step.get("attempt_id"):
                unresolved += 1
            else:
                raw = db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?", (pid, producer)).fetchone()[0]
                proof = json.loads(raw)
                selected, sequence = proof.get("review_id"), proof.get("review_sequence")
                event = db.execute("SELECT kind,payload FROM events WHERE plan_id=? AND sequence=?", (pid, sequence)).fetchone()
                review = json.loads(event[1]) if event else {}
                cycle = review.get("cycle") or {}
                if (selected in reviews and event and event[0] == "record_review" and review.get("review_id") == selected
                        and review.get("verdict") == "passed" and cycle.get("kind") == "submission"
                        and cycle.get("attempt_id") == producer and cycle.get("step_id") == step["step_id"]
                        and cycle.get("checkpoint_sequence") == proof.get("checkpoint_sequence")):
                    linked += 1
                elif selected is not None or sequence is not None:
                    unresolved += 1
        except DomainError:
            unresolved += 1
    elapsed = [_span(case["created_at"], s.get("accepted_at")) for s in accepted_steps]
    return {"source": "canonical_records", "total_steps": len(case["steps"]), "accepted_steps": len(accepted_steps),
            "accepted_with_linked_review": linked, "unresolved_review_bindings": unresolved,
            "review_basis": "Exact accepted artifact and checkpoint with a distinct recorded reviewer; model family and team independence are not established by these records.",
            "checkpoints": outcomes, "reassessments": reassessments, "revisions": counters.get("revise_plan", 0),
            "invalidations": counters.get("invalidate_result", 0), "attempts": count,
            "active_holds": db.execute("SELECT count(*) FROM holds WHERE plan_id=? AND active=1", (pid,)).fetchone()[0],
            "closed_attempt_elapsed_seconds": closed, "acceptance_elapsed_seconds": [s for s in elapsed if s is not None],
            "time_basis": "Calendar elapsed spans, including pauses; not active work or person time.",
            "event_count": sum(counters.values()), "trace": trace, "trace_limit": 40,
            "first_event_at": first, "last_event_at": last}
