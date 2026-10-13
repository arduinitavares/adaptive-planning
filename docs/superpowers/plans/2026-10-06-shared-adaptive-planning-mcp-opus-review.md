# Copilot Opus implementation-plan review

Reviewed plan SHA-256: `f52671d4bde3d9d8d23e555e2bcbc1f316600d2fb315b832f7c3010d25dd3b99`.

Fresh Orca terminal `term_a47effcf-1fcc-4620-a4e9-79bc3fb0d43e`, provider conversation `898286d4-8427-4b71-b762-eb4f3d8efd45`. Runtime confirmed `claude-opus-5.5`, reasoning `high`, client context `default`; planned standard budget. All tool operations denied; zero reviewer tool calls. The following is the actual completed review, preserved verbatim. Coordinator disposition and corrections are recorded in the [review ledger](2026-10-06-shared-adaptive-planning-mcp-review.md).

````
### 1. Critical Issues (High Severity Only)
Path aliases: `plan` = docs/superpowers/plans/2026-10-06-shared-adaptive-planning-mcp.md; `spec` = docs/superpowers/specs/2026-10-06-shared-adaptive-planning-mcp-design.md.

**C1. Restore cannot keep reservations for work missing from the backup**
- Issue: `resolve_store_hold` accepts `HostWork` and `ResourceResolution` records (plan:58-59, 98). However:
  - Both records may reference only `ResourceId`s that exist in the restored registry or in restored uses (plan:59).
  - `resource_uses` has a foreign key to plan and attempt/use (plan:156).
  - Nothing says what durable record a `running`/`unknown` HostWork, or an `unresolved` resolution, becomes after the store hold clears.
  - It is also undefined which capability authorizes a store-wide command, because orchestrator capabilities are per plan (plan:760, 774; spec:127).
- Why it matters: The spec says a restore must never release live reservations (spec:201). After the backup cutoff, resources and plans may exist that the restored database does not contain.
- Failure scenario:
  1. A backup is taken.
  2. Plan P2 is created afterward, registers workspace W, and starts writing to it.
  3. The backup is restored. P2 and W are not in it.
  4. The inventory cannot name W by `ResourceId`, so it lists the work only as text.
  5. The orchestrator clears the hold.
  6. P1 assigns W while P2's worker is still editing it.
- Fix:
  - Let the inventory carry `ResourceClaim`s, and register them inside the resolution transaction using the import-style `register_claim` path.
  - Persist each non-stopped absent work item as a store-level unresolved `resource_use` that does not require a plan or attempt foreign key, so it takes part in `conflicting_uses`.
  - Define the authority for this command: an installation-level admin capability, or "current orchestrator of any plan" plus an explicit actor. Pick one.
  - Add a test: after resolution, assigning the absent resource is still refused until a later explicit stopped release.

**C2. Imported in-progress work cannot reserve its real workspace**
- Issue: `known_contexts: dict[StepId, ExecutionContext]` holds `ResourceId`s only (plan:65, 97).
  - In the first store, or for any workspace not yet registered, no such IDs exist. Only the source directory gets bootstrap registration (plan:753; spec:191).
  - `resolve_attempt` has no way to attach or reserve a context for an imported unresolved attempt later. Its options are `confirm-owner`/`replace`/`reject`/`release`, and only `replace` takes a context (plan:93).
  - "Missing identity" holds only the imported plan (plan:697; spec:193).
- Why it matters: The legacy worker may still be running (spec:193-195). Other plans can assign its workspace, because it is not reserved anywhere.
- Failure scenario: A first-store import of an in-progress legacy step whose worktree is X. `known_contexts={}`, so the imported plan is held. A newly created plan registers X and assigns it while the legacy agent is still editing X.
- Fix:
  - Let `known_contexts` (or a parallel field) carry `ResourceClaim`s that are registered and reserved inside the import transaction. Any conflict rejects the whole import.
  - Add a recovery resolution such as `attach-context`/`reserve` for unresolved attempts.
  - Test `test_active_import_preserves_latch_and_holds` on an empty store with a workspace claim. Assert that a second plan's assignment of that workspace is refused.

