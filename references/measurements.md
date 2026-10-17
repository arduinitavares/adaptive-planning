# Collect planning measurements

Use the same Mac MCP service and planning records. The service records its own MCP tool calls automatically. It cannot observe model tokens, external tools, human work or billing inside an MCP client.

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
