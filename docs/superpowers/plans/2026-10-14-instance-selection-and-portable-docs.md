# Instance selection and portable documentation implementation plan

> Historical pre-implementation record. The authorization boundary and test recipe below describe the plan-review phase. Later execution results belong in the private task report.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans after implementation is authorized. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the skill and shared documentation independent of a particular computer, network address, or deployment.

**Architecture:** Each plan uses one selected planning service instance. Independent instances keep separate saved state and credentials. Clients discover service and plan URLs through the selected MCP connection. Personal deployment records stay outside shared operating instructions.

**Tech Stack:** Existing Python 3.13 service, MCP, SQLite, Docker Compose, Markdown skills, and unittest fixtures. This change adds no protocol or database schema.

**Spec:** The agreed brief below and [the existing shared-service design](../specs/2026-10-06-shared-adaptive-planning-mcp-design.md). Where that historical design describes a particular host, the portability brief governs new instructions. Existing safety and recovery requirements remain in force.

**Status:** Plan only. Implementation has not started. The user requested a written plan and an independent Argon review. Saving and reviewing this plan does not authorize implementation, commits, deployment changes, global configuration edits, or memory writes.

## Agreed brief

The skill must support a local service, a remote service, or several deliberately configured independent services. One service can run on a laptop while another runs on a different computer. No host pair or personal address is required.

For an existing plan, retain its service binding. For a new plan, use an explicit task selection first. Otherwise use an applicable private project preference, then a private user preference. If only one service is configured, use it. If several remain eligible without a selection, ask before creating a plan. If none is available, report the configuration gap.

Read `read_service_info` through that selected MCP connection. Use `InstallationView.dashboard_url` and the returned plan URLs. Retain the configured MCP entry, `host_id`, last observed `epoch`, and `plan_id` in private task/session working records. An endpoint string, `host_id`, or dashboard URL alone does not establish service identity. Within trusted configured connections, compare `(host_id, epoch)` for aliases. Read the expected plan before resuming it. These metadata checks do not provide cryptographic proof against an unauthorized copied database.

A changed epoch requires the existing restore/recovery checks. It does not authorize an automatic move to another instance. An explicit request to move an existing plan needs a separate migration decision. This work does not add migration or replication.

Clients of the same instance share that instance's state. Independent instances do not automatically share plans, grants, events, or resource reservations. Coordination of a resource used through different instances remains an operator responsibility.

The outage rule prohibits creating a competing authority for an existing plan. It does not prohibit independent installations selected deliberately for different plans. A named bounded worker can finish its existing local verification during an outage. New assignments and dependents still wait.

Actual endpoints and credentials belong in private client/deployment configuration. Shared skill or `AGENTS.md` guidance states the selection rule. Private user instructions or memory can record a preferred configured entry for new plans. They do not override an existing plan binding. This change does not write those private settings or memories.

Keep STE as the default for authored planning and reporting text. Caveman Ultra summaries are a separate future decision. This plan does not change the accepted STE policy.

## Baseline and boundaries

- Inspected local HEAD: `daf6c94d09843553eb420c46e396cc050c898f9b`.
- Inspected `origin/main`: `281c882f8fc41f984198b17f63bf5b44008fb5c4`.
- Existing LAN change: `d2ad6a073eeac919fd270b0176df8c5cbc715ff5` on `origin/arduinitavares/lan-access`.
- The LAN change already supplies optional RFC1918 IPv4 configuration, advertised URLs, exact HTTP boundary checks, and `tests/test_service_lan.py`.
- The local STE section is preserved in the current merge. Reuse it during integration.
- HEAD `daf6c94` does not include `d2ad6a0`. It merges the older main release with the local STE policy. Recheck that relationship before execution.
- The installed skill copy is older. Updating a checkout does not update that installation.
- `/work/`, `/outputs/`, `.env`, and `.env.*` are currently ignored. The LAN change explicitly makes `.env.example` trackable.
- Existing client guides link to dated activation sections in `docs/operations.md`. Preserve or redirect these links during cleanup.

