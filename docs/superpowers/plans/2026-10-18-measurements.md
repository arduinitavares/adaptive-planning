# Measurement collection implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect missing measurements and version metadata on the existing Mac service.

**Architecture:** Keep canonical planning state unchanged. Add an independent observation ledger in the same volume, authenticated MCP tools and a bounded read-only report.

**Tech Stack:** Existing Python 3.13, SQLite 3.53.4, MCP SDK 2.3.0, Pydantic and Starlette; no new dependencies.

**Spec:** [Measurement contract](../specs/2026-10-18-measurements.md)

## Global constraints

- Preserve canonical schema version 2 and existing tool envelopes.
- Use the existing Mac container, volume, authentication and network bindings.
- Leave SKILL.md and the original prototype checkout unchanged.
- Unknown measurements remain unknown. No inferred model costs or regression labels.
- No new dependencies, global permission changes or synchronization backups.

## Review focus

- Late receipts and credential fencing must coexist without granting lifecycle authority.
- Retried deltas must not double count; conflicting IDs must fail.
- Missing/partial cost data must never appear complete or zero.
- Parallel work and incompatible resource units must not be added as wall time or money.
- Telemetry and image rollback must preserve canonical data and service availability.

## Tasks

### Task 1: ledger and KPI calculation
- [ ] Add decisive unit tests; run them and retain the missing-feature failure.
- [ ] Implement strict observations, append-only/idempotent SQLite storage and bounded reporting.
- [ ] Prove unknown/zero, causal rework, unit separation and overlapping interval behaviour.

### Task 2: service, MCP and report
- [ ] Add integration tests for capabilities, epoch/reference checks, strict registry and canonical invariance; observe failure.
- [ ] Add service adapters, automatic sanitized MCP traces and GET/HEAD report routes.
- [ ] Run focused and full Linux tests. Preserve runtime/dependency pins.

### Task 3: review and rollout
- [ ] Obtain fresh independent cross-family review; resolve material findings in one fix pass with decisive checks.
- [ ] Build the image and smoke test it with disposable data before production replacement.
- [ ] Roll out to the same Compose service, retain old image rollback, verify live identity/data/MCP/report/persistence.
- [ ] Record audit, verification receipts, rollout identity and collection instructions.
