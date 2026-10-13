# Design review record

Design: [Shared adaptive-planning MCP service](2026-10-06-shared-adaptive-planning-mcp-design.md)

Source baseline: `06a79f7d52554a1660b587013bffdd5a71b12e16`, branch `arduinitavares/mcp-linux-desing`.

Independent reviewer: a fresh Codex-native agent, requested as `gpt-6-astra`, reasoning `high`, standard context. The delegation accepted those explicit settings; separate effective-model runtime metadata was not exposed. Review used the installed `preimplementation-review` instructions and the written specification. No product implementation or runtime checks were performed.

## Findings and corrections

| Finding | Severity | Correction |
| --- | --- | --- |
| Accepted dependency artifacts may be absent in another worktree | High | Starting a consumer requires bindings to accepted result versions and recorded availability checks in its execution context. |
| Reassessment proposals could unlock an outdated graph | High | Apply/reject changes or hold affected work; unresolved proposals cannot unlock affected assignments or starts. |
| Replacing a stale worker could release resources it still uses | High | Preserve prior reservations independently; a replacement worktree does not release the old workspace or shared external resources. |
| Resource aliases and repository-wide overlap were unspecified | Medium | Register physical workspace identities and define repository, workspace, and external-resource overlap across all plans. |
| Final branch review could circularly block implementation task acceptance | Medium | Task reviews gate their tasks; final branch review is a separate prerequisite for plan completion/integration readiness. |
| Active legacy work lacked ownership during import | Medium | Quarantine active imported work, preserve execution scope, and resolve ownership/resources before dispatch. |
| A different import key could create a second canonical plan | Medium | Persist a unique legacy-source mapping and explicit authority handoff; changed legacy content produces a conflict. |
| Repository identity was incorrectly required for non-Git work | Medium | Declare resource context explicitly; local non-Git and external-only tasks reserve their actual resources. |

The second independent pass confirmed closure of the three original high-severity findings and the original medium findings. It identified the non-Git resource-context ambiguity; the author then corrected that definition and added acceptance scenarios for non-Git and external-only work.

## Opus 5.5 review

The user requested an additional independent review. GitHub Copilot CLI 1.0.92 ran a fresh `claude-opus-5.5` session with reasoning `high`, standard context, and tools disabled. Runtime events confirmed that exact model and effort, one completed turn, zero tool executions, and exit code 0. The actual 197-line specification was supplied without Astra's findings. Its SHA-256 was `555a9572504a4d4e80633262b89edc484703ecc4c595f652b96df1c5559191bc`.

The [original Opus review](2026-10-06-shared-adaptive-planning-mcp-opus-review.md) is preserved separately. Its verdict applies to that input, before the following author corrections; Opus has not reviewed the corrections.

| Finding | Assessment and correction |
| --- | --- |
| H1: An already active held step could be accepted | Accepted. Holds block acceptance and cover transitive dependents. Material reassessment installs provisional holds with the checkpoint, before review/acceptance. Only an unaffected proposer may be accepted while other work is held. |
| H2: Competing orchestrators and worker access to orchestrator operations | Accepted within the cooperative local trust boundary. One per-plan orchestrator capability with versioned, explicit takeover fences old mutations; worker tokens permit their own start/activity/checkpoints. This does not add multi-user security or authenticate model identity. |
| H3: Restoring an old backup could drop live reservations | Accepted. Restore changes epochs, invalidates capabilities, preserves unresolved recovered work, and holds the entire store until host-side activity including post-backup work is reconciled. |
| M1: Physical identity changes or reuse | Accepted with qualification. Match physical identity or canonical path aliases conservatively; case-fold only on an attested case-insensitive volume. Blindly case-folding all Mac paths would collapse valid distinct resources. |
| M2: Worker lacks MCP access or becomes stale during a long call | Accepted. Allow attributed orchestrator relay, flagged checkpoints from stale unfenced attempts, and relayed append-only late evidence from fenced attempts, without silently clearing ownership or holds. |
| M3: State transitions undefined | Accepted. Define attempt/step transitions and derive plan status with explicit stop, completion, concurrent activity, and blockers. |
| M4: Reservations through review/fixes undefined | Accepted. Submission retains reservations; verification contexts reserve resources or explicitly hand off exclusive use after stopped-use confirmation. Acceptance releases only resources with that confirmation. |
| Further definitions | Define idempotency scope/retention, dependency availability records, failed-final-review correction flow, same-step first-step retry, duplicate-create identity, and server enforcement versus host attestations. |

The proposed simplification to derive plan state was adopted. Quiescent legacy transfer is preferred, while unresolved imports reuse the existing stale-recovery contract rather than a separate ownership path. Active import remains a specified compatibility capability. The feedback-loop explanation requested by the user was added to both the design and README; concurrency preserves per-outcome reassessment and holds affected work.

## Correction verification

A fresh Astra reviewer, explicitly requested as `gpt-6-astra` with reasoning `high` and standard context, read the corrected specification and original Opus review. Effective runtime model metadata was not exposed. It confirmed the original defects were addressed and identified two remaining medium contract gaps: capability-issuing retries after a lost response, and when provisional reassessment holds become effective. The author added bounded bootstrap replay semantics and atomic holds at checkpoint publication, with an interleaving acceptance requirement. A focused follow-up confirmed both were closed and reported no remaining specification blocker in the reviewed scope.

Documentation checks found no trailing whitespace or missing final newlines in the four changed/new Markdown files; `git diff --check` passed. Product code, runtime configuration, and Git HEAD were unchanged. Documents remain uncommitted. These checks do not establish implementation correctness.

## Verification boundary

This record documents specification review and corrections. It does not establish runtime interoperability, filesystem isolation, test execution, production readiness, or implementation correctness. The design lists the required implementation checks. The user approved the written design on 2026-10-06; implementation planning is the next stage. This approval does not authorize product implementation or client configuration changes yet.
