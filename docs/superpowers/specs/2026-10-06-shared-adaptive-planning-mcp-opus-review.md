````
## 1. Critical Issues (High Severity Only)

**H1 — Holds block assignment and start, but not acceptance**
- Issue: L95 refuses assigning or starting held work. L121 allows acceptance after "reassessment resolution or recorded holds." Under L75, a dependent only needs its prerequisites "accepted complete."
- Why it matters: a step that was already active when a proposal named it can still be accepted.
- Failure scenario: Worker A's checkpoint says sibling B's interface assumption is wrong, so B is held. B was already active, submits, and is accepted "with recorded hold." B's dependents are not themselves held, so they become assignable on top of an invalid output.
- Fix: refuse acceptance of any step under an unresolved hold, and propagate holds to transitive dependents. Restrict "recorded holds" in L121 to the proposer's own step.

**H2 — "Orchestrator-only" operations are not enforceable, and competing orchestrators are not fenced**
- Issue: L53 uses one shared bearer credential and self-declared labels. L103, L121, L137, and L145 treat accept, resolve/release, hold clearing, and structural edits as orchestrator operations. L133 lets a new orchestrator resume, but nothing fences the previous one.
- Why it matters: review gates and reservation safety depend on who calls an operation, and the server cannot tell.
- Failure scenario: a Codex session and a Copilot session both resume a plan and interleave assignments and acceptances. Or a worker, following its workflow's "mark complete" habit, accepts its own attempt, or resolves its stale predecessor and releases workspace X while that predecessor is still editing.
- Fix: issue a per-plan orchestrator lease token on create or explicit takeover; takeover records an event and fences the previous lease. Require the lease for accept, resolve/release, hold clearing, review gating, and structural edits. Worker attempt tokens permit only start, activity, and checkpoint. State that this protects against confused agents, not malicious ones.

**H3 — Restoring a backup can silently drop live reservations**
- Issue: L163 allows rollback to a pre-migration backup. L63 says epoch-replacing recovery changes generation, but restore is not defined as such a recovery.
- Why it matters: reservations are the only cross-plan safety mechanism.
- Failure scenario: the new image runs for two days, then a schema problem forces a restore. Assignments and reservations created after the backup disappear. Another plan is assigned workspace X while X's worker is still editing it, and tokens issued before the backup still validate against the rolled-back state.
- Fix: every restore mints new generations and marks all non-terminal attempts stale-unresolved. It also imposes a store-wide assignment hold until the orchestrator records reconciliation, and reports the backup cutoff time.

**Medium blockers**
- **M1 — Workspace identity can change or be reused (L111).** APFS `st_dev` may change on remount or reboot, and inodes are reused after worktree deletion. Stale reservations outlive reboots. Treat a match on either (dev, inode) or the case-folded canonical path as a conflict.
- **M2 — Workers may not be able to publish (L86, L103, L135).** Dispatched subagents may lack MCP tools or the credential. Long tool calls cannot send a heartbeat every five minutes. Define orchestrator-relayed publication for an attempt, and let stale-but-unreplaced attempts submit flagged checkpoints.
- **M3 — No transition table (L71–73).** Which step status covers "submitted"? Does a rejected attempt return the step to pending? How is plan `running`/`blocked` derived when concurrent attempts differ?
- **M4 — Reservation lifetime through review and fixes is undefined (L109, L117–119).** If submission releases the workspace, reviewer tests run unreserved.

## 2. Hidden Assumptions
- All three clients send static bearer headers over Streamable HTTP and will not divert into OAuth discovery after a 401.
- Models reuse the same idempotency key on retry (L105). If one regenerates the key on plan creation, a duplicate plan results (L139).
- Dev/inode registrations, imported scope and latch, availability claims, and "prior use stopped" are truthful. All of these are host attestations, not server enforcement.
- Accepted-but-unmerged commits can be made present in consumer worktrees without unrecorded integration work (L77, L121).
- Plans name the same external resource consistently (L111).

## 3. Gaps & Missing Definitions
- Idempotency key scope, retention period, and behavior after a generation change.
- Who classifies a reassessment as "material," judges whether effects are "bounded," and sets or clears holds.
- Availability methods for code: prerequisite is an ancestor of the base, merged into the consumer (new base recorded), or supplied by artifact digest. Merging multiple prerequisites needs a defined record.
- Failed final review: retire the review step, add fix steps, and bind a new review to the new branch head.
- Late reports from fenced attempts (L177): the old worker is the best witness that it stopped. Allow append-only notes that change no state.
- Warning when a new plan duplicates an open plan's workspace and objective.

## 4. Ambiguities & Weak Language
- L121 "or recorded holds."
- L95 "if its effects are not yet bounded" names no decider.
- L109 "pure reads of immutable artifacts" conflicts with reviewing a mutable worktree.
- L135 "should report" is advisory, yet staleness has consequences.
- L127: state explicitly that a replacement attempt for the same step remains within first-step permission.
- L71: meaning of plan `blocked` when one sibling is blocked and others run.

## 5. Design & Architecture Critique
Using one SQLite authority is sound and justified by cross-plan reservations. Gating on checkpoints at acceptance is the correct enforcement point, and L99 is candid that local work cannot be observed. The central weakness is role authority: most safety rules depend on who calls an operation, but identity is a single shared token. The reservation taxonomy is heavier than v1 needs but defensible if identity matching stays conservative. Legacy in-progress import (L159) duplicates stale-recovery logic in a second path.

## 6. Regression & Risk Analysis
- Per-attempt semantic merging (L105) departs from whole-snapshot CAS. Legacy tests (L153) must not be loosened to accommodate it.
- The first-step latch combined with import, replacement, and multiple workers needs targeted cases.
- If any document still presents the legacy path as the default, L155 instructions will contradict each other.
- After Mac sleep, a clock jump can mark many attempts stale at once. Present this as informational, not alarming.

## 7. Actionable Fixes (Prioritized)
1. Refuse acceptance of held steps and propagate holds downstream (H1).
2. Add an orchestrator lease with fenced takeover, and partition operations by token (H2).
3. Make restore mint a new generation, mark attempts stale-unresolved, and impose a store-wide hold (H3).
4. Match workspace identity conservatively on either key (M1).
5. Add relayed publication and allow checkpoints from stale attempts (M2).
6. Write one transition table covering attempt, step, plan, hold, and reservation lifetimes (M3, M4).
7. Define availability methods and the failed-final-review flow.
8. Add a table classifying each rule as server-enforced or host attestation.

## 8. Final Verdict
Not ready for user approval and implementation planning until H1–H3 are fixed and M1–M4 are addressed. These are targeted edits, not a redesign; the overall architecture is appropriate.

Most valuable simplifications:
1. Import only quiescent legacy state with no in-progress steps. Active legacy tasks finish on the retained legacy path, which removes L159's recovery import.
2. Derive plan state from step, attempt, and hold state instead of storing it.

Clarity 7/10 · Correctness Confidence 5/10 · Production Readiness 4/10 · Risk Level: High (as written)
````
