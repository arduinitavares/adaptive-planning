# Automatic measurements and dashboard

The measurements page is an operational dashboard with acceptance progress, evidence coverage, five KPI groups, plan comparison, version context and a bounded canonical trace. The default cohort contains explicitly tagged operational plans. Fixture and unclassified counts remain visible and selectable. All-history totals and paginated receipts are labelled separately. Missing cost, usage and rework stay unknown; there are no synthetic trends or improvement claims.

## Collection boundaries

| Group | Automatic from the canonical store | Additional observed receipts |
| --- | --- | --- |
| Outcomes | Accepted steps, exact selected review links, check outcomes | Explicit independent verification attestations |
| Regressions | Result invalidations, failed and blocked checks, shown distinctly | Caught/escaped defect identity, cause and status |
| Corrective rework | No inferred waste from failed exploration | Measured effort linked to a recorded defect |
| Time | Calendar attempt and acceptance spans, with denominators | Active, wait, blocked and person time |
| Resources | Attempts, checkpoints, revisions and events; separate global MCP traces | Task-scoped native usage and actual billing |

Canonical derivation covers historical and future records from every client without a post-commit measurement write. It does not mutate the canonical store, weaken guards, or introduce a second lifecycle. A selected review link proves its recorded artifact/submission binding and a distinct reviewer label. It does not establish model-family independence, team independence or production correctness.

The small skill addition asks Codex and AGY to submit context on creation/resume/settings changes, and observed semantic or available usage receipts at existing checkpoints. No separate user reporting ritual is required. This is instruction-driven agent collection, not a guaranteed native hook. `read_measurements.receipt_contract` makes the receipt contract available remotely; immutable source IDs and bodies provide exact replay and conflicting-reuse rejection. Context includes validated client, OS and version fields. The loaded skill's content hash distinguishes host differences.

No client daemon, hook trust, provider setting, permission profile or credential changes are installed. Native token receipts are conditional on exposed data and exact allocation. The client audit verified Codex usage fields in an owned Windows CLI session and documented AGY headless usage. Complete native AGY per-turn accounting was not qualified. Cumulative session totals, overlapping token subsets and shared account debits cannot become independent task deltas or task billing.

Explicitly unknown rework classification contributes to total resources without asserting zero corrective effort. Zero rework requires complete, classified coverage. Partial positive observations remain lower bounds. A plan-scoped cohort receipt cannot be submitted with step or attempt scope.

## Installed skills

The pre-existing Windows and Mac skill contents differed. The install procedure adds only the receipt section and edition metadata to each baseline. It verifies that removing those additions restores the prior text. It preserves other instructions and files and creates no synchronization backups. Both hosts receive the measurement reference. New client sessions must load the updated skill and discover the measurement tools; already-loaded instructions and tool catalogs may remain stale.

## Verification and rollout ledger

- The initial full Linux suite passed 891 tests, with three platform skips. All three passed on Windows.
- The final installed nonroot image passed 42 focused tests, including actual HTTP/CSS routes, escaping, exact review binding, immutable receipts, unknown usage and review-fix regressions.
- Wheel and sdist asset checks passed. The real Mac Compose contract passed. The build initially omitted the new stylesheet; the exact Docker allowlist entry and installed-asset checks corrected that defect.
- Independent Argon High plan review identified classification, review semantics, idempotency, resource coverage and time-label risks. The spec records their dispositions.
- Whole-change Argon High review found partial-zero rework plus two smaller scope/proof issues. All three were reproduced with failing tests and corrected together. Fresh focused fix review returned Ready to deploy Yes with no unresolved Critical or Important findings.
- A temporary local preview launch was rejected by command policy and stopped. No alternate launch or security change was used. Browser verification uses the planned existing Mac service after rollout.
- Final Linux suite passed 894 tests; three platform skips passed Windows. Final installed-image focused set passed 42 tests.

## Live acceptance

The existing Mac Compose service was updated on 2026-10-18. Runtime container: `3dc6c6fa3db02baac53f31f202280d2bf8ae9596b06406297d1082bd19e7e7bc`. Image: `sha256:fc6d5cda169c5d18bba41f2a74000a3642c1576de36b8fac2803fe8b7397994d`. Python-source fingerprint: `5b9dc83f8634595ca3da23d496fe647365b21ed524d792066f5fe7b180ce1ffe`. Python 3.13.16 and SQLite 3.53.4 were retained.

The same `adaptive-planning-data` volume, UID/GID10001, restart policy and127.0.0.1/192.168.15.199 port8790 bindings were retained. Epoch, host identity, canonical schema2, store hold, token fingerprint and the297-event prefix matched before/after rollout. A live authenticated SDK fixture round-trip accepted a context receipt and replayed the exact retry without changing canonical sequence297. There were31 tools and the original29-tool catalog hash remained `8a6f4dbc21d35917326cea812670d52804fc617dcbe3df10bd4dcfd60f90ce9f`. The new fixture observation is excluded from operational comparisons. No native Codex/AGY token receipt was fabricated by that SDK fixture.

Desktop1440×1000 and mobile390×844 checks found no page overflow or console errors. Cohort controls displayed the correct fixture and empty-unclassified states. Keyboard activation opened source details. The dashboard is at `http://192.168.15.199:8790/measurements`. At acceptance, two operational plans contained11 of19 accepted steps and8 exact linked reviews. Historical token/person-time/model-version gaps remained visible.

Windows installed skill hash: `e1ab7573e15a422e4b5ae3eef107b163a7a367f311f15b73b8742816f3360da2`. Mac/repository skill hash: `d5d53ffaece909c654f62ee8c478b889f244584cde8fd18fd5290c3cb8fd3664`. Both measurement guides: `0b744a783f9c030b09c850e19373ec5736ddcd2b8eca286f52b95be481b76420`. Each installation preserved its prior planning instructions and created no synchronization backup. Windows skill validation passed. Original checkout and the user's `SKILL.ultracave.md` were unchanged.

Rollback uses the retained image tag `adaptive-planning:rollback-before-automatic-20261018` with the same Compose project and existing volume. No schema migration was introduced. The Mac main checkout remains unchanged; source changes belong to `alex/kpi-measurements`. Rebuild from the reviewed branch, rather than the older main checkout.

Language: Issue 9-oriented candidate; full dictionary conformance is not claimed.