## Global constraints

- Preserve the STE policy, exact machine values, accepted plan history, and existing evidence.
- Preserve bearer authentication, exact Host/Origin checks, forwarding-header rejection, capability fences, and immutable same-key replay.
- Keep default deployment loopback-only. LAN exposure remains explicit operator configuration.
- Do not extend the admitted URL formats, address ranges, protocols, or security boundary in this work.
- Retain `requires-python = ">=3.13"`, the frozen dependency lock, and the existing image runtime qualification: Python `3.13.16` and SQLite `3.53.4`.
- Keep SQLite and installation tokens private. Tests use isolated synthetic data only.
- Separate instances on different Docker hosts can use the same default port. Same-host instances require distinct published ports and data volumes. Changing the Compose project name alone does not isolate the explicitly named default volume.
- Do not add an instance registry, automatic discovery scan, server chooser UI, new public metadata fields, or automatic fallback.
- Do not operate on a real planning database, credential, deployment, or active plan during implementation tests.
- Keep historical source/design records intact unless the reviewed cleanup inventory identifies an exact personal-detail edit. Preserve original records privately before any removal.
- Do not update an installed skill, provider configuration, permissions, or memory as part of this implementation. Those are separate setup actions.
- Planning language is an Issue 9-oriented candidate. The official dictionary and governed terminology are unavailable. Do not claim full STE conformance.

## Review focus

1. Two independent services must not be presented as one shared store. Task 3 checks state separation and foreign-epoch rejection.
2. Loopback and LAN aliases of one service must advertise the same authority and plan links. Task 3 runs the existing real HTTP/SDK LAN test.
3. Several configured services must not cause silent selection or a move of an existing plan. Task 2 reviews an explicit selection/resume matrix.
4. An outage or normal epoch change must not create a second canonical authority. Task 2 preserves the recovery sequence and bounded-worker rule.
5. Moving personal receipts must not erase evidence, overstate old verification, or break public links. Task 4 verifies private copies, bounded public summaries, and links.

## File responsibilities

| Files | Responsibility |
| --- | --- |
| `adaptive_planning/config.py`, `adaptive_planning/app.py`, `compose.lan.yaml`, `.env.example`, `.gitignore` | Reuse the five runtime/deployment files from the existing reviewed LAN change. |
| `SKILL.md`, `references/live-dashboard.md`, `README.md` | Define portable selection, discovery, resume, and outage instructions while retaining STE. |
| `docs/clients/codex.md`, `docs/clients/antigravity.md`, `docs/clients/copilot.md` | Explain private configured endpoints and distinct named connections with synthetic credentials. |
| `tests/test_service_instructions.py`, `tests/test_service_lan.py`, new `tests/test_service_instances.py` | Preserve required instruction checks and verify actual service identity/state behavior. |
| `docs/operations.md`, new `docs/release-verification.md` | Keep portable procedures and bounded, sanitized verification summaries. |
| `work/deployment-records/<record-id>/` | Retain private originals, hashes, extraction inventory, and cleanup receipt. These files are not committed. |

## Task 1: Reuse the LAN source change

**Files:** The existing LAN change's 11 files, listed by `git show --stat d2ad6a0`: five runtime/deployment files, `tests/test_service_lan.py`, and five documentation files. Those documents are `README.md`, `SKILL.md`, `docs/operations.md`, `docs/clients/codex.md`, and `docs/clients/antigravity.md`. Later tasks also update the dashboard reference and Copilot guide. No additional runtime source files.

**Interfaces:** Consume the current checkout and existing LAN commit. Produce the existing `Config` LAN fields, `ADAPTIVE_PLANNING_LAN_IP` deployment input, and advertised `InstallationView.dashboard_url`. Do not change the MCP API or `InstallationView` fields.

