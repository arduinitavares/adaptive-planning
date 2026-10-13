# Implementation plan review record

Plan: [Shared adaptive-planning MCP implementation](2026-10-06-shared-adaptive-planning-mcp.md)

Requirements: [Approved design](../specs/2026-10-06-shared-adaptive-planning-mcp-design.md)

Source baseline: `06a79f7d52554a1660b587013bffdd5a71b12e16`, branch `arduinitavares/mcp-linux-desing`.

The user approved the design on 2026-10-06. This stage produces and reviews the implementation plan. Product implementation and execution-method selection remain pending.

## Author self-review

The coordinator reviewed the draft against the approved specification and current legacy publisher/tests, then corrected the following contracts before independent review:

| Area | Correction |
| --- | --- |
| Executable task order | Create the import-safe package before its first locked build; lazy-import later service fixtures; place acceptance/recovery assertions with their owning operations. |
| Authorization | Separate an explicit new authorization assertion from ordinary plan edits. Derive interval, baseline, and first-step latch from authoritative state, including assigned work. |
| Lost responses | Authenticate current capability/epoch before replay; return a matching committed receipt before applying new-command version checks. Superseded bootstrap grants remain private-free. |
| Empty-store bootstrap | Expose public installation information so the first client can discover the epoch and shared host identity. |
| Legacy compatibility | Extract scope and migration decisions as well as validators. Preserve strict legacy validation and distinguish renderer projections from importable publications. Verify the minimum Python interpreter separately. |
| SDK compatibility | Use the pinned official SDK's `Client` auto and legacy modes for positive transport tests. Modern requests do not use a legacy initialize handshake. |
| Resource/review identity | Scope step/review/result relationships by plan; retain physical/path aliases and unresolved reservations; preserve artifact and attempt provenance. |
| Dashboard/security | Define polling interval, pagination, and reconnect behavior; redact validation inputs; bound the full public projection; keep persisted credentials and capability material private. |

The corrected draft submitted for independent review has 26 tasks and 10,711 words. Its SHA-256 is `4167fc8bca940c92fea4776a6c93e57b04d90a61384b1f80bc414ecc5dc2ba58`.

## Independent review

A fresh, read-only Codex-native reviewer was explicitly requested as `gpt-6-astra`, reasoning `high`, standard context. The delegation accepted these requested settings; separate effective-model runtime metadata is not exposed. No descendants are authorized. The reviewer receives the actual specification, plan, source paths, acceptance checks, and verification boundary without the drafting conversation.

The first independent pass found two high-severity contract blockers: no explicit retirement input for failed review gates, and no initial capability/bootstrap replay contract for imports. It also identified acceptance-dependent test sequencing, fixture revision ambiguity, a temporary tool-list test that would fail after assignment tools were introduced, a vacuous completion assertion, and contradictory replay wording.

The author corrected all seven findings. A focused same-assignment Astra closure check of SHA-256 `d3e1546712702ea9edde5c119b76a5ee8e3475ade8e20388e4c73b1fd6984943` confirmed their closure. The corrections add explicit retirement/cancellation inputs, import's initial capability and safe bootstrap replay, historical accepted-result test seeding, explicit fixture metadata refresh/freeze behavior, replacement of the temporary tool-list assertion, a single-step unresolved-use completion test, and consistent exact-replay ordering.

The approved design's bootstrap paragraph was clarified to include import as creation of a new service plan, with the same original-current-grant-only disclosure rule for request replay and legacy-source deduplication. This records the ownership needed by the already specified import operation; it adds no execution authorization or resource-release permission.

The coordinator then identified a residual first-import cycle: ordinary resource registration requires an existing orchestrator, while import originally required a registered source ID. Astra confirmed this named blocker and recommended private registry resolution inside the import transaction. The plan and specification now permit exactly one existing source ID or physical source-directory claim. Task 11 defines the shared private helper; Task 22 uses it atomically and tests an empty installation, canonical source-ID replay, conflicting inputs, and rollback without orphan registrations. No unrelated bootstrap plan is required.

## Copilot review controls and assignment

The replacement user policy requires fresh Copilot `claude-opus-5.5`, reasoning `high`, through Orca for the written-plan reviewer seat. The coordinator loaded `orca-cli/SKILL.md` and the installed version-matched guide, then used coordinator-owned custom terminals. Earlier native Astra work above was a distinct architecture/concurrency assessment performed under the preceding policy.

