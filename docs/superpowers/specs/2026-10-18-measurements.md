# Measurements for the existing planning service

The Mac container is the authority. MCP clients use this same service. Add observations without changing the planning lifecycle, canonical schema version 2, existing tool envelopes, authentication, acceptance, or historical events. Leave SKILL.md unchanged.

## Audit

The read-only snapshot at 2026-10-17T23:23:35Z contains 5 plans, 289 plan events, and 19 checkpoints with reassessments. SQLite contains 290 events in total. Only one plan declares workflow names. The recent container log sample has session termination messages without usable cost telemetry. These are planning records, not five independently verified bug fixes. Historical versions, regressions, resource costs, and active-time coverage are unknown.

## Contract

Use `measurements.sqlite` in the existing `/data` volume. It has its own version and append-only observations. An older image ignores it. Keep the original image available for rollback; no canonical database migration or file synchronization backup is required.

Add `record_measurement(observation)` and `read_measurements(plan_id?, after=0, limit=100)`. Add GET/HEAD `/measurements` and `/api/measurements`. All existing request boundaries apply. Write attribution requires the current plan capability or a valid bound worker capability. Epoch and referenced step, attempt and checkpoint must match. Late observations may refer to completed work, but fenced credentials remain fenced. Plan-level audit classification and client metadata may use the existing service bearer and are labelled as such. They confer no planning authority. No credentials are persisted in the ledger.

Every observation has a stable source record ID, epoch, plan, kind, optional step/attempt/checkpoint, observed timestamp, and typed data. Exact retries count once. Reusing an ID with changed content fails. The server adds receipt time, source fingerprint and authentication provenance. Context records identify workflow version, actual client skill version, model/provider/effort and operational/fixture/unknown case classification when known. Never substitute the bundled skill for the client's actual skill version.

Kinds are context, outcome, defect, effort and coverage. Outcome attestation is scoped to one step. Verified requires current accepted output, a linked passing checkpoint and independent verification evidence. Client attestation remains labelled. Defects require explicit cause, evidence and caught/escaped classification; failed checks alone are not defects. Effort consists of unique delta records with activity category and optional corrective-rework cause. The latter must link an existing defect in the same plan. Tokens, calls, person-seconds, wall time, actual currency and estimates use separate buckets. Cached input is a subset of input and reasoning can be a subset of output. Do not add overlapping categories or different currencies. Coverage records attest completeness for named metrics in one plan; without these, sums are partial lower bounds. Measurements must use stable IDs for each provider receipt/span rather than resending cumulative totals.

## Five KPIs

1. Outcomes: step counts by verified, accepted without independent attestation, failed, blocked, unresolved. Report the step denominator and plan classification. Fixtures and unknown cases remain visible and excluded from operational summary.
2. Regressions: explicit caught and escaped defect counts with cause and evidence. Report unknown detection coverage. A resolved contained defect does not erase a verified result.
3. Rework: effort marked corrective and linked to a defect, reported as a subset of total effort in each matching metric/unit/source bucket. Do not sum incompatible units or infer it from retries.
4. Time: canonical creation-to-acceptance windows separately from reported active, wait and blocked spans. Union concurrent spans for wall time; keep worker/person totals separate. Unfinished outcomes are censored. Never label an open attempt window active time.
5. Resources: known delta sums and coverage for tokens, tools, human/reviewer effort, billed currency and estimated costs. Missing is null. Explicit complete coverage can establish a real zero.

The report includes per-plan version contexts, service fingerprint and observation trace. It supports descriptive comparisons, not causal proof that a workflow improved results. Historical cases are not silently backfilled or classified from names. Log successful and failed MCP tool calls automatically with name, duration and result category only; do not store arguments, credentials, raw exceptions or model claims. Server call traces do not establish full task cost coverage.

## Acceptance

Prove duplicate and conflict handling, unknown versus zero, rework causality and arithmetic, interval overlap, capability boundaries, canonical invariance, strict SDK registration, bounded public reads, HTML escaping, and persistence. Run the existing Linux service suite in an isolated container. Obtain fresh cross-family change review. Build and test the new image before replacing the existing Compose service, keeping the volume, credential, epoch and LAN/loopback bindings. Verify live read-only MCP calls, the measurement page, and sidecar persistence. Any production smoke observation is a labelled fixture and excluded from operational comparisons.

Language check: reviewed as an STE Issue 9-oriented candidate. Full dictionary conformance is not claimed.