**C3. The review → fix → re-review loop has no defined attempt transitions**
- Issue: Plan:585 says a failed review records "rejected attempt/blocked step". The spec says reservations are kept "through review and any fixes" and that review/fix handoffs stay inside the task (spec:87, 149). Neither document defines:
  - a `submitted → active/submitted` transition for a fixed artifact, or
  - whether a second `record_checkpoint` on a submitted attempt is allowed.

  `test_old_review_cannot_accept_changed_artifact` (plan:594) assumes a v2 artifact exists on the same attempt but gives no way to produce one.
- Why it matters: Per-task review rounds are the normal Superpowers path. As written, every failed review fences the implementer's token (the plan:579-581 test asserts `fenced`). Every fix then needs a replacement attempt, and resource and stale semantics churn each round.
- Failure scenario:
  1. The reviewer finds a defect.
  2. The attempt is rejected and the implementer's token is fenced.
  3. The fix is relayed as late evidence that cannot progress, or the orchestrator must replace the attempt.
  4. Replacement keeps old resources unresolved until a stopped release.
  5. The same worker in the same workspace then conflicts with its own unresolved use.
- Fix:
  - Specify that a failed review returns the attempt to a "fix-required" sub-state, or to `active` with an incremented attempt version. Reservations, latch and token stay as they are.
  - Allow a new checkpoint with a new artifact version that supersedes the earlier one.
  - Reserve `rejected` for an explicit `resolve_attempt reject`.
  - Add `test_failed_review_fix_resubmit_rereview_accept` on a single attempt.

**C4. Which capability authorizes each command kind is undefined, so fencing is untestable**
- Issue: `CommandEnvelope` carries both `orchestrator_token` and `attempt_token` (plan:78).
  - The fixture always attaches the orchestrator capability from `receipt` to every command, including `start`, `submit` and `report_activity` (plan:132, 456-457, 491).
  - Task 5 and Task 15 refer to an "operation-authorizing capability" (plan:234, 501) but never define it per kind.
  - The spec requires two things that pull in different directions: takeover must not revoke worker attempts, and relays need the current orchestrator capability plus the attempt token (spec:127, 165).
- Why it matters: The implementation might reject direct worker commands after takeover because a stale orchestrator token is attached, or it might accept relays that carry a fenced orchestrator token. Neither failure is caught, because no test sends a worker command without orchestrator credentials. No negative test checks that an attempt token alone cannot run accept, review, revise, bind or release.
- Fix:
  - Add a normative table listing, per `CommandKind`: the required token(s), forbidden extra token(s), and whether replay checks the orchestrator, the attempt, or both.
  - Make the fixture send only the attempt token for direct worker commands, and both tokens only when `relay` is set.
  - Add tests:
    - a direct worker checkpoint succeeds after takeover;
    - a relay carrying a pre-takeover orchestrator token is `fenced`;
    - an attempt token presented to every orchestrator-only kind is `forbidden`.