Two synthetic control probes ran before any project packet was supplied. Both used an isolated `COPILOT_HOME`, an empty scratch working directory, disabled hooks, memory, IDE auto-connect, custom instructions, experimental features, built-in MCPs and remote export, and no inherited MCP configuration. Only `view` was exposed; `read`, `write`, `shell`, and `url` operations were denied. Each actual `view` invocation failed with runtime error code `denied` and the explicit `read` rule; no file contents, approval escalation or fallback tool appeared.

The unnamed probe exposed auxiliary `gpt-4o-mini` session-title generation. No project material was supplied to it. A fresh corrected probe used an explicit `--name`; its completed runtime events and shutdown model metrics contain only `claude-opus-5.5`. Both probes completed and their owned Orca terminals were closed after verified provider settlement.

Verified named-probe identities: Orca terminal `term_a7d1f4ea-4dcd-4933-b5e0-7f1c57d2a1ff`; provider conversation `f2317cc2-90aa-4a16-9268-fd27eb5c1fb9`. Runtime confirmed selected model `claude-opus-5.5`, reasoning `high`, client context tier `default`, remote steering disabled. Planned context is standard; it is distinct from the selectable client tier.

The actual review uses a fresh identically controlled named session: Orca terminal `term_a47effcf-1fcc-4620-a4e9-79bc3fb0d43e`; provider conversation `898286d4-8427-4b71-b762-eb4f3d8efd45`. Startup events confirm the requested model, effort and context tier. Copilot reports 328K total context with 187.3K free before submission; the scoped packet is 36,485 whitespace-delimited words and remains within the planned standard budget.

The supplied packet contains only assignment requirements, the approved specification, the complete plan, relevant legacy publisher/test/dashboard source, and verification limits. Its user-message content matches the coordinator's packet after the CLI's removal of the final newline. Provider `assistant.turn_start`, final `assistant.message`, `assistant.turn_end` and usage checkpoint confirm execution and completion; an Orca send receipt or its unsupported-provider idle report alone is not completion proof. The [actual completed review](2026-10-06-shared-adaptive-planning-mcp-opus-review.md) is preserved verbatim. Zero tool calls occurred. Provider shutdown metrics contain only `claude-opus-5.5`; observed session consumption was 119.83294 AIC, separately from remaining allowance, which is unknown. The provider exited normally and the owned Orca terminal was closed; existing user terminals were untouched.

## Opus findings and coordinator disposition

Opus's verdict was **changes required**. The coordinator checked its findings against the actual interfaces and legacy source before selecting corrections. The following are contract clarifications needed to satisfy the approved concurrency/recovery invariants; they do not authorize implementation or broaden the service into an agent scheduler.

