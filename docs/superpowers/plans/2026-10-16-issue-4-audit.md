# Issue #4 audit and verification ledger

Baseline: `main` at `f8fed6c10228e7a615d6db7f703d02cee2de8e93`.
Scope: the [verified agent brief](https://github.com/arduinitavares/adaptive-planning/issues/4#issuecomment-6216385332).
Existing untracked `.agents/`, `.codex/`, `AGENTS.md`, and `docs/agents/` are preserved.

## 1. Critical issues

1. A strict shared reassessment validator would also reject already-recorded recovery proposals. `_reassessment_source` revalidates those records; the normal historical reader does not. A disposable-store experiment confirmed that an owner can reject a legacy recovery proposal today, but a hypothetical strict shared validator makes that operation fail atomically with `invalid`.
2. Graph synchronization and reassessment-hold resolution are different operations. `revise_plan` changes the graph without clearing those holds. Instructions must require explicit resolution before affected work can advance.

## 2. Hidden assumptions

Producing-step success does not establish an unchanged downstream plan. Historical audit records and exact request replay must remain usable after tightening NEW publication validation. A document edit is material when it changes tracked downstream definitions/references, even if the edit looks small.

## 3. Gaps and definitions

Reject otherwise-valid NEW normal/recovery checkpoints carrying `unchanged` plus any non-null draft, including an identical draft. Preserve prior authentication, currentness, limit, and artifact errors. Do not rewrite history, change schema, or infer a graph by parsing Markdown.

## 4. Clarified instructions

Material downstream specification/architecture/ADR/contract changes identify affected steps or unbounded effects. Synchronize the canonical graph, definitions, and document metadata before first-step stopping or further dispatch/readiness. Prefer reassessment `apply` with the actual draft; a separate graph revision still requires explicit source-hold resolution. Preserve permitted unaffected sibling work.

## 5. Chosen bounded design

Add a private publication-consistency helper and invoke it at both NEW checkpoint publication sites after existing validation and before their first event write. Leave shared validation, historical readers, and service replay ordering intact. Update both instruction surfaces and their contract coverage. No API, schema, migration, automatic draft application, deployment, or unrelated refactoring.

## 6. Regression and risk analysis

RED requirements: NEW normal and recovery rejection, plus missing instruction-contract coverage. Passing baselines: valid unchanged/material controls, legacy normal/recovery proposal resolution, exact authenticated replay, and earlier error ordering. Compatibility fixtures temporarily disable only the new guard during legacy publication, then exercise real replay/resolution with the guard restored.

## 7. Independent audit and disposition

Fresh AGY `claude-opus-5-5-medium` via this repository's Orca workspace; requested effort `medium`, standard context; read-only isolated packet and terminal sandbox. Conversation `7d980738-20aa-4e1f-8275-499b4677deb5` returned `READY WITH REQUIRED CORRECTIONS` after reading the supplied source/brief and canonical model policy. Backend logs exposed the requested model label and sandbox activation; backend effort/context were not separately exposed.

Accepted: publication-only guard at both pre-write sites; preserve historical readers and replay; distinguish RED requirements from passing compatibility baselines; distinguish graph revision from clearing holds; synchronize before boundary/dispatch.

The launch warned that `--mode plan` has no effect with slash expansion disabled. This warning is retained as a transport-control limitation; it does not establish a generally qualified planning route. No source hashes changed during the audit. The wrapper recorded exit 0 and a completed provider result. Orca separately reported `exited` with exitCode -1/`stop_unverified`; that receipt is not relabeled zero. The owned terminal was closed with a confirmed `ptyKilled` receipt. Sandbox process inspection was denied and was not retried. A fresh final review must use compatible mode controls and inspect actual completion.

## 8. Verdict and next phase

The bounded design incorporates the substantive audit corrections. The user explicitly requested continuing into implementation after the audit. Next: tests first, record RED on unchanged production/instruction bytes, implement the narrow guard/guidance change, run relevant and full checks, then obtain a fresh independent review of the final diff. This audit is not evidence of a tested fix.

## Verification results

- RED on unchanged production/instruction bytes: 71 tests, 6 expected subtest failures, no errors. Normal publication failed both identical/revised-draft cases; recovery failed active/assigned stale cases; both instruction-contract surfaces failed. Compatibility and earlier-error baselines passed.
- RED log: `/private/tmp/adaptive-planning-issue4-red-corrected.log`, SHA256 `3b46c419a31ba227bf3a8797930cc93dd75b742765f38adf3899aaf56cb7ac55`.
- Pre-edit SHA256: checkpoint module `55fceca8dc3cdebb1802b6fe729a957bbe2fde1e8c5a6dedf612fc2202b70c82`; main skill `50c450d365bead8000608e177e6d3cec0b0f486d5fa8584abdd666b06fe2d948`; companion `74319f37483d1de7a2132f3dafa520f0d802ba2662c52185f2798b410d81be1e`.
- The first RED attempt also exposed a stale-plan fixture setup error. Both attempts are retained; the fixture was corrected and clean RED rerun before any production/instruction edit.
- GREEN: 71 targeted checkpoint/recovery/instruction tests passed in 4.088 seconds. Log SHA256 `3790cd2528aeb03523480e14f328565fc4d2630f042c875d7bd5c6a9f178cdf3`.
- First full suite: 853 tests ran in 254.379 seconds, OK with one skip; exit 0. Log SHA256 `0bf4d08022ef159ac0e32184a8e9d92ae06c5f9a669ea7c7d7db1299cdaa90b5`. The Windows-only share-denying handle test is skipped on macOS; the suite's owned failed-startup fixture intentionally logs a traceback/SystemExit.
- Review hardening: qualified the synchronization trigger; documented rejection of a proposal superseded by an already-applied revision with explicit residual effects; normalized whitespace in normative contract assertions. Expanded recovery cases to identical/revised drafts plus stale-version/relay-conflict precedence, and both legacy paths to reject/apply resolution. Production guard remained byte-identical to the first full-suite candidate.
- A focused wording RED preceded the wording correction. The complete targeted suites then passed again: 71 tests in 4.572 seconds. Log SHA256 `0298913fe8c69301b1cfd535e52eb15a73a891830d6a5e9a50133d5d1243fbb9`.

## Final independent review

Fresh AGY `claude-sonnet-5-5-high`, requested effort `high`, standard context, ordinary read-only packet through the same task Orca workspace. Model label, plan-mode application and sandbox activation were exposed in logs; backend effort/context were not separately exposed. Compatible mode flags were used. Conversation `13a13cb4-12a1-4004-9fa0-22d7755d61d3` initially returned With fixes solely for missing full-suite evidence, plus minor guidance/test notes. The same recorded review was resumed only to check the final diff and supporting evidence; closure verdict: **Ready to merge: Yes**, no Critical or Important findings.

Disposition: I1 full-suite evidence supplied; M1 superseded-proposal resolution example added without making reject the only legal choice; M2 synchronization made conditional; M3 wrapping-tolerant contract checks plus harmless-edit/residual-hold coverage; M4 expanded recovery and legacy resolution cases; M5 deliberate private-helper patch retained only for old-server fixture creation. Consumer scenarios under first-step/deadline/peer pressure chose material plus synchronization, harmless edits chose unchanged/no draft, and graph-revised-but-held work required explicit hold resolution. These are hypothetical consumer decisions, not empirical production compliance.

Declined coverage is preserved: deployment/rollout; other publication paths; unrelated repo-wide references; actual production agent compliance. Atomic-denial helper source was supplied, although the normal helper excerpt was truncated after its assertRaises header; the coordinator directly verified its error-code and full-iterdump post-state assertions and the corresponding passing tests. The reviewer saw the full-suite tail rather than running it; the coordinator verified the full command exit 0 and log summary. A final full suite after all refinements closed the remaining nonblocking verification note.

Reviewer wrappers/provider results completed with exit 0. The original final-review terminal reported exited/exit 0; the closure terminal separately reported exited/exitCode -1 with stop_unverified, retained without relabeling. Both owned terminals were closed with confirmed ptyKilled receipts. No same-family substitute, helpers, production edits by reviewers, new permission rules, billing changes, or native collaboration workers were used.

Final six-file diff SHA256: `4703ca875df0a2fb8c5fd4a33b399de9439cd0bf1d1289e93f6ae8df88135fc2`. At review completion, all code changes were local and uncommitted; existing unrelated setup files remained untouched.

## Final acceptance

Final-tree full suite: **853 tests run in 256.702 seconds, OK with one skip; exit 0**. That is 852 passed and the actual-Windows share-denying handle test skipped on macOS. Log: `/private/tmp/adaptive-planning-issue4-final-suite.log`, SHA256 `c279e571480f568919f0eea6b95ac41f2f88f992be72863b4de0cb4c4a210e81`. `git diff --check` passed. There are no remaining Critical or Important review findings. Optional notes about exhaustive real agent compliance and broader rollout remain outside this issue's local implementation scope.

The implementation and regression/instruction changes satisfy the verified brief locally. At verification completion, no commit or push had been performed. No deployment, installed-skill update, schema change, historical rewrite, or mutation of a real saved plan was performed. Rollback is the inverse of the recorded six-file candidate diff, applied only to owned changes with concurrent work preserved; no rollback was executed.
