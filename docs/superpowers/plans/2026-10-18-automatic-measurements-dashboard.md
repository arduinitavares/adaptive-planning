# Automatic measurements and dashboard implementation

Base: ff09d7fd46a2d9a1d72bf6f382b53678cba3799a. Worktree: alex/kpi-measurements.
Spec: ../specs/2026-10-18-automatic-measurements-dashboard.md.

- [x] Inspect current ledger, dashboard and canonical checkpoint/review/acceptance records.
- [x] Complete independent Argon High plan review; resolve material findings.
- [x] Add tested read-only lifecycle derivation and a remote-readable receipt contract.
- [x] Build a readable, responsive measurements dashboard with evidence and coverage.
- [x] Verify Codex/AGY native usage interfaces; add minimal skill instructions and a supporting receipt guide.
- [x] Verify focused and full tests, desktop/mobile UI and fresh independent change review.
- [x] Roll out to the same Mac service; verify preserved store and update installed skill files on both hosts.

Ruling: derive mechanical lifecycle metrics from persisted canonical records at read time. This avoids a second write after commit, repairs historical coverage naturally, and leaves authorization/replay untouched. Semantic receipts remain explicit observations.

Ruling: distinguish canonical acceptance, exact linked independent review, and client verification attestations. They answer different questions and must not be relabelled as one universal success count.

Ruling: client-native usage hooks are conditional on verified support and exact attribution. Missing usage remains unknown; account-wide allowance changes cannot become task cost. Skill-driven receipt submission is instruction-based collection, not a guaranteed client hook.

Ruling: preserve the existing planning dashboard and CSP. The measurements surface gets dedicated assets and a separate renderer, with no external fonts/scripts or synthetic charts.

## Evidence ledger

Initial user authorization includes same-service rollout and Codex/AGY on Windows and Mac. Native collaboration tools are excluded by user policy; delegated audit and independent review use owned Orca terminals with exact model settings.

Argon High plan review: initial Ready No, five gaps. Dispositions are appended to the spec: explicit visible unclassified cohort, precise linked-review semantics without family inference, existing immutable receipt-key contract rather than changing canonical commands, automatic canonical volume alongside partial usage, and calendar-only duration. Rejected suggestions: guessing fixture class from names; calling intra-attempt spacing active time; weakening original command schemas to piggyback unrelated writes. These would introduce unsupported facts or coupled transaction failure modes.

Whole-change review: first attempt interrupted by Orca shutdown, not accepted. Fresh Argon High review completed with one Important and two Minor findings. One fix pass reproduced all three in RED tests and corrected partial-zero rework, plan-only cohort attribution and dangling optional review pointers. Fresh Argon High fix review returned Ready to deploy Yes, no unresolved Critical or Important findings. Optional installer rerun hardening was recorded as non-blocking: first installation checks verified baseline hashes, and an already-patched file is preserved rather than overwritten.

Verification: 891-test initial Linux suite green; its three platform skips passed Windows. Final installed nonroot image passed 42 focused tests. Wheel and sdist asset checks and actual Mac Compose contract passed. Docker asset omission was reproduced in the installed image and fixed with one explicit allowlist entry. Final full suite and live browser/store evidence remain pending at this checkpoint.

Ruling: preserve the distinct installed Windows and Mac planning baselines and add only the receipt section plus metadata. Record the actual loaded SKILL.md content hash; a shared edition label cannot identify these differing instructions. No client hooks or permission settings are changed.

Final acceptance: 894-test Linux suite passed (three platform skips passed Windows). Final installed image passed 42 focused checks. Independent fix review Ready Yes. Live SDK smoke verified 31 tools, unchanged original29 catalog hash, remote receipt contract, exact context retry and unchanged canonical sequence297. Epoch, host, schema2, hold, existing297-event prefix, token fingerprint, volume, UID and both port bindings matched before/after rollout. Python3.13.16 and SQLite3.53.4 retained. Browser checks passed at1440x1000 and390x844, with no page overflow or console errors; fixture/unclassified filters and keyboard details worked. Screenshots and detailed receipts remain in ignored task artifacts.

Installed receipt additions preserve both prior planning baselines and create zero synchronization backups. Windows skill hash e1ab7573e15a422e4b5ae3eef107b163a7a367f311f15b73b8742816f3360da2; Mac/repository skill hash d5d53ffaece909c654f62ee8c478b889f244584cde8fd18fd5290c3cb8fd3664. Both use reference hash0b744a783f9c030b09c850e19373ec5736ddcd2b8eca286f52b95be481b76420. Original checkout and user SKILL.ultracave.md preserved. All owned review workers settled and terminals closed.
