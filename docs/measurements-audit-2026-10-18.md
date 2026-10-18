# Measurement audit and Mac rollout

The existing Mac MCP service now retains optional measurements in the same container volume. The adaptive lifecycle, canonical schema 2, existing tool envelopes, acceptance rules, credentials and SKILL.md are unchanged.

## Audit evidence

The read-only snapshot at `2026-10-17T23:23:35.125719Z` contained 5 plans, 290 store events and 19 checkpoints. Of those events, 289 belong to plans; the remaining event is the earlier `schema_migrated` store event. All 19 checkpoints contain reassessments. There are 13 passing and 6 blocked checkpoint observations, 9 review records and 13 accepted step results. These counts overlap and are not independent bug-fix samples.

Only one plan declares workflow names. The canonical schema has no normalized historical model usage, client skill fingerprints, billed costs, measured human effort or causal rework coverage. The sampled container logs contain session termination messages without usable resource-cost measurements. Free-form evidence is retained; it is not silently converted into measured cost or verified causal attribution.

Inspection of the objectives, creator records, linked project documents and checkpoint limitations identified 2 operational work plans and 3 acceptance fixtures. Five new context observations record these classifications and their audit evidence. A sixth context observation belongs to rollout verification on a pre-existing persistence fixture. These are metadata annotations in the measurement ledger. No historical canonical event was changed. No historical model, skill version, cost, regression or independent-outcome attestation was fabricated.

The operational plans contain 19 steps, including 11 canonically accepted results at this snapshot. Existing acceptance and reviews remain valid. There are no outcome attestations under the new measurement contract yet; this is not a claim that the earlier work was never checked. The report distinguishes canonical acceptance from the new evidence-linked independent-verification attestation.

## Added collection

| Measurement | Existing evidence reused | Added observation |
| --- | --- | --- |
| Outcomes | Current step, attempt, result, acceptance and passing checkpoint | Independent-verifier attestation, explicit failed/blocked/unresolved outcomes |
| Regressions | Linked plan/step/attempt/checkpoint scope | Explicit caught/escaped defect, cause, evidence and resolution |
| Corrective rework | Existing work attribution | Unique delta usage marked corrective and linked to a recorded defect |
| Time | Plan creation and accepted result timestamps | Measured active/wait/blocked intervals; concurrent intervals use their union |
| Resources | Server-observed MCP call name, duration and result category | Client token/call receipts, worker/human/reviewer effort, actual and estimated currency buckets |

The server adds its Python-source fingerprint and runtime versions automatically. Actual client workflow, skill, model, provider and effort versions are reported only when known. Unknown is null; partial sums remain lower bounds. Complete coverage is an explicit client attestation, not a server inference. Estimates, billing allocation and different currencies remain separate. Corrective rework is included in total usage.

New tools are `record_measurement` and `read_measurements`. The same server serves `/measurements` and `/api/measurements`. The append-only ledger is `/data/measurements.sqlite`, schema 1, mode `0600`, owned by `10001:10001`. Canonical `planning.sqlite` remains schema 2. The ledger has no authority to start, accept, reopen, cancel or revise planning work.

Collection instructions are in [references/measurements.md](../references/measurements.md). Existing clients can continue their current workflow. Automatic server call traces start immediately. Tokens, human effort, defects and client versions require clients to submit the documented observations. The service cannot observe those facts inside an MCP client. A monthly report without those receipts will accurately show missing coverage.

## Verification

