# Collect planning measurements

Use the same Mac MCP service and planning records. The service derives lifecycle counts from committed planning evidence and records its own MCP tool calls automatically. It cannot observe model tokens, external tools, human work or billing inside an MCP client. `read_measurements.receipt_contract` provides the contract through MCP even when this file is unavailable remotely.

## Collection during normal planning

On creation or resume, submit a context receipt without asking the user. Set `skill_version` to `sha256:<SHA-256 of the SKILL.md actually loaded>`. If the file cannot be inspected, omit the hash rather than guessing it. This edition is `adaptive-planning/2026-10-18-receipts-1`, with workflow receipt contract `adaptive-planning/receipts-v1`. The edition label does not replace a content hash: Windows and Mac can retain different prior planning instructions. Classify real project work as `operational`, deliberately synthetic acceptance work as `fixture`, and uncertain cases as `unknown`. Preserve an existing explicit classification unless new evidence warrants a change. Report model/provider/effort only when exposed. Settings in a default config do not prove the effective model after an override.

Include `client_name` as the actual Codex or AGY surface, `client_os` as `windows` or `macos`, and exposed `client_version` and `collector_version` when available. These are client attestations, not authenticated identity. Keep session paths, prompts and credentials private.

Codex and AGY on Windows and Mac follow this same receipt procedure. The server does not require a host hook for canonical lifecycle coverage. On 2026-10-18, installed Codex structured output and owned native records exposed usage; documented native hook inputs did not contain token counts. AGY headless results expose usage, but persistent-session totals are cumulative and native lifecycle hooks have no documented token counters. Native AGY token collection is not qualified here. Use only an available, correctly scoped native receipt; a hook firing is not proof of complete accounting. There is no new daemon, hook trust, or client-settings change in this rollout. Instruction-driven context and semantic receipts are automatic agent actions but remain observable for omissions.

Use a source ID scoped to client session, plan, receipt kind and the native event ID or immutable content hash (maximum 160 characters). Store the original timestamp, body and ID in private task records. On an unchanged resume, reuse the saved context receipt. On settings changes, create a new observation. Retrying the same body replays; changing its timestamp or body under the old ID conflicts. Never create a new ID merely because a network response was lost.

At a checkpoint, send new evidence-backed defect/rework/usage receipts with the current domain capability. A linked defect is required before corrective effort. Record exact measured deltas once; a cumulative session total is not a fresh delta at each checkpoint. Shared multi-plan session usage cannot be split without an observed boundary. Omit unsupported values and leave coverage incomplete. This requires agent tool calls, not a user reporting ritual. It is instruction-driven collection; it cannot guarantee that every client follows the skill.

If usage is measured but its rework classification is unknown, use `corrective_rework: null`. Use `false` only for observed non-corrective work. Unknown classification contributes to total resources, but cannot establish zero rework or complete rework coverage. Activity must still match the bound step; omit usage whose plan/step allocation cannot be established.

The server automatically derives acceptance progress, exact linked reviews, check outcomes, reassessment dispositions, plan revisions, invalidations, attempt counts, active holds and calendar spans. These cover old and new canonical calls without hooks or duplicate receipts. A linked review proves a distinct recorded reviewer and the exact accepted artifact. It does not prove reviewer model family, team independence or production correctness. Canonical invalidations and failed checks are shown separately from explicit defect receipts. Calendar spans include pauses and cannot substitute for active work or person time.

The optional tools are `record_measurement(observation)` and `read_measurements(plan_id?, after=0, limit=100)`. Open `/measurements` on the existing dashboard for the report. `/api/measurements` exports the same report as JSON. Use `plan_id` to select one plan. The trace has at most 100 observations per page; follow `next_after`. KPI summaries cover the selected plan collection, independently of the trace page. At most 100 plans and 20,000 observations can be aggregated in one request. Select one plan if a collection exceeds the limit.

## Common envelope

```json
{
  "source_record_id": "client-session-123:context",
  "epoch": "<read_service_info epoch>",
  "plan_id": "<existing plan ID>",
  "kind": "context",
  "observed_at": "2026-10-18T00:00:00Z",
  "step_id": null,
  "attempt_id": null,
  "checkpoint_sequence": null,
  "data": {
    "case_kind": "operational",
    "workflow_version": "<workflow commit or content hash>",
    "skill_version": "<hash of the skill actually loaded by this client>",
    "model_id": "<actual configured model>",
    "provider": "<actual provider>",
    "effort": "<actual exposed effort>"
  }
}
```

Omit unknown context fields. Do not substitute assumed model settings. At plan start, classify `operational`, `fixture` or `unknown`. Record context again when settings change. Bind worker context to its step and attempt. The service adds its actual Python-source fingerprint, runtime versions, receipt time and authentication provenance. A client context is an attestation. The bundled server skill does not identify a client's installed skill.

Use a stable, globally unique `source_record_id` within the epoch. Retry the exact same observation with that ID after an uncertain response. Different content under the same ID fails with `conflict`. Never reuse a provider receipt under a new ID or submit cumulative counters as new deltas. The ledger deduplicates IDs, not the meaning of arbitrary evidence text.