| Finding | Disposition and correction |
| --- | --- |
| C1: restored inventory cannot reserve absent resources/plans | Accepted. Claim-bearing inventory registers identities atomically, stores absent-work uses independently of plan/attempt FKs, and retains them in global conflict checks after the store hold clears. A current orchestrator of any plan authorizes store-wide reconciliation; incomplete/unidentified inventory keeps the hold. |
| C2: first import cannot reserve an unregistered execution workspace | Accepted. Context claims resolve/register/reserve atomically; missing identity holds the store until explicit context attachment. A first-store cross-plan test proves the imported writer still excludes competitors. |
| C3: failed review has no same-attempt fix loop | Accepted. Failed review stays submitted/blocked. Explicit reopen preserves the token, reservation, frozen definition and latch; resubmission changes artifact version and requires matching review. Explicit rejection alone fences the worker. |
| C4: per-command capability routing unspecified | Accepted. Normative bootstrap/orchestrator/direct-worker/relay matrix, role-specific fixtures, extra-token rejection and takeover/relay/negative-role tests. Accepted worker grants are exact-replay-only; rejected/released/replaced grants are fenced. |
| Stale reads and activity/CAS | Accepted clarification. Reads do not mutate; only explicit authenticated sweep persists staleness once. Progress gates reject elapsed inactivity until sweep/recovery, so an unswept heartbeat cannot reactivate ownership and a failed command cannot commit a hidden stale write. Keep semantic activity revisions from the approved model and require bounded CAS reread/reconciliation rather than introducing another revision scheme. |
| Ephemeral-port Host/Origin tests | Accepted. Derive strict allowlists from the validated loopback base URL and actual port; production remains 8790. |
| Renderer mapping and mode detection | Accepted. Explicit legacy field mapping and externally read DOM data attributes select legacy SSE or service polling. |
| Resource reconciliation and invalidation clearance | Accepted. Explicit conservative registry redirects preserve all old uses/history; artifact-bound per-consumer invalidation resolution retains completed history and current valid-result identity. |
| Full coordination projection growth blocks cleanup | Accepted. Bounded summary/pages include current/unresolved work, retain all authoritative records, and remove resolved rows from the current view; history growth cannot prevent release/hold resolution. |
| Backup marking/export/private grant material | Accepted. Official backup copies cannot serve directly; restore changes marker/epoch/authority. Public JSON exports redact secrets. Private backup/replay material is protected by restrictive filesystem permissions, without claiming encryption. Arbitrary unsupported copies of a live database cannot be recognized reliably; document this operational limit. |
| Legacy limit/fault patch boundaries | Accepted. Preserve named module constants/call boundaries through wrappers and explicit pure-core limit injection, checked against actual corrective regressions. |
| Copied single-script self-containment | Qualified. The approved extraction requires the sibling core package. Preserve standard-library absolute CLI use from any cwd within the installed package bundle; document that copying only the script is unsupported. Do not duplicate the extracted implementation. |
| Cross-session grant retention | Accepted clarification. Supported private local cache or explicit takeover; no client persistence feature is assumed. |
| SDK API evidence absent from packet | Verification limit, not a new design defect. Primary documentation/pinned-wheel inspection exists, but runtime remains unproved and Task 1 is the explicit gate. No installed-runtime claim is made. |
| Imports, SDK test setup, error codes, canonical encoding, tool exposure, platform/config evidence | Accepted concrete drafting corrections: exact imports/setup and error codes; canonical bytes; each owning task exposes only its gated tools; concrete client expansion and image artifacts. |
| Inline review wording | Clarified. Task/scoped seats exist only where the selected workflow requires them; inline adds no per-task reviewers, and final independent Astra review remains required. |

The corrected plan is stable at SHA-256 `549d259cd0d0db3b0e6aa82980c139b72df26caafded5857f8c4186d2fec82b4`: 26 tasks, 14,517 words, 26 Python blocks with 44 named test methods. The coordinator completed its author self-review of specification coverage, interfaces, task order, review-focus tests and proportion. It made final field-order, started-post-backup-worker and artifact-bound-resolution proof corrections. A repeated mechanical check confirmed unchanged hash, syntax, local links, whitespace, receipt/payload fields and source boundary. These examples were parsed, not executed.

The fresh focused Opus closure attempt was stopped on a runtime model-control mismatch: despite exact Opus/high selection and an explicit name, successful ancillary calls appeared as `gpt-5.6-luna-utility` at `medium`. No completed verdict was produced. The coordinator interrupted the provider, observed abort, closed its owned terminal and verified no matching provider process remained. Zero tool operations occurred. The [control record](2026-10-06-shared-adaptive-planning-mcp-opus-closure-control.md) distinguishes startup selection from observed runtime models and does not overstate the utility's disclosure scope. This is an unsupported-control condition, not allowance exhaustion or a permission denial.

The coordinator disclosed the standing-authorized native fallback and launched one fresh `gpt-6-astra`, reasoning `high`, standard-context closure reviewer. Requested settings are explicit; separate effective native-model runtime metadata is not exposed. Its assignment was limited to C1–C4 and coupled hold/resource/proof contracts, with no descendants or implementation. The [actual completed native report](2026-10-06-shared-adaptive-planning-mcp-astra-closure.md) marks C1–C4 directly CLOSED and finds one coupled blocker, H1: writable verification reservations lacked an explicit store-hold command gate. It also identifies a cross-plan pagination version ambiguity and emphasizes submitted-overdue recovery verification.

## Final author corrections

The coordinator accepted the concrete H1 scenario and checked the two related notes against the plan before making these bounded corrections:

| Item | Correction and decisive planned evidence |
| --- | --- |
| H1: writable verification during restore hold | Normative hold contract and Task 17 reject creating/activating writable verification use before reservation/handoff/event changes; immutable inspection and observed review evidence stay available. A Task 17 synthetic hold fixture avoids depending on future restore. Task 23 exercises actual accepted artifact → backup → unknown later writer → restore → invalidation: held reservation fails without use/event/metadata changes; after inventory the orphan conflicts and a separate free workspace succeeds. |
| Foreign-plan orphan pagination | Both coordination DTOs include store_sequence, drawn from the existing store-wide committed event sequence in the same SQLite snapshot. Cursors bind epoch/plan/revision/store_sequence; dashboard discards partial pages on changes/conflicts. The existing Task 23 orphan-release test now pages through an observer plan, releases through a different owner, proves unchanged observer metadata and rejects its old cursor. No second authorization/CAS revision is introduced. |
| Submitted-overdue recovery | Explicitly gate overdue submitted acceptance/reopen, preserve prior state, and establish activity/version only through a recorded actual ownership confirmation. Task 21's concrete example rejects unswept reopen/accept, sweeps, confirms submitted ownership, then reopens/resubmits/accepts the same attempt. Other holds, tokens, latch and reservations survive confirmation. |

The specification carries the same clarifications. The author reviewed their ordering, capability/replay precedence, guards and test arrangements. Current plan SHA-256 is `deff04477ef2ef85b351ed8e4b1a23e2b02bddcc4f5a91d2558783cc869ee221`: 26 tasks, 15,127 words, 26 Python blocks and 47 named test methods. An explicitly selected Sol low-effort mechanical checker confirmed the stable hash, parsed all examples, inspected fixture/payload/DTO sequencing, checked README plus eight current Markdown documents and `git diff --check`, and confirmed unchanged source HEAD. These checks pass; they do not execute future tests.

The [actual final focused closure](2026-10-06-shared-adaptive-planning-mcp-final-closure.md) reviewed that exact unchanged snapshot and marks H1, the pagination ambiguity and submitted-overdue verification CLOSED. Its verdict is **READY FOR IMPLEMENTATION — plan readiness only**. It identifies no further required fix within those three corrections; C1–C4 retain the preceding report's closed disposition without a duplicate whole-plan audit. The reviewer was fresh, explicitly selected native `gpt-6-astra`, reasoning `high`, standard scoped context, with no descendants or external providers. Separate effective native-model runtime metadata is not exposed.

Status: written plan, author self-review, required reviewer findings/dispositions, final focused fix closure and mechanical checks complete. User review of the written plan and selection of the execution method remain pending. Product implementation and runtime acceptance remain unstarted. Recommend subagent-driven execution for the coupled transactions/concurrency/recovery work; inline remains available with its own workflow and no added per-task reviewers. The final independent review required for implemented changes remains a future Task 26 gate.

All coordinator-owned Copilot/Orca review terminals have been settled and closed, including the aborted control-mismatch attempt. Existing user terminals remain untouched. Future Copilot entries are gated on technical model-control verification; the standing-authorized disclosed native review fallback covers the observed unsupported-control condition. No claim of Copilot allowance exhaustion is made.

The final archival check confirms the reviewed plan hash remains unchanged, README plus all nine Markdown documents have valid relative local links/final newlines/no trailing whitespace, and `git diff --check` passes. The tracked diff remains confined to README; planning/review documents remain untracked. No commit was made.

## Verification boundary

The initial Opus-submitted plan had 26 tasks and 11,933 whitespace-delimited words; SHA-256 `f52671d4bde3d9d8d23e555e2bcbc1f316600d2fb315b832f7c3010d25dd3b99`. A separate explicitly selected `gpt-6.1-sol` low-effort mechanical checker parsed all 26 Python example blocks, checked final newlines/trailing whitespace and relative local links in README and all five documents, checked first-import field/helper/example consistency, and confirmed the source HEAD. These checks and `git diff --check` passed. The tracked diff check does not cover untracked documents; the filesystem checks do. This was mechanical validation, not another reviewer seat. SDK evidence comes from primary documentation and inspection of the pinned public wheel as source text; it is not installed-runtime proof.

No product tests, package builds, dependency installations, container starts, user client configuration changes, commits, or deployment have occurred during this planning stage. Isolated temporary reviewer settings and synthetic control files were created solely for the authorized review. All implementation acceptance checks remain marked not run. Codex five-hour/weekly windows and Google remaining quota are unexposed and remain unknown. Before the fresh closure review, Copilot /usage reported 10,880 of 20,000 AIC used (9,120 remaining); its reset time/window duration was not exposed. Observed reviewer session consumption is tracked separately; shared allowance changes are not attributed to this task, and unknown windows are not evidence of exhaustion. No billing, overflow or reset settings were changed.