### 2. Hidden Assumptions
- Stale detection "at read/mutation boundary" (plan:671) is a write. Plan:212 says reads are immutable, and the unauthenticated dashboard polls every 1000 ms (plan:286). Either reads mutate state (writes on every poll, revision/CAS churn), or stale is computed at read time and never persisted. The test expects `read_plan` to show `stale` (plan:680-682).
- Worker activity advances plan revision (plan:450). The five-minute activity cadence (plan:26) then invalidates the orchestrator's revise compare-and-swap (CAS) regularly and materializes the full snapshot (up to 10 MB) on every activity report.
- Hard-coded Host/Origin allowlists `127.0.0.1:8790`/`localhost:8790` (plan:259, 279) assume the fixed port. Task 1, 7, 10 and 20 run "ephemeral" Uvicorn/production apps (plan:113, 280, 329, 647), whose Host headers will be rejected.
- The shared `app.js` will tell legacy SSE mode (app.js:1039) apart from service polling mode (plan:286) without inline configuration (CSP, plan:300). The mode detection rule is unstated.
- `legacy_snapshot` will pass `renderablePlan`. That function requires `history` (≤100 entries), `updatedAt`, a non-empty `changeSummary`, string-only evidence, and a v2 `scope`/`generation` (app.js:26-43, 72-79). Service `Evidence` is structured (plan:61), and nothing maps service events to this history.
- Moving validators into core keeps the tests' module-constant patches working. `lp.MAX_FILE_BYTES` is patched at test_corrective_regressions.py:244 and 251, while the size checks are at live_plan.py:514 and 991. Plan:134 and 149 say "storage-fault patch targets" without listing constants.
- The legacy script assumes the `adaptive_planning/` sibling package is always present after extraction (plan:149). Before this change it was self-contained (live_plan.py:5).
- An installed client will retain the orchestrator token across session close and "resume" (plan:343, 661). Nothing says where it is stored, or that resume requires takeover.
- The evidence supplied does not verify any SDK API details (`Client(mode=...)`, `streamable_http_app` kwargs, `httpx2`). Task 1 gating is the only proof.

### 3. Gaps & Missing Definitions
- No command reconciles conflicting resource identities, for example a path that resolves to ID X while the inode resolves to ID Y. Both the spec and the plan require "explicit reconciliation" (spec:139; plan:367), but the command list has no such kind (plan:78-98). Mixed case-attestation for the same path is also undefined.
- Invalidation: `invalidate_result` is internal (plan:508). No command kind invalidates a result outside a checkpoint (for example, from late evidence), and nothing defines how invalidation holds on consumers that were already accepted are cleared. Plan completion requires that no holds remain (spec:93).
- Raw backup restore bypass: the epoch change happens only in `restore_database` (plan:774). A backup file copied into the volume would start under the old epoch, and old capabilities would still work. Backups carry no "is-backup" marker that forces the restore path.
- Backup/export redaction of "protected persisted grant material" (plan:234) is unspecified for `export_database` (plan:760). "Protected" is also undefined: hashed vs. recoverable, and the key.
- Whether `register_resource` and `takeover` are allowed under the store hold is listed only partially (plan:774). Registration is needed to reconcile post-cutoff resources.
- Whether an attempt token remains valid for exact replay after the attempt reaches `accepted`/`released` is undefined.
- The `DomainError` codes (plan:76) lack `stale`/`not_found`, and the stale-heartbeat test asserts only `DomainError` (plan:683).
- Test imports omit `copy`, `threading`, `ThreadPoolExecutor`, `subprocess` and `sys`, although the examples use them (plan:43 vs. 380, 387, 743, 789).
- SDK test modules use "direct setup" (plan:132), but the Task 7 and Task 12 methods in `tests/test_service_sdk.py` use `self.f` (plan:273, 393-398).

### 4. Ambiguities & Weak Language
- "or bounded service sweep" (plan:671): choose one mechanism, and define whether marking stale emits an event or advances revision.
- "Add a semantic MCP tool only after its corresponding tests pass" (plan:348), but Task 20 says "Register all domain-gated semantic tools" (plan:660), and Tasks 12-19 each modify `transport.py`. It is unclear which task actually exposes each tool.
- "Execute canonicalizes validated payload" (plan:212): the canonical encoding used for idempotent payload equality (key order, tuple vs. list, float form) is unspecified.
- "verify actual expansion" / "Docs schema tests" (plan:331, 342) do not define pass criteria.
- "Test supported arm64/amd64 build platform evidence" (plan:323): no expected artifact.
- The Task 24 RED step allows "record new test already passes" (plan:794), so it is not a real red step.
- Plan:870 ("Do not add per-task reviewers to inline execution") conflicts with plan:876 ("required task/scoped reviewer seats").