For kinds other than `context`, include one capability field in the envelope: `orchestrator_token` for the plan owner or `attempt_token` for the exact worker attempt. Worker reports also require `step_id` and `attempt_id`. Coverage and independently verified outcomes require the owner. Completed attempts can report while their capability remains valid. Fenced or cancelled grants confer no reporting authority; the current owner can record delayed receipts. Capabilities are checked, then discarded. Never put credentials in `data`, evidence, IDs or version strings. Report content is visible under the dashboard's existing public read policy.

## Evidence and cost data

`effort` records contain delta quantities. Examples:

```json
{
  "activity_kind": "debug",
  "corrective_rework": true,
  "cause_id": "regression-17",
  "metrics": {"input_tokens": 1200, "cached_input_tokens": 600, "output_tokens": 400, "worker_seconds": 45},
  "source": "provider_receipt",
  "evidence": "receipt session-123/request-9",
  "started_at": "2026-10-18T00:00:00Z",
  "ended_at": "2026-10-18T00:00:45Z",
  "time_kind": "active"
}
```

Activity is `plan`, `implement`, `verify`, `review`, `debug` or `recover`. `corrective_rework` is mandatory. A true value requires a previously recorded `defect_id` from this plan. Normal verification and exploration are not corrective rework just because a check fails.

Supported metrics are `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `tool_calls`, `worker_seconds`, `human_seconds` and `reviewer_seconds`. Token/call counts are integers. Values are finite and nonnegative. Cached input is part of input; reasoning can be part of output. Do not add these subsets to their totals. Person-seconds count work by each person; elapsed spans use the union of simultaneous intervals within each time category. Active, wait and blocked categories can overlap and must not be added as exclusive partitions.

`source` is `provider_receipt`, `client_measured` or `estimate`. Keep estimates separate from measured amounts. Actual billing uses `billing_amount`, `currency` such as `USD`, and `billing_scope` as `isolated` or `shared`. Estimated cost uses `estimated_amount` with those same currency/allocation fields. A shared account debit is not attributable task cost. The report keeps billing/estimate, currency and allocation buckets separate.

`defect` data requires `defect_id`, `stage` as `caught` or `escaped`, `status` as `open` or `resolved`, `cause` and `evidence`. The cause identifies the introducing step, attempt or artifact and why it caused the failure. `caught` means before acceptance; `escaped` means after acceptance. Reuse the same defect ID with a new observation ID to record resolution. Its original cause and stage cannot change. Failed checkpoints, blocked work and reopened attempts do not automatically become regressions.

`outcome` requires `step_id` and data with `status` as `verified`, `failed`, `blocked` or `unresolved`, plus evidence. A verified outcome also requires its exact `attempt_id` and canonical checkpoint event sequence, `independent: true`, `verifier`, and `result_version`. The owner must attest this. The server checks current canonical acceptance, the exact passing checkpoint and accepted result version. It also rejects the implementing assignee as verifier. This is evidence-linked attestation, not independent identity verification by the server. Invalidating or replacing the result prevents an old attestation from counting as current verified success. After acceptance, only current-result owner attestations can change the step KPI. Old-attempt receipts and worker outcomes remain in the trace. They cannot override owner verification. Eligible attestations are ordered by observation time, then receipt sequence.

`coverage` data contains `metrics`, a list of the supported metric names or `regressions`, and evidence that all relevant deltas for this plan were reconciled. Record it after the final receipt. Later reported quantities make that metric partial again until a new reconciliation. Without coverage, a known sum is a lower bound. With no reports, the value is null. Complete client-attested coverage can establish a true zero. Money remains partial because billing allocation and final invoices can arrive later.

## Read the five KPIs

- Outcomes count current steps, with an explicit denominator. Canonical acceptance without independent attestation is separate. Failed and blocked attestations do not silently disappear.
- Regressions count explicit defects. A fixed contained defect does not erase a verified output. No detection coverage means the observed defect count is a lower bound.
- Corrective rework is a subset of reported resource usage in the same unit. It is already included in total resources.
- Creation-to-acceptance time starts at plan creation and ends at acceptance of each attested step. It is not diagnosis or active execution time. Unverified/unresolved steps are censored. Measured active/wait/blocked intervals remain separate.
- Total resources show measured lower bounds, their completeness, and separate actual/estimated money buckets. Global MCP call traces cover this server only and are not assigned to arbitrary plans.

Compare operational cases with similar scope, versions, model settings and coverage. Exclude fixtures and keep unknown classifications visible. These observations can support a monthly descriptive review. They do not establish that a workflow change caused an improvement. Self-improvement remains a proposal followed by new versioned observations or a controlled comparison. No automatic skill or policy edits are enabled.

## Storage and recovery

`measurements.sqlite` is independent of canonical `planning.sqlite` schema 2. The same container volume retains both. Rolling the image back leaves the measurement file in place and the old server ignores it. Existing canonical backup/restore commands cover the planning store only; they do not archive or restore measurements. After a canonical restore, the new epoch isolates historical measurement rows. Treat trace loss or collection errors as missing coverage, never zero. `collection_health` reports dropped automatic traces since the current process started.

Language check: STE Issue 9-oriented candidate; full dictionary conformance is not claimed.