- [ ] Record the execution checkout, HEAD, dirty paths, and the relationship between the current main branch and the LAN commit.
- [ ] If the LAN change is already included upstream, use that integrated source. Otherwise incorporate the reviewed `d2ad6a0` change in the task's isolated implementation checkout. Do not switch this chat's checkout or overwrite unrelated changes.
- [ ] Resolve documentation overlap by retaining the accepted STE section and the LAN discovery mechanism. Do not restore Caveman compression or unconditional localhost reporting.
- [ ] Run the existing LAN boundary tests in an isolated Linux environment with the qualified Python/SQLite runtime.

Run: `uv run --frozen python -B -m unittest tests.test_service_lan -v`.

Use this direct command only inside an already qualified isolated Linux checkout. Otherwise use the disposable container procedure below. Do not run these POSIX Store tests directly on an unqualified Windows host.

Expected: all LAN tests pass, including default LAN denial, exact allowed origins, bearer checks, forwarding-header rejection, and advertised URL behavior. Record unavailable runtime/platform checks as gaps.

**Done when:** the LAN mechanism is present once, its tests pass, and the local STE policy is intact. Record the resulting source revision. This step does not update a live container.

## Task 2: Define instance-aware skill and client instructions

**Files:** Modify `SKILL.md`, `references/live-dashboard.md`, `README.md`, all three `docs/clients/*.md` guides, and the existing documentation assertions in `tests/test_service_instructions.py`.

**Interfaces:** Consume `read_service_info() -> InstallationView` with `host_id`, `epoch`, and `dashboard_url`, plus existing returned plan URLs. Produce a documented selection/resume contract. Private working-record fields are guidance, not new public schema fields or an implemented client registry.

- [ ] Update the existing test assertion that treats a literal localhost dashboard sentence as unconditional authority. Require the documented `read_service_info`/`dashboard_url` mechanism. Keep the other checkpoint, recovery, and authorization assertions.
- [ ] Run the focused instruction test before editing the prose. Confirm that the missing instance-selection contract is detected. Do not fabricate a failure if a newer baseline already contains it.
- [ ] Write the selection and resume rules from the agreed brief in the skill and dashboard reference.
- [ ] Replace the LAN delta's sentence `All clients share the same canonical saved state.` with instance-scoped wording. Remove its `Local installation default` workaround paragraph. Keep localhost as a clearly labelled example, without preserving an obsolete assertion through duplicated boilerplate.
- [ ] Explain that loopback URLs are local examples. Show two separately named configured connections in the client guides using synthetic values. Preserve each client's current configuration schema and credential-provisioning limits.
- [ ] State that deployment configuration owns addresses. Provide a generic optional instruction for choosing a preferred configured entry. Do not place a personal address in shared `AGENTS.md` or create/edit private settings.
- [ ] Review the matrix below against each guide. Correct contradictions and ambiguous defaults.

| Case | Required instruction |
| --- | --- |
| One configured local service | Select it and use its advertised URLs. |
| One configured remote service | Select it and use its advertised URLs. |
| Several services, explicit new-task selection | Use the selected configured entry. |
| Several services, applicable private project preference | Select that entry for a new plan. It takes precedence over a private user preference. |
| Several services, private user preference only | Select that entry for a new plan. |
| Selected or preferred entry is missing or unavailable | Report the gap. Ask before choosing another instance for a new plan. Do not silently fall back. |
| Several services, no applicable selection | Ask before creating a plan. |
| No configured service available | Report the configuration gap. Do not start a per-task fallback server. |
| Existing plan, another preferred service | Resume the original binding. A move requires an explicit migration decision. |
| Two endpoint aliases of one service | Compare `(host_id, epoch)` through the trusted connections. Read the expected `plan_id` when resuming. Do not create duplicate canonical plans. |
| Service unavailable | Retain last-known state. An assigned bounded worker may finish local verification and retain unpublished evidence. New assignments and dependents wait. Do not create a competing authority for that plan. Deliberately configured independent instances remain available for other plans. |
| Epoch changes on the bound service, same `host_id` | Reread identity, ownership, holds, and events. Follow existing restore/recovery rules before publishing. |
| Bound endpoint returns an unexpected `host_id` | Stop plan mutations. Report the identity mismatch and obtain an explicit recovery or migration decision. |