### 5. Design & Architecture Critique
- A single 10 MB cap on the public projection "including coordination" (plan:19, 182) covers attempts, reviews and reservations, which only grow. A plan near the cap can reject the very mutations that would release resources or resolve holds, and stay stuck. Bound the projection to current/unresolved coordination, or exempt reductions.
- If worker heartbeats advance the global revision, workers and the orchestrator fight over CAS. Separating a "structural revision" from an "activity sequence" would keep CAS meaningful.
- Holds and invalidation are plan-scoped. Store-scoped coordination exists only for resources and the restore hold. This is acceptable within scope, but the C1 store-level unresolved use should follow the same model.
- Strong points:
  - Atomic transaction design: `BEGIN IMMEDIATE`, per-operation connections.
  - Bootstrap replay semantics: superseded grants return `private=None`.
  - Conservative OR-matching of resource identities.
  - First-step latch kept separate from attempt ownership.
  - Final-gate modelling.

### 6. Regression & Risk Analysis
- Legacy: extracting validators risks silently changing the size, UUID and datetime patch surfaces. `SnapshotRegressions.test_bounded_history_output_limit_and_failed_update_bytes` and `test_utf8_nonfinite_oversize_and_deterministic_wire` depend on `lp.MAX_FILE_BYTES` (test_corrective_regressions.py:244, 251).
- Legacy dashboard: shared `app.js`, `index.html` and `style.css` edits (plan:285) can break SSE-only legacy serving (live_plan.py:1288-1297; app.js:1035-1066). The DOM suite is the only guard.
- Recovery: C1 and C2 can each lead to two writers in the same workspace. That is the main safety property.
- Tests: hard-coded Host values make the SDK and interop gates fail on ephemeral ports. This risks someone "fixing" them by loosening validation.
- Operations: a raw backup copy restores without changing the epoch (section 3).
- Runtime readiness is 0. Nothing has been implemented or run.

### 7. Actionable Fixes (Prioritized)
1. C1: a claim-capable restore inventory, store-level durable unresolved uses for absent work, and a defined authority for `resolve_store_hold` (plan:58-59, 98, 156, 760-762).
2. C2: claim-bearing import contexts reserved atomically, plus an `attach-context` recovery resolution (plan:93, 97, 695-697, 753).
3. C3: define the fix/resubmit lifecycle on one attempt, change plan:585, and add a test of the full review loop.
4. C4: a per-kind capability matrix, split worker vs. relay fixtures, and negative attempt-token tests (plan:78, 132, 234, 448, 501).
5. Pick one stale mechanism. Keep reads free of side effects, or make sweep writes separate and authenticated (plan:212, 286, 671).
6. Derive Host/Origin allowlists from `Config.base_url` and the actual bound port in tests (plan:241, 259, 279).
7. Add a resource-reconciliation command and an invalidation-clearance command (plan:78-98, 367, 508).
8. Enumerate the legacy module constants and functions that are patch targets, and keep them in `scripts/live_plan.py` (plan:134, 149).
9. Specify the `legacy_snapshot` field mapping (history, changeSummary, updatedAt, evidence flattening) and how `app.js` detects its mode (plan:71, 286, 300).
10. Bound or exempt coordination from the 10 MB cap. Mark backups so only `restore_database` can serve them. Redact grant material from exports. Fix the `self.f` usage in SDK tests and the missing imports.

### 8. Final Verdict
- Clarity: 6/10. The contracts are dense and mostly precise, but capability-per-command, stale mechanics and restore resolution are underspecified.
- Correctness Confidence: 5/10. Four defects can still lead to two writers on one workspace or a stuck/fenced review loop. These are restore inventory, import reservation, review resubmission and capability routing.
- Production Readiness: 1/10. Nothing is implemented or verified, so runtime readiness is 0; the plan itself needs the fixes above.
- Risk Level: High. The plan's concurrency and recovery guarantees are its core, and both have gaps where reservations are not held.
- Implementation-plan verdict: changes required.
````
