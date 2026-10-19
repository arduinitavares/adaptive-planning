"""Stdlib-only presentation of the read-only measurement report.

Canonical lifecycle values and receipt-covered quantities remain separate. This
module does not classify plans, infer telemetry, or modify the report.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
import json
import math
import re
from statistics import median
from urllib.parse import quote, urlencode


COHORTS = {"operational": "Operational", "fixture": "Fixtures", "unknown": "Unclassified", "all": "All plans"}
TRACE_LIMIT = 20

_CAVEMAN_STOP = {
    "the", "a", "an", "into", "with", "for", "from", "by", "to", "and", "of", "in",
    "on", "at", "is", "are", "be", "been", "this", "that", "its", "their", "current",
    "future", "optional", "authorized", "remaining", "work", "system", "initial",
}
_CAVEMAN_SUBS = (
    (r"\bwithout\s+(\w+)ing\b", r"no-\1"),
    (r"\bwithout\b", "no"),
    (r"\barchitecture\b", "arch"),
    (r"\bconfiguration\b", "config"),
    (r"\bcomputer vision\b", "CV"),
    (r"\bcalibration\b", "calib"),
    (r"\bcontinuation\b", "cont"),
    (r"\bpersistence\b", "persist"),
    (r"\bacceptance\b", "accept"),
    (r"\bsynchronization\b", "sync"),
    (r"\binspection\b", "inspect"),
    (r"\bcontainer-specific\b", "container"),
    (r"\bzero-downtime\b", "0-downtime"),
)


def _caveman_ultra(text: str, max_words: int = 7) -> str:
    cleaned = re.sub(r"\s*\([^)]*\)", "", str(text).strip()).rstrip(".")
    for pat, rep in _CAVEMAN_SUBS:
        cleaned = re.sub(pat, rep, cleaned, flags=re.IGNORECASE)
    words = [w.strip(",;:") for w in cleaned.split()]
    kept: list[str] = []
    seen: set[str] = set()
    for word in words:
        lw = word.lower()
        if not word or lw in _CAVEMAN_STOP:
            continue
        if lw in seen and len(lw) > 3:
            continue
        seen.add(lw)
        kept.append(word)
    if not kept:
        kept = words
    result = " ".join(kept[:max_words])
    return result + "…" if len(kept) > max_words else result


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


def _svg_bar(ratio: float | None, tone: str = "teal", width: int = 200, height: int = 8) -> str:
    r = 0.0 if ratio is None or not math.isfinite(ratio) else max(0.0, min(1.0, float(ratio)))
    fill_w = max(4.0, round(width * r, 1)) if r > 0 else 0.0
    rx = height // 2
    fill_rect = (f'<rect class="bar-fill bar-fill--{_e(tone)}" x="0" y="0" '
                 f'width="{fill_w:.1f}" height="{height}" rx="{rx}"></rect>' if fill_w > 0 else "")
    return (f'<svg class="chart-bar" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="bar-track" x="0" y="0" width="{width}" height="{height}" rx="{rx}"></rect>'
            f'{fill_rect}</svg>')


def _svg_donut(accepted: int | float | None, total: int | float | None, reviewed: int | float | None) -> str:
    acc_ratio = (float(accepted) / float(total)) if (accepted is not None and total) else 0.0
    rev_ratio = (float(reviewed) / float(accepted)) if (reviewed is not None and accepted) else 0.0
    acc_ratio = max(0.0, min(1.0, acc_ratio))
    rev_ratio = max(0.0, min(1.0, rev_ratio))
    c_outer = 2.0 * math.pi * 40.0
    c_inner = 2.0 * math.pi * 28.0
    dash_outer = f"{c_outer * acc_ratio:.1f} {c_outer:.1f}"
    dash_inner = f"{c_inner * rev_ratio:.1f} {c_inner:.1f}"
    pct_label = f"{acc_ratio:.0%}" if (accepted is not None and total) else "—"
    return (
        '<svg class="donut-svg" viewBox="0 0 100 100" width="96" height="96" aria-hidden="true">'
        '<circle class="donut-track" cx="50" cy="50" r="40"></circle>'
        f'<circle class="donut-ring donut-ring--outer" cx="50" cy="50" r="40" stroke-dasharray="{dash_outer}"></circle>'
        '<circle class="donut-track donut-track--inner" cx="50" cy="50" r="28"></circle>'
        f'<circle class="donut-ring donut-ring--inner" cx="50" cy="50" r="28" stroke-dasharray="{dash_inner}"></circle>'
        f'<text class="donut-text" x="50" y="54" text-anchor="middle">{_e(pct_label)}</text>'
        '</svg>'
    )


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
    is_unknown = str(value).startswith("Unknown")
    classes = " ".join(part for part in ("metric", tone, "unknown" if is_unknown else "") if part)
    return (f'<div class="{classes}"><dt>{_e(label)}</dt><dd><span>{_e(value)}</span>'
            + (f'<small>{_e(note)}</small>' if note else "") + '</dd></div>')


def _group(name, description, content, note, bar_ratio: float | None = None, bar_tone: str = "teal"):
    bar_html = _svg_bar(bar_ratio, bar_tone, 240, 6) if bar_ratio is not None else ""
    return (f'<section class="kpi-group" aria-label="{_e(name)}"><div class="group-heading">'
            f'<div class="group-title-row"><h3>{_e(name)}</h3></div>{bar_html}</div><div class="group-body">'
            f'<dl class="metrics">{content}</dl>'
            f'<details class="group-note-details"><summary>Rich description</summary>'
            f'<p class="group-desc">{_e(description)}</p>'
            f'<p class="group-note">{_e(note)}</p></details></div></section>')


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
    items = []
    notes = []
    for label, value, note in rows:
        ratio = (value / count) if count else 0.0
        tone = "teal" if value > 0 else "muted"
        pct = f"{ratio:.0%}" if count else "0%"
        items.append(
            f'<li data-covered="{"true" if value > 0 else "false"}">'
            f'<div class="cov-row-top"><span>{label}</span><strong>{value}<span> / {count} ({pct})</span></strong></div>'
            + _svg_bar(ratio, tone, 240, 7) + '</li>'
        )
        notes.append(f"<li><strong>{label}:</strong> {_e(note)}</li>")
    return (f'<aside class="coverage" aria-labelledby="coverage-title">'
            '<div class="card-head-row"><h2 id="coverage-title">Evidence coverage</h2>'
            f'<span class="caveman-chip">{count} plans</span></div>'
            f'<ul>{"".join(items)}</ul>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p>Plans with evidence in this view</p>'
            f'<ul class="cov-notes-list">{"".join(notes)}</ul>'
            '<p class="coverage-note">A receipt can cover part of a plan. Missing data remains unknown.</p>'
            '</details></aside>')


def _overview(plans, cohort):
    accepted = _sum(plans, "lifecycle", "accepted_steps")
    total = _sum(plans, "lifecycle", "total_steps")
    reviewed = _sum(plans, "lifecycle", "accepted_with_linked_review")
    progress = ""
    if not plans:
        name = "operational" if cohort == "operational" else COHORTS[cohort].lower()
        headline = f"No {name} plans in this report"
        caption = "Classification requires an explicit context receipt. Select another cohort to inspect its evidence."
        caveman_stat = f"0 {name} plans"
    elif accepted is None or total is None:
        headline = "Acceptance is unknown"
        caption = "Canonical lifecycle evidence is not available for every plan in this view."
        caveman_stat = "Acceptance unknown"
    else:
        headline = f"{_number(accepted)} of {_number(total)} steps accepted"
        caption = f"{_number(total - accepted)} steps remain unaccepted. Acceptance describes the recorded plan state."
        caveman_stat = f"{_number(accepted)} / {_number(total)} steps accepted"
        if total:
            progress = (f'<progress max="{total}" value="{accepted}" aria-label="Accepted steps">'
                        f'{accepted} of {total}</progress><p class="progress-caption">'
                        f'{accepted / total:.0%} accepted <span>{COHORTS[cohort]} cohort</span></p>')
    review_text = (f"{_number(reviewed)} of {_number(accepted)} accepted steps have a linked review"
                   if reviewed is not None and accepted is not None else "Linked review coverage is unknown")
    rev_short = (f"{_number(reviewed)} / {_number(accepted)} reviewed"
                 if reviewed is not None and accepted is not None else "Review unknown")
    rem_short = (f"{_number(total - accepted)} remaining"
                 if accepted is not None and total is not None else "—")
    unresolved = _sum(plans, "lifecycle", "unresolved_review_bindings")
    unresolved_note = (f'<p>{_number(unresolved)} accepted results have unresolved review bindings.</p>'
                       if unresolved else "")
    donut = _svg_donut(accepted, total, reviewed)
    return (f'<section id="overview" class="overview" aria-labelledby="overview-title"><div class="acceptance">'
            '<div class="card-head-row"><h2 id="overview-title">Acceptance progress</h2>'
            f'<span class="caveman-chip">{_e(COHORTS[cohort])}</span></div>'
            '<div class="donut-layout">'
            + donut
            + '<div class="donut-stats">'
            f'<p class="acceptance-value">{_e(headline)}</p>'
            f'<div class="stat-pills">'
            f'<span class="stat-pill stat-pill--teal">● {_e(caveman_stat)}</span>'
            f'<span class="stat-pill stat-pill--blue">● {_e(rev_short)}</span>'
            f'<span class="stat-pill stat-pill--neutral">○ {_e(rem_short)}</span>'
            '</div>'
            + progress + '</div></div>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            f'<p class="overview-description">{_e(caption)}</p>'
            f'<div class="review-evidence"><strong>{_e(review_text)}</strong>'
            '<p>Exact accepted result and recorded review binding. Cross-family independence is unknown unless separately attested.</p>'
            + unresolved_note + '</div></details></div>' + _coverage(plans) + '</section>')


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
    passed = _sum(plans, "lifecycle", "checkpoints", "passed")
    failed = _sum(plans, "lifecycle", "checkpoints", "failed")
    blocked = _sum(plans, "lifecycle", "checkpoints", "blocked")
    checkpoint_values = [passed, failed, blocked]
    checkpoints = sum(checkpoint_values) if all(value is not None for value in checkpoint_values) else None
    material = _sum(plans, "lifecycle", "reassessments", "material")
    unchanged = _sum(plans, "lifecycle", "reassessments", "unchanged")
    attempts = _sum(plans, "lifecycle", "attempts")
    events = _sum(plans, "lifecycle", "event_count")
    revisions = _sum(plans, "lifecycle", "revisions")
    holds = _sum(plans, "lifecycle", "active_holds")
    entries = ((attempts, "attempts", "teal"), (checkpoints, "checkpoints", "green"),
               (events, "events", "blue"), (revisions, "revisions", "amber"),
               (holds, "active holds", "red"))
    numeric_vals = [v for v, _, _ in entries if isinstance(v, (int, float)) and v > 0]
    max_val = max(numeric_vals) if numeric_vals else 1.0
    items = []
    for value, label, tone in entries:
        ratio = (float(value) / float(max_val)) if isinstance(value, (int, float)) and max_val else 0.0
        items.append(
            f'<li><div class="vol-item-top"><strong>{_number(value)} {_e(label)}</strong></div>'
            + _svg_bar(ratio, tone, 180, 6) + '</li>'
        )
    return ('<section class="canonical-volume" aria-label="Canonical activity volume">'
            '<div class="volume-header"><h2>Canonical activity volume</h2>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p>Automatic counts across all recorded history in this view.</p>'
            f'<p class="volume-footnote">{_number(material)} material and {_number(unchanged)} unchanged reassessments. '
            'Counts describe activity, not efficiency.</p></details></div>'
            f'<ul>{"".join(items)}</ul></section>')


def _kpis(plans):
    accepted = _sum(plans, "lifecycle", "accepted_steps")
    total = _sum(plans, "lifecycle", "total_steps")
    reviewed = _sum(plans, "lifecycle", "accepted_with_linked_review")
    acc_ratio = (float(accepted) / float(total)) if (accepted is not None and total) else 0.0
    outcomes = (_metric("Accepted steps", f"{_number(accepted)} / {_number(total)}", "Canonical plan state", "positive")
                + _metric("With a linked review", f"{_number(reviewed)} / {_number(accepted)}", "Denominator: accepted steps")
                + _metric("Client verification attestations", _number(_sum(plans, "outcomes", "verified")),
                          "Separate receipt evidence"))
    groups = [_group("Outcomes", "What reached acceptance?", outcomes,
                     "A distinct recorded reviewer does not establish model-family independence or production correctness.",
                     acc_ratio, "teal")]
    caught, caught_note = _defects(plans, "caught")
    escaped, escaped_note = _defects(plans, "escaped")
    inv = _sum(plans, "lifecycle", "invalidations")
    regressions = (_metric("Result invalidations", _number(inv), "Canonical withdrawn results")
                   + _metric("Caught defects", _number(caught), caught_note)
                   + _metric("Escaped defects", _number(escaped), escaped_note))
    failed = _sum(plans, "lifecycle", "checkpoints", "failed")
    blocked = _sum(plans, "lifecycle", "checkpoints", "blocked")
    passed = _sum(plans, "lifecycle", "checkpoints", "passed")
    chk_total = (passed or 0) + (failed or 0) + (blocked or 0)
    pass_ratio = (float(passed or 0) / float(chk_total)) if chk_total else 0.0
    groups.append(_group("Regressions", "What evidence identifies a defect?", regressions,
                         f"{_number(failed)} failed and {_number(blocked)} blocked checkpoints. These do not automatically count as defects or wasted work.",
                         pass_ratio, "green"))
    rework = []
    has_rework = False
    for key, label in (("worker_seconds", "Worker person time"), ("human_seconds", "Human person time"),
                       ("reviewer_seconds", "Reviewer person time")):
        value, coverage = _quantity(plans, "rework", key)
        has_rework |= value is not None
        rework.append(_metric(label, _duration(value), coverage))
    groups.append(_group("Corrective rework", "Effort linked to a recorded defect", "".join(rework),
                         "Only explicitly attributed corrective effort is included. Person time differs from calendar time."
                         if has_rework else "No measured rework effort. Missing receipts do not establish zero rework.",
                         1.0 if has_rework else 0.0, "muted"))
    closed = _spans(plans, "closed_attempt_elapsed_seconds")
    acceptance = _spans(plans, "acceptance_elapsed_seconds")
    attempts = _sum(plans, "lifecycle", "attempts")
    unclosed = max(0, attempts - len(closed)) if attempts is not None else None
    closed_ratio = (len(closed) / float(attempts)) if (attempts and attempts > 0) else 0.0
    worker, worker_coverage = _quantity(plans, "resources", "worker_seconds")
    time = (_metric("Median closed attempt", _duration(median(closed) if closed else None), f"{len(closed)} closed attempt spans")
            + _metric("Median creation to acceptance", _duration(median(acceptance) if acceptance else None),
                      f"{len(acceptance)} acceptance spans")
            + _metric("Measured worker person time", _duration(worker), worker_coverage))
    groups.append(_group("Time", "Calendar spans and measured effort", time,
                         f"{_number(unclosed)} attempts without a closed span. Calendar spans include gaps and waiting. They do not measure active work.",
                         closed_ratio, "blue"))
    resource = []
    has_res = False
    for key, label in (("input_tokens", "Input tokens"), ("output_tokens", "Output tokens"), ("tool_calls", "Client tool calls")):
        value, coverage = _quantity(plans, "resources", key)
        has_res |= value is not None
        resource.append(_metric(label, _number(value), coverage))
    groups.append(_group("Resources", "Observed usage and billing", "".join(resource),
                         "Partial sums are lower bounds. Cached input and reasoning tokens are subsets. Estimates and shared billing stay separate.",
                         1.0 if has_res else 0.0, "muted"))
    groups.append(_money(plans))
    return ('<section id="kpis" class="section" aria-labelledby="kpis-title"><div class="section-heading">'
            '<h2 id="kpis-title">The five KPI groups</h2>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p>Evidence first. Unknown is a useful result.</p></details></div>'
            '<div class="kpi-list">' + "".join(groups) + '</div></section>')


def _comparison(plans):
    rows = []
    medians = []
    for plan in plans:
        spans = (_get(plan, "lifecycle", "closed_attempt_elapsed_seconds") or [])
        medians.append(median(spans) if spans else 0.0)
    max_med = max(medians) if medians and max(medians) > 0 else 1.0

    for plan, med_val in zip(plans, medians):
        lifecycle = plan.get("lifecycle") or {}
        spans = lifecycle.get("closed_attempt_elapsed_seconds") or []
        accepted, total = lifecycle.get("accepted_steps"), lifecycle.get("total_steps")
        reviewed = lifecycle.get("accepted_with_linked_review")
        input_tokens, coverage = _quantity([plan], "resources", "input_tokens")
        href = "/plans/" + quote(plan["plan_id"], safe="")
        status = plan.get("status", "unknown")
        raw_title = plan.get("title") or plan["plan_id"]
        caveman_title = _caveman_ultra(raw_title, 7)
        tok_str = _number(input_tokens)
        tok_cls = ' class="cell-unknown"' if tok_str == "Unknown" else ""
        med_str = _duration(median(spans) if spans else None)
        med_cls = ' class="cell-unknown"' if med_str == "Unknown" else ""
        acc_ratio = (float(accepted) / float(total)) if (accepted is not None and total) else 0.0
        rev_ratio = (float(reviewed) / float(accepted)) if (reviewed is not None and accepted) else 0.0
        dur_ratio = (float(med_val) / float(max_med)) if (spans and max_med) else 0.0
        rows.append(
            f'<tr><th scope="row">'
            f'<details class="plan-row-details"><summary>'
            f'<small class="plan-status-tag" data-status="{_e(status)}">{_e(status)} · {_e(COHORTS.get(plan.get("case_kind"), "Unclassified"))}</small>'
            f'<span class="caveman-plan-title">{_e(caveman_title)}</span>'
            f'</summary><div class="plan-rich-box">'
            f'<p>{_e(raw_title)}</p>'
            f'<a class="open-plan-link" href="{_e(href)}">Open plan DAG →</a>'
            f'</div></details></th>'
            f'<td data-label="Accepted"><div class="cell-bar-wrap"><strong>{_number(accepted)} / {_number(total)}</strong><small>steps</small>'
            + _svg_bar(acc_ratio, "teal", 120, 6) + '</div></td>'
            f'<td data-label="Linked reviews"><div class="cell-bar-wrap"><strong>{_number(reviewed)}</strong><small>accepted results</small>'
            + _svg_bar(rev_ratio, "blue", 100, 6) + '</div></td>'
            f'<td data-label="Median attempt"{med_cls}><div class="cell-bar-wrap"><strong>{med_str}</strong><small>{len(spans)} closed spans</small>'
            + (_svg_bar(dur_ratio, "amber", 120, 6) if spans else "") + '</div></td>'
            f'<td data-label="Input tokens"{tok_cls}>{tok_str}<small>{_e(coverage)}</small></td></tr>'
        )
    content = ('<div class="table-card"><table class="plan-table"><caption class="sr-only">Plan comparison for the selected cohort</caption>'
               '<thead><tr><th scope="col">Plan (click row for rich description)</th><th scope="col">Accepted</th><th scope="col">Linked reviews</th>'
               '<th scope="col">Median attempt</th><th scope="col">Input tokens</th></tr></thead>'
               '<tbody>' + "".join(rows) + '</tbody></table></div>') if rows else '<p class="empty">No plans in this cohort.</p>'
    return ('<section id="comparison" class="section" aria-labelledby="comparison-title"><div class="section-heading">'
            '<h2 id="comparison-title">Plan comparison</h2>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p>Current recorded state. Different scope can explain different results.</p></details></div>'
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
        plan_title = plan.get("title") or plan["plan_id"]
        caveman_plan = _caveman_ultra(plan_title, 5)
        step_id = event.get("step_id")
        full_label = f"{plan_title} · {step_id}" if step_id else str(plan_title)
        rows.append(
            f'<li><details class="event-details"><summary>'
            f'<div class="event-heading"><strong>{_e(title)}</strong>'
            + (f'<span class="badge {tone}">{_e(outcome)}</span>' if outcome else "")
            + (f'<code class="step-chip">{_e(step_id)}</code>' if step_id else "")
            + f'<span class="event-caveman-plan">{_e(caveman_plan)}</span></div>'
            + f'<div class="event-meta">{_time(event.get("at"))}<span>Event #{_e(event.get("sequence", "unknown"))}</span></div>'
            + f'</summary><div class="event-rich-body">'
            + f'<p class="event-plan" title="{_e(full_label)}">{_e(plan_title)}'
            + (f' · {_e(step_id)}' if step_id else "")
            + '</p></div></details></li>'
        )
    content = '<ol class="activity">' + "".join(rows) + '</ol>' if rows else '<p class="empty">No canonical activity is available for this view.</p>'
    return ('<section id="activity" class="activity-section" aria-labelledby="activity-title"><div class="column-card-header"><h2 id="activity-title">Recorded activity</h2>'
            f'<p class="muted">Latest {len(events)} canonical events · at most {TRACE_LIMIT} shown</p></div>'
            + content + '<details class="caveman-details"><summary>Rich description</summary>'
            '<p class="footnote">Actual persisted activity. Event volume does not show improvement.</p></details></section>')


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
        raw_title = plan.get("title") or plan["plan_id"]
        caveman_title = _caveman_ultra(raw_title, 6)
        rows.append(f'<details class="version-plan"><summary><span class="caveman-version-title">{_e(caveman_title)}</span></summary>'
                    f'<p class="version-full-title">{_e(raw_title)}</p>'
                    f'<dl class="version-values">{values}</dl><details><summary>All reported contexts</summary>'
                    + _raw(versions) + '</details></details>')
    version = data.get("service_version") or {}
    return ('<section id="versions" class="version-section" aria-labelledby="versions-title">'
            '<div class="column-card-header"><h2 id="versions-title">Version evidence</h2>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p class="muted">Latest reported values per field. A plan can use multiple versions.</p>'
            + (f'<p class="evidence-notice">{missing} of {len(plans)} plans lack version context. Version comparisons have incomplete coverage.</p>'
               if missing else '<p class="evidence-notice">Version context is descriptive. It does not establish which version caused an outcome.</p>')
            + '</details></div>'
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
            '</div></header><main id="main" class="workspace">'
            '<div class="top-command-bar">'
            '<div class="page-heading"><div><h1>Planning measurements</h1>'
            '<details class="caveman-details"><summary>Rich description</summary>'
            '<p>A clear view of progress, effort and the evidence behind them.</p></details></div>'
            f'<a class="export-link" href="{_e(_target("/api/measurements", api_query))}">JSON report</a></div>'
            '<div class="scope-bar"><p><strong>All recorded history</strong><span>No date filter applied</span></p>'
            f'<p>{scope} · {len(all_plans)} in report {scope_link}</p>'
            f'<p>Canonical snapshot <strong>#{_e(data.get("canonical_store_sequence", "unknown"))}</strong></p></div>'
            '<nav class="cohorts" aria-label="Measurement cohorts">' + "".join(cohort_links) + '</nav>'
            '</div>'
            + _overview(plans, cohort) + _volume(plans) + _comparison(plans) + _kpis(plans)
            + '<div class="evidence-columns">' + _trace(plans) + _versions(plans, data) + '</div>'
            + _details(data, query, cohort, plans)
            + '<footer>Read-only measurements · Missing means unknown · Partial sums are lower bounds</footer>'
            '</main></body></html>')