Run: `uv run --frozen python -B -m unittest tests.test_service_instructions -v`.

**Done when:** required tests pass and a human review confirms every matrix row. Substring assertions do not establish the meaning of natural-language instructions. Keep that semantic review explicit.

## Task 3: Verify independent service state

**Files:** Create `tests/test_service_instances.py`. Reuse `tests/service_fixtures.py` and existing LAN/HTTP helpers without changing production APIs.

**Interfaces:** Use `make_service(directory: Path | None = None, clock: FakeClock | None = None) -> ServiceFixture`, `ServiceFixture.create`, `ServiceFixture.command`, and each fixture's `service`. Fixture cleanup must close every Store and remove only owned temporary data.

Add one test-local helper, `_build_instance_app(directory: Path, token: str, base_url: str) -> Starlette`, in `tests/test_service_instances.py`. Follow `TestLan.real_app` without changing that existing helper: create an owned mode-`0700` directory, write its `service-token` once with `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)`, then call `build_app(Config(directory, base_url=base_url))`. Register `app.state.close` before scheduling directory cleanup. Use distinct synthetic token constants and separate directories. Never read a real installation token.

- [ ] Add `test_independent_instances_keep_distinct_epochs_and_plan_state`. Create two independent temporary fixtures. Create a plan on A. Assert different epochs, the plan is present on A, and B has no plans.
- [ ] Add `test_foreign_instance_command_is_rejected_without_mutation`. Build a valid A command and prove it succeeds on A. Submit that exact command to B. Assert `DomainError.code == "stale_epoch"` and unchanged B plans/events.
- [ ] Add `test_foreign_plan_and_grant_are_rejected_after_epoch_check`. First read A's plan ID through B and assert `not_found`. Then use B's valid current epoch and B's own plan ID with A's orchestrator token in an otherwise valid mutation. Assert `forbidden`. Compare B's plan, event sequence, and request records before and after rejection. This must exercise checks beyond the epoch fence.
- [ ] Add `test_resource_reservations_are_scoped_to_each_instance`. Register the same synthetic external `ResourceClaim` in A and B. Assign a step that holds that resource on A. Confirm B has no corresponding use or hold and can assign its own step for its separately registered resource. Use `ServiceFixture.register`, `assign`, and the existing resource fixtures. This test demonstrates the absence of cross-instance coordination; it does not authorize concurrent real-resource use.
- [ ] Add `test_instance_credentials_are_not_interchangeable`. Build two real isolated apps with distinct synthetic installation tokens. Verify authenticated MCP access to each own instance and HTTP 401 with the other instance's token.
- [ ] Run `TestLan.test_real_http_sdk_initializes_discovers_tools_and_reads_same_lan_advertised_service`. It checks real MCP initialization, discovery, service identity, dashboard links, and plan links over both aliases of one instance.
- [ ] In the independent-fixture test, retain the existing equal `synthetic-host` IDs and assert distinct epochs. In the alias test, require equal `(host_id, epoch)` and the same advertised plan URLs. These cases reject identity rules based only on host ID or URL.
- [ ] Run the focused suite. These state-isolation tests can pass before editorial changes because they qualify existing behavior. Do not alter production code to force a red/green demonstration.

Run: `uv run --frozen python -B -m unittest tests.test_service_instances tests.test_service_lan tests.test_service_instructions -v`.

**Done when:** actual temporary services prove isolation and alias consistency, all Stores are closed, and no test touches production state. If existing behavior fails, stop and report the runtime defect as a separate design decision.

## Task 4: Separate portable guidance from personal evidence

**Files:** Modify `docs/operations.md` and the client guides. Create `docs/release-verification.md`. Create ignored originals and an extraction manifest under `work/deployment-records/<record-id>/`.

**Interfaces:** Consume existing procedure sections, dated verification sections, and incoming Markdown links. Produce portable procedures, a bounded public verification summary, compatible links, and privately preserved exact originals.