- Final Linux suite: 869 tests, zero failures or errors, 342.126 seconds. Three platform-specific skips passed separately on Windows: two Node dashboard cases and the actual Windows share-denying-handle case. The real Docker Compose packaging contract passed separately on the Mac host because the runtime container has no Docker CLI.
- Candidate image: all 17 focused measurement and service cases passed as UID 10001, with Python 3.13.16 and SQLite 3.53.4. No production volume was mounted in test containers.
- Decisive checks cover duplicate/conflicting IDs, true zero versus unknown, partial coverage, causal rework, estimates, interval overlap, references/capabilities, canonical invariance, strict SDK registration, HTML escaping and persistence.
- A fresh independent Argon High review identified an outcome-selection defect. A delayed failure from an older attempt could displace current owner verification. The regression was observed failing, then fixed by current attempt/result binding, owner provenance and observation-time ordering. Worker and old-attempt receipts remain visible in the trace.
- The final fresh cross-family review returned `Ready to merge? Yes`, with no Critical, Important or Minor findings. Live rollout and client attestations remained coordinator verification responsibilities, not reviewer claims.
- A disposable volume proved candidate-image seed, old-image admission, then candidate-image read with the same epoch, plan and measurement. The old image ignored the measurement file. The owned volume was removed after all test containers exited.
- Live MCP: 31 tools. The original 29-tool schema, annotation, default and description catalog retains SHA-256 `8a6f4dbc21d35917326cea812670d52804fc617dcbe3df10bd4dcfd60f90ce9f`. The existing Windows MCP connection also read the same service identity after rollout.
- Live exact retry replayed once without a duplicate measurement. The canonical event sequence stayed 293 across collection checks. Missing token usage returned null. The report correctly showed 2 operational plans and 3 fixtures, and retained automatic call traces without arguments or credentials.
- Browser: the live report loaded in the in-app browser with five plan sections, the correct cohort counts, observation and MCP traces, and zero captured console errors.

The private raw snapshot, test logs, reviewer reports and rollout receipts are retained under ignored `work/measurements-audit/`. Snapshot SHA-256 is `962f0d7d70d488a35c3157b372b2b6c3b036eecc1c741f364fcc9ee1cafeafd5`.

## Deployment and rollback

Runtime code commit is `021461e5a1b799f8737831f82001f6cf102968cd`, based on deployed `50df3f53a56e80515d48694a1400b84dff6419df`. The branch is `alex/kpi-measurements` in Windows and Mac repositories. The active Mac checkout remains on main; its unrelated files were not changed.

| Runtime item | Verified value |
| --- | --- |
| Compose project/service | `adaptive-planning-live` / `adaptive-planning` |
| Container | `75eebe1f39a0805b227ea00e4416fd1e3adb1d260d9e8577035113e8a6a95cbb` |
| Image | `sha256:4bede02f5d05b87aff532a051c763b75029d9136e41ea9495f9b312596d49a91` |
| Python-source fingerprint | `41108f5bae9df5f95fa3e5b96d7906804947e39b9b659e8cad3f56b0b8f37112` |
| Volume | `adaptive-planning-data`, mounted at `/data` |
| Runtime user/restart | `10001:10001` / `unless-stopped` |
| Network | Original loopback and RFC1918 LAN bindings on port 8790 |
| Canonical identity | Existing epoch and host ID preserved; no store hold |

A registry fetch for an optional test image failed because the noninteractive Mac session could not access the locked Keychain. That fetch stopped. No credential, permission or security setting was changed. The production candidate was built locally from the already installed runtime image, copying the reviewed Python files into both the source and installed package locations. The pinned product Dockerfile, SQLite library and dependencies are unchanged. The source fingerprint matches the reviewed repository bytes.

The existing Compose project was updated with `up -d --no-build --no-deps --pull never adaptive-planning`, using its original Compose files and environment file. Compose emitted the existing volume-owner-label mismatch warning, then reused the exact named volume. Verification confirmed its data and configuration. No volume was deleted or reinitialized.

The prior image remains tagged `adaptive-planning:rollback-before-measurements-021461e`, ID `sha256:352e9e8301790cae248c749fa984b9dadf24b32191a512296a3f54a52f3d0f40`. To roll back, retag that image as `adaptive-planning:local` and repeat the same original Compose update command. Keep the volume. The old server ignores the retained measurement file. Existing canonical backup/restore commands do not include this separate ledger; no synchronization backups or security changes were made in this task.

Immediately before rollout, the canonical cutoff was 293. After rollout, the complete event prefix through 293 matched its prior hash; epoch, host identity, canonical schema, plan count and credential hash were preserved. Other clients had legitimately added three events since the initial audit at sequence 290. Those events were preserved as well.

Language check: STE Issue 9-oriented candidate. Full dictionary conformance is not claimed.
