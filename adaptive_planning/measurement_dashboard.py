"""Stdlib-only presentation of the read-only measurement report.

Canonical lifecycle values and receipt-covered quantities remain separate. This
module does not classify plans, infer telemetry, or modify the report.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
import json
import math
from statistics import median
from urllib.parse import quote, urlencode


COHORTS = {"operational": "Operational", "fixture": "Fixtures", "unknown": "Unclassified", "all": "All plans"}
TRACE_LIMIT = 20


def _e(value) -> str:
    return escape(str(value), quote=True)


def _get(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _number(value) -> str:
    if value is None:
        return "Unknown"
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def _duration(value) -> str:
    if value is None:
        return "Unknown"
    if value < 60:
        return f"{_number(value)} sec"
    if value < 3600:
        return f"{_number(round(value / 60, 1))} min"
    if value < 86400:
        return f"{_number(round(value / 3600, 1))} hr"
    return f"{_number(round(value / 86400, 1))} days"


def _sum(plans, *path):
    values = [_get(plan, *path) for plan in plans]
    return sum(values) if values and all(value is not None for value in values) else None


def _quantity(plans, group, name):
    values = [_get(plan, group, name) or {} for plan in plans]
    known = [value["known_sum"] for value in values if value.get("known_sum") is not None]
    complete = bool(values) and all(value.get("coverage") == "client_attested_complete" for value in values)
    label = ("Client-attested complete" if complete else
             f"Lower bound · receipts in {len(known)} of {len(plans)} plans" if known else "No receipts")
    return sum(known) if known else None, label


def _defects(plans, name):
    values = [_get(plan, "regressions", name) for plan in plans]
    known = [value for value in values if value is not None]
    complete = bool(plans) and all(_get(plan, "regressions", "coverage") == "client_attested_complete" for plan in plans)
    note = ("Client-attested complete" if complete else
            f"Lower bound · {len(known)} of {len(plans)} plans with defect evidence" if known else "No defect receipts")
    return sum(known) if known else None, note


def _spans(plans, name):
    return [value for plan in plans for value in (_get(plan, "lifecycle", name) or [])
            if type(value) in (float, int) and math.isfinite(value) and value >= 0]


def _metric(label, value, note="", tone=""):
    return (f'<div class="metric {tone}"><dt>{_e(label)}</dt><dd><span>{_e(value)}</span>'
            + (f'<small>{_e(note)}</small>' if note else "") + '</dd></div>')


def _group(name, description, content, note):
    return (f'<section class="kpi-group" aria-label="{_e(name)}"><div class="group-heading">'
            f'<h3>{_e(name)}</h3><p>{_e(description)}</p></div><div class="group-body">'
            f'<dl class="metrics">{content}</dl><p class="group-note">{_e(note)}</p></div></section>')


def _time(value):
    if not value:
        return '<span class="muted">Unknown time</span>'
    try:
        label = datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M %Z")
    except (ValueError, AttributeError):
        label = str(value)
    return f'<time datetime="{_e(value)}">{_e(label)}</time>'


def _raw(value):
    return '<pre>' + _e(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'


def _target(path, query):
    return path + ("?" + urlencode(query) if query else "")


def _coverage(plans):
    count = len(plans)
    canonical = sum(_get(plan, "lifecycle", "source") == "canonical_records" for plan in plans)
    token = sum(any(_get(plan, "resources", key, "known_sum") is not None
                    for key in ("input_tokens", "output_tokens")) for plan in plans)
    person = sum(any(_get(plan, "resources", key, "known_sum") is not None
                     for key in ("worker_seconds", "human_seconds", "reviewer_seconds")) for plan in plans)
    version = sum(any(any(row.get(key) for key in ("workflow_version", "skill_version", "model_id"))
                      for row in plan.get("versions", [])) for plan in plans)
    rows = (("Canonical lifecycle", canonical, "Automatic from persisted records"),
            ("Token receipts", token, f"{token} of {count} plans with token receipts"),
            ("Person time", person, "Measured receipts only"),
            ("Version context", version, "Client-reported workflow, skill or model"))
    content = "".join(f'<li><div><span>{label}</span><small>{_e(note)}</small></div>'
                      f'<strong>{value}<span> / {count}</span></strong></li>' for label, value, note in rows)
    return (f'<aside class="coverage" aria-labelledby="coverage-title"><h2 id="coverage-title">Evidence coverage</h2>'
            f'<p>Plans with evidence in this view</p><ul>{content}</ul>'
            '<p class="coverage-note">A receipt can cover part of a plan. Missing data remains unknown.</p></aside>')


def _overview(plans, cohort):
    accepted = _sum(plans, "lifecycle", "accepted_steps")
    total = _sum(plans, "lifecycle", "total_steps")
    reviewed = _sum(plans, "lifecycle", "accepted_with_linked_review")
    progress = ""
    if not plans:
        name = "operational" if cohort == "operational" else COHORTS[cohort].lower()
        headline = f"No {name} plans in this report"
        caption = "Classification requires an explicit context receipt. Select another cohort to inspect its evidence."
    elif accepted is None or total is None:
        headline = "Acceptance is unknown"
        caption = "Canonical lifecycle evidence is not available for every plan in this view."
    else:
        headline = f"{_number(accepted)} of {_number(total)} steps accepted"
        caption = f"{_number(total - accepted)} steps remain unaccepted. Acceptance describes the recorded plan state."
        if total:
            progress = (f'<progress max="{total}" value="{accepted}" aria-label="Accepted steps">'
                        f'{accepted} of {total}</progress><p class="progress-caption">'
                        f'{accepted / total:.0%} accepted <span>{COHORTS[cohort]} cohort</span></p>')
    review_text = (f"{_number(reviewed)} of {_number(accepted)} accepted steps have a linked review"
                   if reviewed is not None and accepted is not None else "Linked review coverage is unknown")
    unresolved = _sum(plans, "lifecycle", "unresolved_review_bindings")
    unresolved_note = (f'<p>{_number(unresolved)} accepted results have unresolved review bindings.</p>'
                       if unresolved else "")
    return (f'<section id="overview" class="overview" aria-labelledby="overview-title"><div class="acceptance">'
            '<h2 id="overview-title">Acceptance progress</h2>'
            f'<p class="acceptance-value">{_e(headline)}</p><p class="overview-description">{_e(caption)}</p>{progress}'
            f'<div class="review-evidence"><strong>{_e(review_text)}</strong>'
            '<p>Exact accepted result and recorded review binding. Cross-family independence is unknown unless separately attested.</p>'
            + unresolved_note + '</div></div>' + _coverage(plans) + '</section>')


def _money(plans):
    buckets = {}
    for plan in plans:
        for key, value in (plan.get("money") or {}).items():
            if value.get("known_sum") is not None:
                buckets[key] = buckets.get(key, 0) + value["known_sum"]
    if not buckets:
        return '<p class="no-billing">No billing receipts. Cost is unknown.</p>'
    rows = []
    for key, value in sorted(buckets.items()):
        field, currency, scope = key.split(":", 2)
        label = "Billed" if field == "billing_amount" else "Estimate"
        rows.append(f'<li><span>{label} · {_e(currency)} · {_e(scope)}</span>'
                    f'<strong>{_e(_number(value))} {_e(currency)}</strong><small>Partial receipts</small></li>')
    return '<ul class="billing">' + "".join(rows) + '</ul>'


def _volume(plans):
    checkpoint_values = [_sum(plans, "lifecycle", "checkpoints", outcome) for outcome in ("passed", "failed", "blocked")]
    checkpoints = sum(checkpoint_values) if all(value is not None for value in checkpoint_values) else None
    material = _sum(plans, "lifecycle", "reassessments", "material")
    unchanged = _sum(plans, "lifecycle", "reassessments", "unchanged")
    entries = ((_sum(plans, "lifecycle", "attempts"), "attempts"), (checkpoints, "checkpoints"),
               (_sum(plans, "lifecycle", "event_count"), "events"),
               (_sum(plans, "lifecycle", "revisions"), "revisions"),
               (_sum(plans, "lifecycle", "active_holds"), "active holds"))
    values = "".join(f'<li>{_number(value)} {_e(label)}</li>' for value, label in entries)
    return ('<section class="canonical-volume" aria-label="Canonical activity volume"><h2>Canonical activity volume</h2>'
            '<p>Automatic counts across all recorded history in this view.</p>'
            f'<ul>{values}</ul><p>{_number(material)} material and {_number(unchanged)} unchanged reassessments. '
            'Counts describe activity, not efficiency.</p></section>')


def _kpis(plans):
    accepted = _sum(plans, "lifecycle", "accepted_steps")
    total = _sum(plans, "lifecycle", "total_steps")
    reviewed = _sum(plans, "lifecycle", "accepted_with_linked_review")
    outcomes = (_metric("Accepted steps", f"{_number(accepted)} / {_number(total)}", "Canonical plan state", "positive")
                + _metric("With a linked review", f"{_number(reviewed)} / {_number(accepted)}", "Denominator: accepted steps")
                + _metric("Client verification attestations", _number(_sum(plans, "outcomes", "verified")),
                          "Separate receipt evidence"))
    groups = [_group("Outcomes", "What reached acceptance?", outcomes,
                     "A distinct recorded reviewer does not establish model-family independence or production correctness.")]
    caught, caught_note = _defects(plans, "caught")
    escaped, escaped_note = _defects(plans, "escaped")
    regressions = (_metric("Result invalidations", _number(_sum(plans, "lifecycle", "invalidations")), "Canonical withdrawn results")
                   + _metric("Caught defects", _number(caught), caught_note)
                   + _metric("Escaped defects", _number(escaped), escaped_note))
    failed = _sum(plans, "lifecycle", "checkpoints", "failed")
    blocked = _sum(plans, "lifecycle", "checkpoints", "blocked")
    groups.append(_group("Regressions", "What evidence identifies a defect?", regressions,
                         f"{_number(failed)} failed and {_number(blocked)} blocked checkpoints. These do not automatically count as defects or wasted work."))
    rework = []
    has_rework = False
    for key, label in (("worker_seconds", "Worker person time"), ("human_seconds", "Human person time"),
                       ("reviewer_seconds", "Reviewer person time")):
        value, coverage = _quantity(plans, "rework", key)
        has_rework |= value is not None
        rework.append(_metric(label, _duration(value), coverage))
    groups.append(_group("Corrective rework", "Effort linked to a recorded defect", "".join(rework),
                         "Only explicitly attributed corrective effort is included. Person time differs from calendar time."
                         if has_rework else "No measured rework effort. Missing receipts do not establish zero rework."))
    closed = _spans(plans, "closed_attempt_elapsed_seconds")
    acceptance = _spans(plans, "acceptance_elapsed_seconds")
    attempts = _sum(plans, "lifecycle", "attempts")
    unclosed = max(0, attempts - len(closed)) if attempts is not None else None
    worker, worker_coverage = _quantity(plans, "resources", "worker_seconds")
    time = (_metric("Median closed attempt", _duration(median(closed) if closed else None), f"{len(closed)} closed attempt spans")
            + _metric("Median creation to acceptance", _duration(median(acceptance) if acceptance else None),
                      f"{len(acceptance)} acceptance spans")
            + _metric("Measured worker person time", _duration(worker), worker_coverage))
    groups.append(_group("Time", "Calendar spans and measured effort", time,
                         f"{_number(unclosed)} attempts without a closed span. Calendar spans include gaps and waiting. They do not measure active work."))
    resource = []
    for key, label in (("input_tokens", "Input tokens"), ("output_tokens", "Output tokens"), ("tool_calls", "Client tool calls")):
        value, coverage = _quantity(plans, "resources", key)
        resource.append(_metric(label, _number(value), coverage))
    groups.append(_group("Resources", "Observed usage and billing", "".join(resource),
                         "Partial sums are lower bounds. Cached input and reasoning tokens are subsets. Estimates and shared billing stay separate."))
    groups.append(_money(plans))
    return ('<section id="kpis" class="section" aria-labelledby="kpis-title"><div class="section-heading">'
            '<h2 id="kpis-title">The five KPI groups</h2><p>Evidence first. Unknown is a useful result.</p></div>'
            '<div class="kpi-list">' + "".join(groups) + '</div></section>')


def _comparison(plans):
    rows = []
    for plan in plans:
        lifecycle = plan.get("lifecycle") or {}
        spans = lifecycle.get("closed_attempt_elapsed_seconds") or []
        accepted, total = lifecycle.get("accepted_steps"), lifecycle.get("total_steps")
        reviewed = lifecycle.get("accepted_with_linked_review")
        input_tokens, coverage = _quantity([plan], "resources", "input_tokens")
        href = "/plans/" + quote(plan["plan_id"], safe="")
        rows.append(f'<tr><th scope="row"><a href="{_e(href)}">{_e(plan.get("title") or plan["plan_id"])}</a>'
                    f'<small>{_e(plan.get("status", "unknown"))} · {_e(COHORTS.get(plan.get("case_kind"), "Unclassified"))}</small></th>'
                    f'<td data-label="Accepted"><strong>{_number(accepted)} / {_number(total)}</strong><small>steps</small></td>'
                    f'<td data-label="Linked reviews">{_number(reviewed)}<small>accepted results</small></td>'
                    f'<td data-label="Median attempt">{_duration(median(spans) if spans else None)}<small>{len(spans)} closed spans</small></td>'
                    f'<td data-label="Input tokens">{_number(input_tokens)}<small>{_e(coverage)}</small></td></tr>')
    content = ('<table class="plan-table"><caption class="sr-only">Plan comparison for the selected cohort</caption>'
               '<thead><tr><th scope="col">Plan</th><th scope="col">Accepted</th><th scope="col">Linked reviews</th>'
               '<th scope="col">Median attempt</th><th scope="col">Input tokens</th></tr></thead>'
               '<tbody>' + "".join(rows) + '</tbody></table>') if rows else '<p class="empty">No plans in this cohort.</p>'
    return ('<section id="comparison" class="section" aria-labelledby="comparison-title"><div class="section-heading">'
            '<h2 id="comparison-title">Plan comparison</h2><p>Current recorded state. Different scope can explain different results.</p></div>'
            + content + '</section>')


def _trace(plans):
    events = [(event, plan) for plan in plans for event in (_get(plan, "lifecycle", "trace") or [])]
    events.sort(key=lambda pair: pair[0].get("sequence", 0), reverse=True)
    events = events[:TRACE_LIMIT]
    rows = []
    for event, plan in events:
        outcome = event.get("outcome") or event.get("disposition")
        tone = "warning" if outcome in ("failed", "blocked", "rejected") else "neutral"
        title = str(event.get("kind", "unknown")).replace("_", " ")
        rows.append(f'<li><div class="event-heading"><strong>{_e(title)}</strong>'
                    + (f'<span class="badge {tone}">{_e(outcome)}</span>' if outcome else "")
                    + f'</div><p>{_e(plan.get("title") or plan["plan_id"])}'
                    + (f' · {_e(event["step_id"])}' if event.get("step_id") else "")
                    + f'</p><div class="event-meta">{_time(event.get("at"))}<span>Event #{_e(event.get("sequence", "unknown"))}</span></div></li>')
    content = '<ol class="activity">' + "".join(rows) + '</ol>' if rows else '<p class="empty">No canonical activity is available for this view.</p>'
    return ('<section id="activity" class="activity-section" aria-labelledby="activity-title"><h2 id="activity-title">Recorded activity</h2>'
            f'<p class="muted">Latest {len(events)} canonical events · at most {TRACE_LIMIT} shown</p>'
            + content + '<p class="footnote">Actual persisted activity. Event volume does not show improvement.</p></section>')


def _versions(plans, data):
    rows = []
    missing = 0
    for plan in plans:
        versions = plan.get("versions") or []
        latest = {}
        for row in versions:
            for key in ("workflow_version", "skill_version", "model_id", "provider", "effort"):
                if row.get(key):
                    latest[key] = row[key]
        if not latest:
            missing += 1
        values = "".join(f'<dt>{label}</dt><dd>{_e(latest.get(key, "Unknown"))}</dd>'
                         for key, label in (("workflow_version", "Workflow"), ("skill_version", "Skill"),
                                            ("model_id", "Model"), ("provider", "Provider"), ("effort", "Effort")))
        rows.append(f'<details class="version-plan"><summary>{_e(plan.get("title") or plan["plan_id"])}</summary>'
                    f'<dl class="version-values">{values}</dl><details><summary>All reported contexts</summary>'
                    + _raw(versions) + '</details></details>')
    version = data.get("service_version") or {}
    return ('<section id="versions" class="version-section" aria-labelledby="versions-title"><h2 id="versions-title">Version evidence</h2>'
            '<p class="muted">Latest reported values per field. A plan can use multiple versions.</p>'
            + (f'<p class="evidence-notice">{missing} of {len(plans)} plans lack version context. Version comparisons have incomplete coverage.</p>'
               if missing else '<p class="evidence-notice">Version context is descriptive. It does not establish which version caused an outcome.</p>')
            + "".join(rows) + '<details class="service-version"><summary>Service fingerprint</summary>'
            + _raw(version) + '</details></section>')


def _details(data, query, cohort, plans):
    api_query = {key: value for key, value in query.items() if key in ("plan_id", "after", "limit")}
    next_link = ""
    if data.get("next_after") is not None:
        next_query = {**api_query, "after": str(data["next_after"]), "cohort": cohort}
        next_link = f'<a href="{_e(_target("/measurements", next_query))}">Next receipt page</a>'
    calls = data.get("mcp_calls") or {}
    intervals = [{"plan_id": plan["plan_id"], "measured_wall_seconds": _get(plan, "time", "measured_wall_seconds"),
                  "person_time": {key: _get(plan, "resources", key) for key in ("worker_seconds", "human_seconds", "reviewer_seconds")},
                  "estimated_resources": plan.get("estimated_resources")} for plan in plans]
    return ('<section id="evidence" class="section evidence-section" aria-labelledby="evidence-title">'
            '<div class="section-heading"><h2 id="evidence-title">Source details</h2>'
            f'<a class="text-link" href="{_e(_target("/api/measurements", api_query))}">Open full JSON report</a></div>'
            '<details><summary>Measured intervals, person time and estimates</summary>'
            '<p>Reported interval categories can overlap. Person time is distinct from calendar time.</p>' + _raw(intervals) + '</details>'
            f'<details><summary>Receipt trace · {len(data.get("observations", []))} on this page</summary>'
            '<p>Receipt pagination does not change the all-history KPI totals.</p>'
            + _raw(data.get("observations", [])) + next_link + '</details>'
            f'<details><summary>Global server MCP calls · {_number(calls.get("count"))}</summary>'
            '<p>Global across this server. Calls are not attributed to the selected cohort or plan.</p>' + _raw(calls) + '</details>'
            '<details><summary>Collection health and interpretation</summary>'
            + _raw({"collection_health": data.get("collection_health"), "limitations": data.get("limitations")}) + '</details>'
            '<details><summary>Raw report · all cohorts in the report scope</summary>' + _raw(data) + '</details></section>')


def render_measurements(data: dict, cohort: str = "operational", query: dict[str, str] | None = None) -> str:
    """Render an existing report. Only an explicit case_kind selects a cohort."""
    if cohort not in COHORTS:
        raise ValueError("Unknown cohort")
    query = query or {}
    all_plans = data.get("plans", [])
    plans = [plan for plan in all_plans if cohort == "all" or plan.get("case_kind", "unknown") == cohort]
    counts = {kind: sum(plan.get("case_kind", "unknown") == kind for plan in all_plans)
              for kind in COHORTS if kind != "all"}
    counts["all"] = len(all_plans)
    cohort_links = []
    for key, label in COHORTS.items():
        values = {name: value for name, value in query.items() if name in ("plan_id", "limit")}
        href = _target("/measurements", {**values, "cohort": key})
        current = ' aria-current="page"' if cohort == key else ""
        cohort_links.append(f'<a href="{_e(href)}" data-cohort="{key}" data-count="{counts[key]}"{current}>'
                            f'{label}<span class="cohort-count">{counts[key]}</span></a>')
    api_query = {key: value for key, value in query.items() if key in ("plan_id", "after", "limit")}
    scope = 'Selected plan' if query.get("plan_id") else 'All plans'
    scope_link = '<a href="/measurements">Clear plan selection</a>' if query.get("plan_id") else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Measurements | Adaptive planning</title><link rel="stylesheet" href="/measurements.css">'
            '</head><body><a class="skip-link" href="#main">Skip to measurements</a>'
            '<header class="site-header"><div class="header-inner"><a class="brand" href="/">Adaptive planning</a>'
            '<nav aria-label="Main navigation"><a href="/">Plans</a><a href="/measurements" aria-current="page">Measurements</a></nav>'
            '</div></header><main id="main" class="workspace"><div class="page-heading"><div><h1>Planning measurements</h1>'
            '<p>A clear view of progress, effort and the evidence behind them.</p></div>'
            f'<a class="export-link" href="{_e(_target("/api/measurements", api_query))}">JSON report</a></div>'
            '<div class="scope-bar"><p><strong>All recorded history</strong><span>No date filter applied</span></p>'
            f'<p>{scope} · {len(all_plans)} in report {scope_link}</p>'
            f'<p>Canonical snapshot <strong>#{_e(data.get("canonical_store_sequence", "unknown"))}</strong></p></div>'
            '<nav class="cohorts" aria-label="Measurement cohorts">' + "".join(cohort_links) + '</nav>'
            + _overview(plans, cohort) + _volume(plans) + _kpis(plans) + _comparison(plans)
            + '<div class="evidence-columns">' + _trace(plans) + _versions(plans, data) + '</div>'
            + _details(data, query, cohort, plans)
            + '<footer>Read-only measurements · Missing means unknown · Partial sums are lower bounds</footer>'
            '</main></body></html>')