- [ ] Inventory personal hostnames, live deployment claims, absolute user paths, local artifact locations, plan IDs, and persistent-client activation records in the operations and client guides.
- [ ] Include the dated `Current closure evidence, 2026-10-13`, `Local activation evidence, 2026-10-13`, and `Native client acceptance completed, 2026-10-13` sections. Inspect dated records in the Codex, Antigravity, and Copilot guides. Extend the extraction inventory only where an additional exact personal-detail block is identified.
- [ ] Distinguish those records from generic examples such as `/data`, explicit synthetic values, supported-platform facts, rollback procedures, and qualified test limits.
- [ ] Copy the exact affected source files and extracted record blocks into the ignored private directory. Record source revisions, original headings, hashes, and intended public replacements. Verify the copies before editing.
- [ ] Move personal activation records out of shared operating instructions. Do not ignore the whole operations guide. `.gitignore` does not hide paragraphs inside a tracked document.
- [ ] Explain independent deployments in `README.md` and `docs/operations.md`. Separate hosts may use the default port. Same-host instances need distinct published ports and data volumes. State that the explicit `adaptive-planning-data` volume name is not isolated by a different Compose project name. Do not add a new deployment abstraction.
- [ ] Keep the technically useful results in `docs/release-verification.md`: dated source/runtime qualifications, actual client surfaces, exact coverage limits, and unverified platforms. Remove personal addresses, usernames, private absolute paths, and claims that a reader's installation is already activated.
- [ ] Replace the old activation sections with stable anchor stubs linking to the public summary. Update client-guide links and remove obsolete preparation/pending labels from normative setup instructions. Retain those original historical labels in the private record.
- [ ] Review shared normative files for unconditional instance URLs and personal deployment data. Explicit local/synthetic examples and supported-platform distinctions remain allowed.
- [ ] Verify every changed local Markdown link and explicit anchor. Verify private files remain ignored and are absent from the staged/public diff.
- [ ] Preserve the rollback text boundaries used by `tests/test_service_instructions.py`: `### Deployment update and rollback`, `## Build inputs and platform verification`, `To update the service deployment image:`, `Rollback using the retained verified image:`, `Rollback from a verified source checkout:`, and `After either rollback path:`. Run that test module again after cleanup.

Run: `git check-ignore -- work/deployment-records/<record-id>/manifest.json`.

Expected: Git reports the record path as ignored. Also inspect `git status --short` and the explicit staged path list. Passing `check-ignore` alone does not prove that an already tracked file was removed from the index.

Run: `uv run --frozen python -B -m unittest tests.test_service_instructions -v` in the qualified test environment. Expected: rollback-section parsing and instruction assertions pass. Scan normative shared documents against the private extraction manifest's personal-value list. Expected: no real hostnames, usernames, private absolute paths, or installation addresses remain. Do not commit that personal-value list.

**Done when:** exact originals remain recoverable, public instructions contain no personal deployment requirements, public verification claims retain their original limits, and no referenced anchor is broken.

## Isolated Linux test procedure

Record the Docker context, daemon host, and target architecture first. Use an execution-host-owned Docker context with a valid source bind path. If that context or the required runtime is unavailable, report a qualification gap. Do not install Docker, switch to a production remote context, or use a real planning volume to fill the gap without separate authorization.

The current Dockerfile already installs the frozen environment at `/app/.venv` and qualifies Python `3.13.16` with SQLite `3.53.4`. Build a task-owned image tag. Record the resulting immutable image ID. This PowerShell example resolves the implementation checkout dynamically:

```powershell
$testRoot = (git rev-parse --show-toplevel).Trim()
$testRunId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$testTag = "adaptive-planning-portability-test:$testRunId"
$testRecord = Join-Path $testRoot "work/portability-tests/$testRunId"
New-Item -ItemType Directory -Path $testRecord -ErrorAction Stop
$imageRecord = Join-Path $testRecord 'image-id.txt'
docker build --iidfile $imageRecord --tag $testTag $testRoot
if ($LASTEXITCODE -ne 0) { throw 'Test image build failed.' }
$testImage = (Get-Content -LiteralPath $imageRecord -Raw).Trim()
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m --mount "type=bind,source=$testRoot,target=/workspace,readonly" --workdir /workspace --entrypoint /app/.venv/bin/python $testImage -B -m unittest tests.test_service_instances tests.test_service_lan tests.test_service_instructions -v
if ($LASTEXITCODE -ne 0) { throw 'Focused test suite failed.' }
```

For Task 1, run only `tests.test_service_lan` through the same container command. For Task 2 or Task 4, run only `tests.test_service_instructions`. No host port is published. No named volume, home directory, credential, or Docker socket is mounted. The checkout is read-only and temporary test state stays under `/tmp`. Each fixture closes its Stores. The test run must not start the image's normal `serve` entrypoint.

The container's loopback interface remains available with `--network none`. Existing ASGI transport tests supply synthetic LAN Host/Origin values; they do not claim actual cross-machine connectivity. Record that limitation. Cleanup removes only the task-owned test image/tag after its containers exit. Never prune shared images or volumes.

Docker references: [read-only bind mounts](https://docs.docker.com/engine/storage/bind-mounts/), [tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/), [none networking](https://docs.docker.com/engine/network/drivers/none/), and [container run options](https://docs.docker.com/reference/cli/docker/container/run/).

## Task 5: Verify and review the complete change

- [ ] Run the focused suites from Tasks 1–3 in the isolated qualified Linux environment.
- [ ] Resolve `skill-creator/scripts/quick_validate.py` through the execution host's skill catalog. Validate the source skill with that exact script path. If it is unavailable, check the YAML frontmatter and required skill structure directly and record the missing validator as a tool gap. Do not search unrelated directories or install a tool silently.
- [ ] Review STE text as an Issue 9-oriented candidate. Check exact literals, technical meaning, conditions, and uncertainty. Do not rewrite historical plan evidence for style.
- [ ] Inspect the complete diff for credentials, personal deployment data, unrelated edits, and accidental changes to the accepted STE policy.
- [ ] Obtain a fresh cross-family review of the final implementation under the execution-host model policy. Resolve material findings before acceptance.
- [ ] If implementation and commits are explicitly authorized, stage only reviewed source/document/test files. Keep private records and real configuration outside the commit. Do not push or deploy without the required task authorization.
- [ ] Report the source revision, test results, review disposition, private-record preservation, and remaining platform/setup gaps.

**Rollback:** Before each edit, retain the original files and source revision. Revert only task-owned source changes. Keep the private record copies. Do not reset unrelated work, delete volumes, or change the live database. No image or database rollback is required because this task does not deploy.

## Acceptance and stop point

The implementation is acceptable when the selection matrix is unambiguous, advertised links work for the selected service, independent instances remain separate, STE is retained, and private deployment records are absent from shared guidance. Runtime assertions and independent review must support those claims.

This planning request ends after saving the plan, receiving Argon's read-only review, recording findings and their disposition, and reporting the result. No implementation, merge, commit, push, deployment, installed-skill synchronization, provider configuration change, or memory update is authorized by that result.

## Review ledger

| Round | Reviewer | Verdict | Coordinator disposition |
| --- | --- | --- | --- |
| 1 | `gemini-4-argon-high`, effort `high`, fresh ordinary AGY session through Orca | With fixes; no Critical findings, three Important findings, two Minor findings | Expanded selection and identity rules, added post-epoch isolation checks and the explicit HTTP fixture helper, protected cleanup targets/anchors, aligned source files, and specified isolated Linux test execution. |
| 2 | `gemini-4-argon-high`, effort `high`, fresh ordinary AGY session through Orca | Yes; no Critical or Important findings, one non-blocking Minor finding | All material findings were resolved. The explicit image-cleanup/ignored-test-receipt note is recorded in the companion review. The reviewed substantive plan remains unchanged. Implementation is not authorized. |

Review budget: one initial review and at most two bounded correction reviews. Stop with an unresolved finding if the same material issue survives that budget. Reviews do not authorize implementation.
