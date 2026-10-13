# Shared Adaptive Planning MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one durable local MCP service that records adaptive plans, bounded concurrent work, evidence, review, and explicit recovery across Codex, Antigravity, and local Copilot.

**Architecture:** Extract the legacy publisher's pure domain rules into a standard-library core, retaining its CLI and atomic-file behavior. A SQLite authority stores semantic operations, append-only events, materialized snapshots, capabilities, and reservations in one transaction; the official MCP SDK and a read-only dashboard expose that authority. The orchestrator remains in the client and performs all dispatch, host-side verification, permission interpretation, and integration.

**Tech Stack:** Python service >=3.13; Python-standard-library legacy/core >=3.10; `mcp==2.3.0`, `mcp-types==2.3.0`, `starlette==1.7.0`, `uvicorn==0.54.0`, `httpx2==2.13.1`, `pydantic==2.13.5`; `hatchling==1.32.4`; `uv==0.10.12`; SQLite; existing vanilla dashboard assets; Linux container Python 3.13.16.

**Spec:** [Approved design](../specs/2026-10-06-shared-adaptive-planning-mcp-design.md), approved 2026-10-06, source checkout `06a79f7d52554a1660b587013bffdd5a71b12e16`. Read both documents before execution.

## Global Constraints

- This document authorizes no implementation, installation, deployment, client configuration, test execution, or commit. All commands below are future execution instructions. Preserve the existing dirty `README.md` and approved documents.
- One service instance, one SQLite authority, one named data volume; `/mcp`, `/`, `/plans/<planId>`; port `8790` published only to Mac `127.0.0.1`; dashboard URLs contain no credentials.
- Non-root UID `10001`, restart policy `unless-stopped`; no host repositories, home directory, Docker socket, or client credentials mounted. Service executes no project commands and reads no client-supplied host paths.
- All MCP HTTP methods require the installation bearer credential. Dashboard is read-only and unauthenticated under the single-local-user loopback boundary; strict Host/Origin validation applies to every route, with escaped content and restrictive CSP.
- Retain states `planning`, `running`, `stopped`, `blocked`, `complete`; step states `pending`, `in-progress`, `blocked`, `complete`, `retired`; default horizon ten upcoming steps, 200 nodes, 50 evidence items per step, 10 MB per canonical public projection/page, with bounded coordination summary/first page; full authority/history remains durable; never truncate evidence. Private capabilities are separate.
- Preserve `plan-only`, `first-step`, `complete-task`, immutable completed history, evidence prefixes, retirement, CAS, explicit v1 migration, corrupt-state recovery, and the lasting global `scope.claimed` latch. Ordinary changes advance revision; epoch recovery changes UUID generation.
- Domain labels and authorization notes are recorded assertions, not identity proof or new permission. No autonomous scheduling, worker self-selection, model routing, automatic Git integration, remote access, transcript retention, or hidden-reasoning retention.
- SQLite: per-operation connection, `foreign_keys=ON`, `busy_timeout=2000`, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE` for writes; validation, records, events, snapshots, and idempotency commit together. No model/network calls or host commands inside transactions.
- Worker mutations use generation plus semantic attempt/definition versions, not global revision. Capabilities are private mutation results; never public projections/events or logs. Log operation name, request ID, outcome, and duration only, never arguments, bodies, headers, or returned results.
- Resource identity matches physical identity OR registered canonical path; case-fold only host-attested insensitive volumes. Unresolved obligations survive submission, acceptance, stale detection, replacement, and disconnected sessions until explicit safe resolution.
- Checkpoint publication installs material reassessment holds on affected steps and transitive consumers atomically, before review/acceptance. Unbounded effects hold the plan. Acceptance and availability are separate; accepting code does not integrate it.
- Stale inactivity default 15 minutes; instructions request observable activity at least every five minutes when possible. Time/heartbeat alone never release reservations or resurrect stale ownership.
- Exact dependency versions and image digest below were checked from primary sources during planning; compatibility remains unproven until Task 1 and installed-client checks pass. Documented protocol negotiation is allowed; dependency changes or unrecorded fallback are not.

## Review Focus

1. A lost creation/takeover response followed by supersession must return a non-secret committed receipt, never resurrect the obsolete capability (Tasks 4–5).
2. Mac volume/device changes, canonical-path reuse, and sensitive versus insensitive aliases must preserve unresolved reservations rather than manufacture another writable resource (Task 11).
3. Material reassessment of an already active sibling must install holds before the submitting worker's review; the unaffected worker can finish while affected descendants cannot advance (Tasks 15–16).
4. An older backup may omit running host work; restore must hold the entire store and require an explicit inventory of absent work before execution resumes (Task 23).
5. A worker without MCP or with a long uninterruptible tool call must preserve attributed evidence without fabricating liveness, reassigning itself, or launching another canonical server (Tasks 21, 25).

---

## Boundaries, shared contracts, and execution order

New root package: `adaptive_planning/{__init__.py,__main__.py,core.py,types.py,schema.py,store.py,ownership.py,plans.py,resources.py,attempts.py,checkpoints.py,reviews.py,recovery.py,imports.py,transport.py,dashboard.py,config.py,admin.py,app.py}`. `core.py` holds pure graph/scope/publication rules; `types.py` DTOs and error contracts; `schema.py` versioned DDL; `store.py` transactions; remaining domain modules own their named operations. `app.py` composes `PlanningService`; transport and dashboard have no separate state authority. `config.py` validates service configuration; `admin.py` owns explicit local init/backup/restore. `__main__.py` delegates the CLI. Keep `scripts/live_plan.py` standard-library-only, with public aliases and file storage unchanged. Reuse `assets/dashboard/` via wheel force-inclusion, not a copied asset tree.

Tests: `tests/__init__.py`, `tests/service_fixtures.py`, `tests/test_service_{sdk,core,store,ownership,plans,http,dashboard,packaging,resources,attempts,scope,availability,checkpoints,holds,reviews,acceptance,interop,recovery,imports,admin,instructions,acceptance_matrix}.py`. Use `unittest` so existing and new tests run together; import `asyncio`, `copy`, `threading`, `ThreadPoolExecutor` from concurrent.futures, `subprocess`, `sys`, `dataclasses.replace`, `TemporaryDirectory`, `Path`, `datetime/timezone` where examples use them. Each test name below denotes one method in the named module's `TestService` class, except explicitly named existing regressions. All examples are named methods; self.f/self.p/self.plan_id use the Task 2 defined service setUp unless direct earlier-stage setup is shown. No sleeps: injected clock, thread barriers and separate connections for contention, reopened SQLite for restart, fault injection for rollback. Browser/client/container checks are additional acceptance evidence, not substitutes for deterministic tests.

Task 2 defines these shared DTOs in `types.py` using frozen dataclasses/TypedDicts and JSON-safe serialization. `JSONValue` is null/bool/int/finite float/string/list of JSONValue/string-keyed dict of JSONValue. `PlanId`, `StepId`, `AttemptId`, `ResourceId`, `ResultVersion`, `DefinitionVersion`, `Epoch`, `Generation`, and `RequestKey` are string aliases; revision/version counters are integers. `Clock = Callable[[], datetime]` returns timezone-aware UTC.

| DTO | Exact fields / meaning |
| --- | --- |
| `InstallationView` | `epoch: Epoch, schema_version: int, host_id: str, dashboard_url: str, store_hold: bool, restore_cutoff: str | None`; no grants/credentials |
| `Metadata` | `epoch: Epoch, plan_id: PlanId, generation: Generation, revision: int, ownership_version: int, dashboard_url: str` |
| `ArtifactRef` | `kind: Literal['code','document','data','observation'], identity: str, version: str, digest: str | None, summary: str`; essential evidence text accompanies temporary paths |
| `DocumentRef` | `path: str, branch: str | None, commit: str | None, version: str, task_id: StepId | None`; references only, never opened by server |
| `StepDefinition` | `step_id, title: str, dependencies: tuple[StepId,...], completion_check: str, document: DocumentRef | None, definition_version: DefinitionVersion, review_required: bool, final_gate: bool` |
| `Authorization` | `mode: Literal['plan-only','first-step','complete-task'], interval: str, since_revision: int, note: str, baseline: tuple[StepId,...], claimed: StepId | None` |
| `RetirementRequest` | `step_id: StepId, reason: str`; explicit cancellation/retirement, never stopped-use evidence |
| `ReservationView` | `use_id: str, plan_id: PlanId | None, attempt_id: AttemptId | None, absent_work_id: str | None, resource_ids: tuple[ResourceId,...], actor: str, writer_active: bool, unresolved: bool, repository_exclusive: bool` |
| `AuthorizationChange` | `mode: Literal['plan-only','first-step','complete-task'], note: str`; explicit new assertion grounded in user instruction |
| `HostWork` | `work_id: str, actor: str, state: Literal['running','stopped','unknown'], resource_ids: tuple[ResourceId,...], resource_claims: tuple[ResourceClaim,...], evidence: tuple[Evidence,...]` |
| `ResourceResolution` | `use_id: str | None, absent_work_id: str | None, resource_ids: tuple[ResourceId,...], resolution: Literal['stopped','isolated','unresolved'], reason: str`; IDs must match restored uses or declared HostWork |
| `PlanDraft` | `objective: str, acceptance_criteria: tuple[str,...], constraints: tuple[str,...], facts: tuple[str,...], assumptions: tuple[str,...], next_check: str, authorization: Authorization, steps: tuple[StepDefinition,...], documents: tuple[DocumentRef,...], workspace_refs: tuple[ResourceId,...], workflow: dict[str,str] | None, horizon: int=10, stop_reason: str | None, block_reason: str | None` |
| `Evidence` | `summary: str, artifact: ArtifactRef | None, workspace_id: ResourceId | None, check: str, result: Literal['passed','failed','blocked','observation'], limitations: tuple[str,...], blockers: tuple[str,...]` |
| `Reassessment` | `summary: str, facts: tuple[str,...], assumptions: tuple[str,...], disposition: Literal['unchanged','material'], reason: str, affected_steps: tuple[StepId,...], unbounded: bool, proposed_draft: PlanDraft | None` |
| `Checkpoint` | `outcome: Literal['passed','failed','blocked'], check: str, evidence: tuple[Evidence,...], reassessment: Reassessment, origin_worker: str, relay: str | None, artifact: ArtifactRef | None` |
| `ResourceClaim` | `host_id: str, kind: Literal['repository','workspace','external'], device: int | None, inode: int | None, canonical_path: str | None, case_insensitive: bool, repository_id: ResourceId | None, external_identity: str | None` |
| `ExecutionContext` | `kind: Literal['repository','non-repository','external-only','read-only'], repository_id: ResourceId | None, workspace_id: ResourceId | None, repository_exclusive: bool, external_ids: tuple[ResourceId,...], writes: bool, checked_base: str | None` |
| `ExecutionContextClaims` | `kind: Literal['repository','non-repository','external-only','read-only'], repository_claim: ResourceClaim | None, workspace_claim: ResourceClaim | None, repository_exclusive: bool, external_claims: tuple[ResourceClaim,...], writes: bool, checked_base: str | None`; same field order as ExecutionContext with claims replacing IDs |
| `Availability` | `prerequisite: StepId, result_version: ResultVersion, method: Literal['ancestor','merge','cherry-pick','equivalent','observation'], checked_base: str, artifact_digest: str | None, evidence: Evidence` |
| `Review` | `reviewer: str, artifact: ArtifactRef, verdict: Literal['passed','failed'], findings: tuple[str,...], evidence: tuple[Evidence,...], verification_use_id: str | None` |
| `ReviewView` | `plan_id: PlanId, step_id: StepId, attempt_id: AttemptId, definition_version: DefinitionVersion, recorded_sequence: int, review: Review` |
| `AttemptView` | `attempt_id, step_id, assignee: str, state: Literal['assigned','active','submitted','accepted','rejected','released','stale'], prior_state: str | None, version: int, definition_version, last_activity: str, context: ExecutionContext | None, recovery_required: bool, result_version: ResultVersion | None` |
| `CoordinationPage` | `plan_id: PlanId, revision: int, store_sequence: int, items: tuple[CoordinationItem,...], next_cursor: str | None, total_counts: dict[str,int]`; cursor binds epoch/plan/revision/store_sequence |
| `CoordinationItem` | `kind: Literal['attempt','reservation','review','hold','invalidation'], value: AttemptView | ReservationView | ReviewView | HoldView | InvalidationView` |
| `HoldView` | `hold_id: str, kind: Literal['reassessment','invalidation','stale','identity'], step_id: StepId | None, source_id: str, reason: str` |
| `InvalidationView` | `invalidation_id: str, producer_step_id: StepId, result_version: ResultVersion, affected_steps: tuple[StepId,...], resolved_steps: tuple[StepId,...], recorded_sequence: int` |
| `CoordinationView` | `attempts: tuple[AttemptView,...], reservations: tuple[ReservationView,...], holds: tuple[HoldView,...], reviews: tuple[ReviewView,...], result_versions: dict[StepId,ResultVersion], store_hold: bool, restore_cutoff: str | None, ownership_actor: str, ownership_version: int, store_sequence: int, next_cursor: str | None, total_counts: dict[str,int], has_live_attempts: bool, has_unresolved_uses: bool, has_plan_holds: bool` |
| `PlanView` | `plan_id: PlanId, metadata: Metadata, draft: PlanDraft, legacy_snapshot: dict[str,JSONValue], legacy_publication: dict[str,JSONValue] | None, coordination: CoordinationView, status: str, published_at: str`; `legacy_snapshot` is renderer-compatible v2-shaped graph, not necessarily an importable legacy publication; `legacy_publication` exists only when strict legacy validation passes |
| `Event` | `sequence: int, plan_revision: int | None, plan_id: PlanId | None, kind: str, actor: str, origin_worker: str | None, relay: str | None, published_at: str, payload: dict[str,JSONValue]`; no secrets |
| `ReadyStep` | `step_id: StepId, ready: bool, reasons: tuple[str,...]` |
| `PrivateGrant` | `orchestrator_token: str | None, attempt_token: str | None`; separate from public results, repr-redacted |
| `MutationReceipt` | `committed: dict[str,JSONValue], committed_metadata: Metadata, current_metadata: Metadata, replayed: bool, private: PrivateGrant | None`; fenced bootstrap replay has `private=None` |
| `DomainError` | exception with `code: str, message: str, current_metadata: Metadata | None`; codes include `invalid`, `conflict`, `forbidden`, `fenced`, `stale_epoch`, `stale`, `not_found`, `held`, `resource_conflict`, `busy`, `schema_newer`, `handoff_conflict` |

`CommandEnvelope` is a typed tagged record with `kind: CommandKind, epoch: Epoch, key: RequestKey, actor: str, plan_id: PlanId | None, generation: Generation | None, expected_revision: int | None, ownership_version: int | None, orchestrator_token: str | None, attempt_id: AttemptId | None, attempt_token: str | None, attempt_version: int | None, definition_version: DefinitionVersion | None, payload: CommandPayload`. `BOOTSTRAP_KINDS`, `WORKER_KINDS`, and `ORCHESTRATOR_KINDS` are exact disjoint frozensets derived from the normative matrix. `CommandKind` is the Literal union of the kind strings in the next table; `CommandPayload` is the union of one named TypedDict per row (`CreatePlanPayload`, `RevisePlanPayload`, `TakeoverPayload`, `RegisterResourcePayload`, `AssignStepPayload`, `BindAvailabilityPayload`, `StartStepPayload`, `ReportActivityPayload`, `RecordCheckpointPayload`, `ResolveReassessmentPayload`, `RecordReviewPayload`, `AcceptStepPayload`, `ResolveAttemptPayload`, `ReserveVerificationPayload`, `ReleaseResourceUsePayload`, `AppendLateEvidencePayload`, `ImportPlanPayload`, `ResolveStoreHoldPayload`, `ReopenAttemptPayload`, `SweepStalePayload`, `ReconcileResourcesPayload`, `InvalidateResultPayload`, `ResolveInvalidationPayload`). `Command` aliases the validated discriminated union of these envelopes, with payload correlated to kind by `parse_command(raw: Mapping[str,JSONValue]) -> Command`; reject unknown fields and mismatched kind/payload, rather than trusting annotations. Secrets have redacted representations. Only `parse_command` defaults omitted revise_plan.retire_steps to `()` and resolve_attempt.attached_context to None; all validated payloads contain these fields, making retries canonical. `CommandPayload` variants have exactly these fields:

| `kind` | Payload fields |
| --- | --- |
| `create_plan` | `creation_id: str, draft: PlanDraft, new_plan_intent: str | None` |
| `revise_plan` | `draft: PlanDraft, reason: str, authorization_change: AuthorizationChange | None, retire_steps: tuple[RetirementRequest,...]` |
| `takeover` | `expected_ownership_version: int, reason: str` |
| `register_resource` | `claim: ResourceClaim` |
| `reconcile_resources` | `resource_ids: tuple[ResourceId,...], canonical_claim: ResourceClaim, reason: str`; no stopped-use field |
| `invalidate_result` | `step_id: StepId, result_version: ResultVersion, reason: str, evidence: tuple[Evidence,...]` |
| `resolve_invalidation` | `invalidation_id: str, affected_step_id: StepId, disposition: Literal['revalidated','superseded','replanned'], corrective_step_id: StepId | None, corrected_result_version: ResultVersion | None, evidence: tuple[Evidence,...], reason: str` |
| `assign_step` | `step_id: StepId, assignee: str, context: ExecutionContext` |
| `bind_availability` | `bindings: tuple[Availability,...], checked_base: str` |
| `start_step`, `report_activity` | `origin_worker: str, relay: str | None, observed_activity: str` |
| `record_checkpoint` | `checkpoint: Checkpoint` |
| `resolve_reassessment` | `checkpoint_id: str, decision: Literal['apply','reject','hold'], reason: str, draft: PlanDraft | None, affected_steps: tuple[StepId,...], unbounded: bool` |
| `reopen_attempt` | `reason: str` |
| `sweep_stale` | empty payload `{}`; target plan in envelope |
| `record_review` | `step_id: StepId, review: Review` |
| `accept_step` | `step_id: StepId, artifact: ArtifactRef, stopped_resource_ids: tuple[ResourceId,...], stopped_reason: str | None` |
| `resolve_attempt` | `resolution: Literal['confirm-owner','replace','reject','release','attach-context'], reason: str, next_step_state: Literal['pending','blocked','retired'], stopped_resource_ids: tuple[ResourceId,...], replacement_assignee: str | None, replacement_context: ExecutionContext | None, attached_context: ExecutionContext | ExecutionContextClaims | None` (normal variants require attached_context=None) |
| `reserve_verification` | `step_id: StepId, verifier: str, context: ExecutionContext, handoff_from: str | None, stopped_reason: str | None` |
| `release_resource_use` | `use_id: str, resource_ids: tuple[ResourceId,...], reason: str, stopped: bool` |
| `append_late_evidence` | `origin_attempt_id: AttemptId, evidence: tuple[Evidence,...], stopped_resource_ids: tuple[ResourceId,...], reason: str` |
| `import_plan` | `host_id: str, source_directory_id: ResourceId | None, source_directory_claim: ResourceClaim | None, original_task_id: str, original_snapshot: dict[str,JSONValue], provenance: DocumentRef, writers_stopped: str, known_contexts: dict[StepId,ExecutionContext | ExecutionContextClaims]`; exactly one of ID/claim is present |
| `resolve_store_hold` | `backup_cutoff: str, inventory_complete: bool, absent_work_inventory: tuple[HostWork,...], resource_resolutions: tuple[ResourceResolution,...], reason: str` |

Normative capability matrix (installation bearer authenticates every MCP request independently):

| Kinds | Required domain tokens | Forbidden extras / replay authority |
| --- | --- | --- |
| create_plan, import_plan, takeover | none; installation credential/current epoch; takeover additionally target generation and expected ownership version | both domain tokens forbidden; bootstrap grant-current-only replay |
| start_step, report_activity, record_checkpoint with relay=None | current attempt token only; origin_worker equals assigned worker | orchestrator token forbidden; authenticate attempt before exact replay |
| same three worker kinds with relay non-null | current orchestrator plus current attempt token; origin_worker equals assigned worker | both checked before exact replay; old relay owner fenced after takeover |
| every other mutator in the kind table | current orchestrator of target plan; resolve_store_hold permits current orchestrator of ANY authorizing plan | attempt token forbidden; orchestrator checked before replay; orphan release permits any plan owner but normal uses require their own plan |

Extra tokens are forbidden. Accepted attempt grants become replay-only for their own committed worker commands after epoch/generation/token validation; new progress fails terminal checks. Explicit rejected/released/replaced grants are fenced even for replay. Reads never change persisted state. Installation info/listing/plan bootstrap/takeover/resource registration/identity attachment and reconciliation remain available during store hold; assignment/start/reopen/accept and reserve_verification creating or activating writable use remain blocked. The verification guard returns held before reservation/handoff mutation, with no new use/event; read-only inspection and recording already observed immutable review evidence remain available. A result's invalidation hold alone does not prohibit its artifact-bound revalidation review. Exact authenticated replay remains ordered before new-operation guards.

Common interfaces: `PlanningService.read_service_info() -> InstallationView`, `PlanningService.execute(command: Command) -> MutationReceipt`, `list_plans(include_terminal: bool=False) -> tuple[PlanView,...]`, `read_plan(plan_id: PlanId) -> PlanView`, `read_events(plan_id: PlanId, after: int=0, limit: int=100) -> tuple[Event,...]`, `list_ready(plan_id: PlanId) -> tuple[ReadyStep,...]`, `read_coordination(plan_id: PlanId, after: str | None=None, limit: int=100) -> CoordinationPage`. Limits are validated, no arbitrary SQL or filesystem operations. Required public committed payload keys: register_resource.resource_id, assign_step.attempt_id, record_checkpoint.checkpoint_id, reserve_verification.use_id, invalidate_result.invalidation_id, import_plan.source_directory_id; epoch/plan/generation/revision live in committed_metadata. Each receipt remains immutable and excludes tokens from committed.

Future task commits use exact paths listed in their Files block. They are local checkpoints only; inspect staged diff, exclude unrelated dirty work, and do not push. No commit occurs during planning.

## Milestone 1 — Persistent planning, shared HTTP, dashboard, and container

Working deliverable: reusable rules and persistent **planning-only** create/revise events, current orchestration, one HTTP service, shared dashboard, and three-client read/write planning proof. Assignment/start/checkpoint/review/acceptance tools remain absent until Milestone 2 gates pass; this milestone is not a complete release.

### Task 1: Prove the pinned SDK and client transport contract before building

**Files:** Create `pyproject.toml`, `uv.lock`, `adaptive_planning/__init__.py`, `tests/__init__.py`, `tests/test_service_sdk.py`.
**Interfaces:** Consumes pinned versions above. Provides locked environment, import-safe empty package, and test-only `async list_tool_names(server: MCPServer) -> tuple[str,...]`, `async probe_protocols() -> tuple[SdkProbe,...]`; `SdkProbe(version: str, tool_names: tuple[str,...], echoed: str, listener_closed: bool, path: str)` records the actual modern/legacy test path.

- [ ] **RED:** Add `test_streamable_http_two_protocols` using an ephemeral official `MCPServer` exposing a synthetic echo tool, localhost Uvicorn, and official client. Use `from mcp import Client`: fresh authenticated transport for `Client(transport, mode="auto")` yields modern `2026-07-28` per-request metadata without initialize; fresh `Client(transport, mode="legacy")` negotiates `2025-11-25` initialize. Both perform actual list/call; assert `client.protocol_version`, not a header-only override. Never use the legacy version string 2025-11-25 as mode; use auto/legacy explicitly for this probe. Assert actual version/path, echo/tool result and closed listener.

```python
def test_streamable_http_two_protocols(self):
    probes = asyncio.run(probe_protocols())
    self.assertEqual({p.version for p in probes}, {"2026-07-28", "2025-11-25"})
    self.assertTrue(all("echo" in p.tool_names and p.echoed == "synthetic" and p.listener_closed for p in probes))
```

- [ ] **Run fail:** `python -B -m unittest tests.test_service_sdk -v`; expected missing SDK/project dependency failure before implementation.
- [ ] **Implement:** Define build-system `requires=["hatchling==1.32.4"]`, `build-backend="hatchling.build"`; project `name="adaptive-planning"`, `version="0.1.0"`, `requires-python=">=3.13"`, exact five runtime dependencies from Tech Stack plus `mcp-types==2.3.0`, wheel `packages=["adaptive_planning"]`. Create minimal import-safe `__init__.py`; add CLI entry in Task 7, assets in Task 9. Lock using `uv lock`. Use `from mcp.server import MCPServer`, not FastMCP. SDK client: `streamable_http_client(url, http_client=http_client)` yields `(read, write)`; `async with httpx2.AsyncClient(headers={"Authorization":"Bearer synthetic-test-token"}) as http_client` supplies the test credential; `async with Client(streamable_http_client(url,http_client=http_client), mode=mode) as client` executes list/call in each explicit mode. ClientSession.initialize() has no version argument and is not the positive modern path. Build only the test harness now. Verify installed Codex `0.160.1`, AGY `1.3.0`, Copilot CLI `1.0.92`, VS Code `1.140.0` availability/version without modifying configuration; record missing surfaces.
- [ ] **Run pass:** `uv run --frozen python -B -m unittest tests.test_service_sdk -v`; both protocols pass. If pins, entry points, or required installed-client surface fail, stop dependency/transport integration here, report exact limitation, and resolve it before larger implementation. Documentation is not runtime proof.
- [ ] **Local commit:** `git add pyproject.toml uv.lock adaptive_planning/__init__.py tests/__init__.py tests/test_service_sdk.py`; `git commit -m "test: pin and prove MCP transport contract"`.

### Task 2: Extract pure legacy rules and define typed contracts/fixtures

**Files:** Create `adaptive_planning/core.py`, `adaptive_planning/types.py`, `tests/service_fixtures.py`, `tests/test_service_core.py`; modify `adaptive_planning/__init__.py`, `scripts/live_plan.py:177-951` only around rule extraction/imports.
**Interfaces:** Consumes existing legacy validators. Provides DTOs/commands above, `parse_command`, `PlanValidationError`, `validate_proposal_schema`, `validate_plan_steps_and_graph`, `validate_plan`, `validate_published_snapshot` preserving their existing signatures; `prepare_publication(proposal: dict[str,JSONValue], previous: dict[str,JSONValue] | None, *, timestamp: str, generation: Generation, expected_revision: int, expected_generation: str | None, authorization_note: str | None, migrate_v1: bool=False, max_file_bytes: int, max_history: int) -> dict[str,JSONValue]`. `validate_service_graph(proposed: dict[str,JSONValue], previous: dict[str,JSONValue] | None, coordination: CoordinationView) -> None` shares DAG/evidence/retirement/scope primitives while enforcing the service state table. Provides `validate_execution_scope(previous: Authorization, step_id: StepId | None) -> Authorization` for assignment eligibility/latching, and `renew_authorization(previous: Authorization, change: AuthorizationChange, completed: tuple[StepId,...], claimed_steps: tuple[StepId,...], *, revision: int, interval: str) -> Authorization`. No I/O in core.

Fixtures defined once: `FakeClock(start: datetime)` with `__call__() -> datetime` and `advance(seconds: int) -> None`; `step(step_id: str, dependencies: tuple[str,...]=(), *, review_required: bool=False, final_gate: bool=False) -> StepDefinition`; `draft(mode: str='complete-task', steps: tuple[StepDefinition,...] | None=None) -> PlanDraft` defaults to independent `a`,`b` and dependent `c` on `a`; `make_service(directory: Path, clock: FakeClock | None=None) -> ServiceFixture`; `ServiceFixture` exposes `service, store, directory: Path, clock, epoch`; Store path is directory/planning.sqlite and Config.data_dir is directory; `command(kind: CommandKind, payload: CommandPayload, *, receipt: MutationReceipt | None=None, attempt: MutationReceipt | None=None, key: str | None=None, expected_revision: int | None=None, refresh_metadata: bool=True) -> Command`. By default read CURRENT public epoch/generation/revision and own attempt version. Select tokens by the normative kind/relay matrix, not receipt presence: a receipt may supply public plan info without exporting its orchestrator token into a direct worker command. Grants/ownership_version come only from supplied receipt/cached original private grant; bootstrap commands strip both tokens. `refresh_metadata=False` freezes the supplied receipt epoch/generation/revision/attempt version; explicit expected_revision overrides refresh for negative CAS. Return immutable deep-copied commands; capture a command BEFORE mutation and replay that same object unchanged. Bootstrap receipt=None has no plan/grant and reads only installation info; `create(mode: str='complete-task', steps: tuple[StepDefinition,...] | None=None, creation_id: str='fixture-create', key: str | None=None) -> MutationReceipt`; `workspace(path: str, inode: int, *, repository_id: str | None=None, case_insensitive: bool=False) -> ResourceClaim` uses host `synthetic-host`, device `7`; `context(workspace_id: str | None, *, repository_id: str | None=None, external_ids: tuple[str,...]=(), writes: bool=True) -> ExecutionContext`; `checkpoint(outcome: str='passed', *, affected: tuple[str,...]=(), unbounded: bool=False, artifact_version: str='v1') -> Checkpoint` gives explicit synthetic check/evidence, worker `worker-a`, immutable artifact `synthetic-artifact`, unchanged reason unless effects specified. Also define pure `legacy_proposal() -> dict[str,JSONValue]` by copying the existing valid_v2_proposal fixture and `coordination(attempts: tuple[AttemptView,...]=()) -> CoordinationView` with empty views/no store hold/store_sequence=0. `make_service` lazy-imports app only when called. Add lazy fixture methods `assign(receipt: MutationReceipt, step_id: str="a") -> MutationReceipt` using read-only context and worker-a, `start(receipt: MutationReceipt, attempt: MutationReceipt) -> MutationReceipt`, `submit(receipt: MutationReceipt, attempt: MutationReceipt, value: Checkpoint | None=None) -> MutationReceipt`, and `accept(receipt: MutationReceipt, attempt: MutationReceipt, value: Checkpoint) -> MutationReceipt`; `accept` always sends empty stopped_resource_ids/stopped_reason=None, never invents stop confirmation. `register(claim: ResourceClaim, receipt: MutationReceipt) -> ResourceId` extracts committed resource_id; create supplies explicit independent synthetic new-plan intent. Each builds defined commands, retains initial private tokens internally, and reads latest attempt version. Service-test `setUp` from Task 4 creates TemporaryDirectory, `self.f=make_service(Path(directory))`, `self.p=self.f.create()`, `self.plan_id=self.p.current_metadata.plan_id`; cleanup closes temp directory. Pure/Store/SDK tests use their direct setup.

- [ ] **RED:** `test_core_import_without_service_dependencies` imports core under Python3.10 with SDK imports blocked; `test_absolute_legacy_cli_outside_checkout` runs the absolute legacy script from a temporary directory; `test_scope_publication_is_pure` verifies unchanged input and injected timestamp/generation. `test_service_status_boundary_preserves_strict_legacy` pins stale/explicitly stopped service attempts retaining in-progress history: service-derived blocked/stopped is valid, while unchanged strict legacy validators reject blocked/stopped/planning publications with in-progress steps. `test_core_snapshot_limits` directly checks 201 nodes/51 evidence items/>10MB reject without truncation. Preserve public aliases and storage-fault patch targets.

```python
def test_service_status_boundary_preserves_strict_legacy(self):
    proposed = legacy_proposal()
    proposed["steps"][0]["status"] = "in-progress"
    attempt = AttemptView("x", "step-1", "worker-a", "stale", "active", 1, "v1", "2026-10-06T00:00:00Z", context(None,writes=False), True, None)
    for status in ("blocked", "stopped"):
        proposed["status"] = status
        validate_service_graph(proposed, None, coordination((attempt,)))
        with self.assertRaises(PlanValidationError):
            validate_plan(proposed)
```

- [ ] **Run fail:** `python -B -m unittest tests.test_service_core -v`; expected missing core.
- [ ] **Implement:** Move validators at lines 177/238/412/491 and pure scope/baseline/latch/migration decisions from publication lines 700–951. `prepare_publication` enforces expected revision (create0/update previous), generation (create none/v2 current/v1 literal "none"), nonempty note for create/v1/mode change; same-mode explicit legacy note renews interval. New revision=previous+1 or1; timestamp=max(injected UTC,previous.updatedAt); generation=injected on create/migration, retained on ordinary v2. Preserve call-boundary `datetime`, `uuid`, validators and storage patch targets by obtaining/injecting values in legacy wrapper; retain legacy locking/CAS/file writes/root assets and patch targets lp.MAX_FILE_BYTES, lp.MAX_HISTORY, lp.uuid.uuid4, lp.datetime, read_published_snapshot, _atomic_replace_with_retry, shutil.copyfileobj, FileLock, os.replace, time.sleep, json.load, ThreadedHTTPServer methods, and public validators. Wrapper passes CURRENT patched limits/time/UUID/validators at call boundary, never copies constants into core defaults; prepare_publication receives max_file_bytes/max_history explicitly; preserve bounded-history/output-limit and UTF8/nonfinite/deterministic-wire regressions. Absolute CLI import adds resolved checkout/package root, without SDK dependency/import-time serving. Bundled sibling core/assets are required after extraction; copied single live_plan.py without sibling core is unsupported. Update legacy docstring accordingly, retain original installed-skill filenames/layout. Keep strict legacy publication consistency checks as the default, including current lines 397–409; never use `legacy=True` to bypass safety. Split shared primitives from the legacy status consistency wrapper. Service validates through `validate_service_graph`, preserving in-progress history with derived blocked/stopped state. `PlanView.legacy_snapshot` carries the service-derived status also exposed by PlanView.status, so blocked/stopped plus retained in-progress history is expressly non-importable; label it non-importable when strict legacy rules fail and set `legacy_publication=None`. Coordination metadata never enters strict legacy proposals.
- [ ] **Run pass:** `uv run --no-project --python 3.10 python -B -m unittest tests.test_service_core -v`; pure module/fixtures block SDK imports and lazily import app, absolute CLI subprocess uses sys.executable 3.10 from outside checkout. Core/types/__init__ syntax is 3.10-compatible. Then locked 3.13 full discovery passes; `node --check assets/dashboard/app.js`; `node tests/test_frontend_dom.js`.
- [ ] **Local commit:** `git add adaptive_planning/__init__.py adaptive_planning/core.py adaptive_planning/types.py scripts/live_plan.py tests/service_fixtures.py tests/test_service_core.py`; `git commit -m "refactor: share pure planning rules without changing legacy storage"`.

### Task 3: Atomic SQLite authority, epochs, and event projection

**Files:** Create `adaptive_planning/schema.py`, `adaptive_planning/store.py`, `tests/test_service_store.py`.
**Interfaces:** Consumes `MutationReceipt`, `Clock`. Provides `Store(path: Path, clock: Clock, host_id: str | None=None)`, `Store.write(operation: Callable[[sqlite3.Connection], T]) -> T`, `Store.read(operation: Callable[[sqlite3.Connection], T]) -> T`, `Store.installation_epoch() -> Epoch`, `initialize_schema(connection: sqlite3.Connection) -> None`; generic `T` is a TypeVar. Tables: installation, plans, steps, attempts, capabilities, resources, resource_redirects, aliases, absent_host_work, store_hold_reasons, resource_uses, availability, checkpoints, holds, reviews, result_versions, events, requests, creation_ids, imports. Keys: installation singleton/epoch; plans PK(plan_id), UNIQUE(generation); steps PK(plan_id,step_id); attempts PK(plan_id,attempt_id), FK(plan_id,step_id); result_versions PK(plan_id,result_version), FK(plan_id,step_id); checkpoint/review/availability FKs retain same plan/step/attempt/result pairing. Requests UNIQUE(epoch,key); creation_ids UNIQUE(epoch,creation_id) maps one plan/content; imports UNIQUE(host_id,source_directory_id,original_task_id), digest/tuple metadata only. Resource redirects FK retired_id/survivor_id, no cycles; physical/alias indexes scoped by host; aliases unique by host plus normalized canonical path; resource_uses normal rows retain plan/attempt scoped FKs; orphan rows require plan_id/attempt_id NULL and absent_work_id FK(absent_host_work), exclusive source CHECK constraint. All unresolved rows indexed for global overlap; orphan rows persist conservative scope: a bare repository identity is repository-exclusive against descendant workspaces, workspace/external identities are exclusive under normal predicates. installation.authority_kind is authority|backup; store_hold_reasons persists unidentified import/restore attempts/work. Event.sequence is a store-wide INTEGER PRIMARY KEY AUTOINCREMENT; every semantic mutation including store-wide reconciliation/release appends its event atomically. store_sequence is MAX(events.sequence), or 0 for an empty store, read in the same SQLite snapshot as each projection. It is a projection watermark, not another CAS or authorization revision. Never permit cross-plan FK substitution.

- [ ] **RED:** `test_atomic_failure_rolls_back_every_record` injects failure after event insert and asserts no plan/snapshot/request/event; `test_restart_preserves_epoch_and_events` closes/reopens DB and compares epoch and event sequence; `test_write_contention_is_bounded` uses barrier/separate connections, gets structured `busy` within the configured bounded wait.

```python
def test_restart_preserves_epoch_and_events(self):
    with TemporaryDirectory() as directory:
        path = Path(directory)/"planning.sqlite"
        clock = FakeClock(datetime(2026,10,6,tzinfo=timezone.utc))
        store = Store(path, clock)
        epoch = store.installation_epoch()
        reopened = Store(path, clock)
        self.assertEqual(reopened.installation_epoch(), epoch)
        self.assertEqual(reopened.read(lambda db: db.execute("SELECT count(*) FROM events").fetchone()[0]), 0)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_store -v`; expected missing Store.
- [ ] **Implement:** Own data directory mode0700, DB/WAL/SHM/backup/private grant storage0600 with umask0077; assert actual permissions. Apply global PRAGMAs per connection; use short writes, rollback on every exception, close connections deterministically. Snapshots are transactional materializations, never a second authority; Events use semantic command kind strings (for example record_checkpoint); post-commit notifications carry sequence only. Create installation epoch and shared host UUID once (fixture injection host_id="synthetic-host"); reject mismatched reopening injection. Registration must match this host, never client-invented namespaces. Create installation epoch once; reopening preserves it. Version schema at `1`; refuse newer schema before any mutation.
- [ ] **Run pass:** Same module command; all tests pass; inspect actual PRAGMA values in test assertions.
- [ ] **Local commit:** `git add adaptive_planning/schema.py adaptive_planning/store.py tests/test_service_store.py`; `git commit -m "feat: add transactional SQLite planning authority"`.

### Task 4: Durable creation, semantic planning CRUD, and safe retries

**Files:** Create `adaptive_planning/plans.py`, `adaptive_planning/app.py`, `adaptive_planning/ownership.py`, `tests/test_service_plans.py`; modify `tests/service_fixtures.py` to construct the service.
**Interfaces:** Consumes `Store.write`, `Store.read`, `PlanDraft`, `validate_service_graph`, `renew_authorization`. Provides `issue_orchestrator(connection: sqlite3.Connection, plan_id: PlanId, actor: str) -> PrivateGrant`, `PlanningService(store: Store, *, base_url: str, clock: Clock)` and read/execute signatures above; `create_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `revise_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `project_plan(connection: sqlite3.Connection, plan_id: PlanId) -> PlanView`, `retire_step(connection: sqlite3.Connection, plan_id: PlanId, request: RetirementRequest, actor: str) -> None` (initially unassigned pending/blocked only). These domain handlers share the same transaction connection; no nested transactions.

- [ ] **RED:** `test_creation_id_survives_changed_request_key` asserts two keys with same creation ID/draft yield one plan; changed content conflicts. `test_request_replay_and_cas` captures immutable command before mutation; refresh_metadata=False or explicit old expected_revision pins negative CAS/generation/epoch fixtures. Exact retry returns original committed metadata plus separately current metadata; changed kind/payload under same key fails with zero events; stale generation/revision fails. `test_limits_without_truncation` asserts 201 nodes/>10MB plan content or coordination page fail and explicit horizon 11 retained; evidence bounds are pure Task 2 and worker Task 15 tests. `test_unassigned_retirement` retires pending step with explicit reason, preserves node/definition/history; completed retirement/alteration and invalid DAG fail atomically. `test_revise_cannot_tamper_authorization` rejects altered baseline/claimed/interval/since_revision. `test_empty_store_epoch_bootstrap` calls read_service_info on empty store, then creates using returned epoch and finds one plan. `test_duplicate_candidate_not_silent_merge` lists same objective/workspace as possible duplicate and requires new-plan intent for deliberate separation.

```python
def test_creation_id_survives_changed_request_key(self):
    first = self.f.create(key="create-one")
    retry = self.f.create(key="create-two")
    self.assertEqual(first.current_metadata.plan_id, retry.current_metadata.plan_id)
    self.assertEqual(len(self.f.service.list_plans()), 1)

def test_empty_store_epoch_bootstrap(self):
    with TemporaryDirectory() as directory:
        fresh=make_service(Path(directory))
        self.assertEqual(fresh.service.list_plans(),())
        info=fresh.service.read_service_info()
        command=fresh.command("create_plan",{"creation_id":"bootstrap","draft":draft(),"new_plan_intent":None})
        self.assertEqual(command.epoch,info.epoch)
        receipt=fresh.service.execute(command)
        self.assertEqual(receipt.current_metadata.epoch,info.epoch)

def test_unassigned_retirement(self):
    before=self.f.service.read_plan(self.plan_id)
    command=self.f.command("revise_plan",{"draft":before.draft,"reason":"explicit retirement","authorization_change":None,"retire_steps":(RetirementRequest("b","synthetic obsolete route"),)},receipt=self.p)
    self.f.service.execute(command)
    after=self.f.service.read_plan(self.plan_id)
    node=next(s for s in after.legacy_snapshot["steps"] if s["id"]=="b")
    self.assertEqual(node["status"],"retired")
    self.assertEqual(after.draft.authorization,before.draft.authorization)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_plans -v`; missing service/handlers.
- [ ] **Implement:** Project renderer-compatible legacy_snapshot: schemaVersion2/taskId=plan_id/objective/status/executionMode/nextStepId/steps; scope uses camelCase mode/sinceRevision/baseline/claimed; metadata revision/generation/updatedAt match current committed state, changeSummary is nonempty latest public event summary. history has latest100 public event summaries with revision/timestamp/summary (full events retained); deterministic string evidence joins summary/check/result/artifact identity/version/digest/limitations/blockers, never private fields. Imported original bounded history remains provenance, not invented reverified evidence. Keep separate coordination/derived status; populate optional importable `legacy_publication` only if strict legacy validation passes. Persist creation identity, public metadata and events; issue the initial random private orchestrator grant with `issue_orchestrator`; Task 5 supplies takeover and complete replay/fencing enforcement before transport exposure. Create requires empty baseline/null claimed, interval=""/since_revision=0; server supplies interval UUID/since_revision1. Ordinary revise requires echoed authorization identical to current, never treats stored note as renewal. Only nullable `authorization_change` grounds a new mode/interval assertion; server derives baseline from accepted work, claimed_steps from assigned/active/submitted/stale-unresolved ownership, latch, fresh interval/since_revision=new revision. First-step renewal claims the one unfinished nonbaseline logical step; two such steps reject. Client never supplies authoritative claimed_steps; legacy note-renewal branch retains its existing graph semantics separately. Import has a separate validated history path. Use retire_steps to apply explicit unassigned retirement, retaining nodes/reason and enforcing pending/blocked-only legacy transition, evidence-prefix and DAG rules; never infer completion or stopped host use. At this stage allow only planning mutations. Canonical command bytes use UTF-8 JSON sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False; tuples become arrays, finite floats use standard JSON representation, no Unicode normalization. Compare the entire validated envelope, including public epoch/generation/CAS/attempt/ownership metadata and supplied tokens; exact replay reuses original immutable command bytes. Epoch-scoped idempotency retains keys for epoch lifetime. Reads are immutable copied DTOs and expose no secrets. read_plan includes full bounded graph/evidence plus coordination summary/priority first page, not all history. Priority current/live/unresolved rows use adaptive byte budget; old terminal attempts/reviews remain reachable via read_coordination/events. Each page<=10MB; cursor binds installation epoch, target plan ID/revision and store_sequence read in one snapshot, and any changed component rejects with conflict before returning rows; if graph leaves no row room, summary-only with next_cursor is valid. Safety logic/derived status uses full authority SQL aggregates, never just paged rows; has_live_attempts/has_unresolved_uses/has_plan_holds are plan-scoped. Coordination may expose store-level orphan uses for inspection; another plan's normal obligations do not alone prevent this plan completion, while store hold does. Reducers release/resolve do not add plan/evidence content or serialize old history; full reason remains events and resolved rows leave current page, so historical growth cannot reject safety reduction. Dashboard compares (epoch, plan revision, store_sequence), fetches all pages, and discards partial pages/restarts from read_plan on any cursor conflict or changed component. Another plan releasing an orphan use invalidates the cursor even when the target plan revision is unchanged; retain all authority records and global safety aggregates.
- [ ] **Run pass:** Same module command, including injected rollback after projection; malformed graph/history/evidence transitions fail through shared rules and service eligibility validation; strict legacy publication rejections remain unchanged.
- [ ] **Local commit:** `git add adaptive_planning/plans.py adaptive_planning/app.py adaptive_planning/ownership.py tests/service_fixtures.py tests/test_service_plans.py`; `git commit -m "feat: persist canonical plans and semantic retries"`.

### Task 5: Orchestrator grants, takeover, and fenced bootstrap replay

**Files:** Create `tests/test_service_ownership.py`; modify `adaptive_planning/ownership.py`, `adaptive_planning/plans.py`, `adaptive_planning/app.py`.
**Interfaces:** Consumes `issue_orchestrator` from Task 4. Provides `require_kind_capabilities(kind: CommandKind, relay: str | None, orchestrator_token: str | None, attempt_token: str | None) -> None` (enforces matrix token presence/forbidden extras before current-capability authentication), `require_orchestrator(connection: sqlite3.Connection, command: Command) -> None`, `takeover(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `replay_request(connection: sqlite3.Connection, command: Command) -> MutationReceipt | None`. Bootstrap create/takeover, and Task 22 import-as-creation, authenticate installation at transport, then exact request identity/current epoch.

- [ ] **RED:** `test_concurrent_takeover_one_winner` uses barrier/two connections against one ownership version; one succeeds. `test_lost_bootstrap_response_current_grant_only` exact create/takeover retry returns same token while current; after takeover returns original public receipt with `private is None`. `test_fenced_mutation_cannot_replay` old token cannot revise or replay previously committed revise; `test_read_event_repr_secret_redaction` scans projections/events/error/repr/log captures for synthetic tokens.

```python
def test_lost_bootstrap_response_current_grant_only(self):
    original = self.f.create(key="lost-create")
    takeover = self.f.command("takeover", {"expected_ownership_version":original.current_metadata.ownership_version,"reason":"synthetic explicit takeover"},receipt=original)
    self.f.service.execute(takeover)
    retry = self.f.create(key="lost-create")
    self.assertIsNone(retry.private)
    self.assertEqual(retry.committed_metadata.plan_id, original.committed_metadata.plan_id)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_ownership -v`; expected missing grant fencing.
- [ ] **Implement:** Cryptographically random tokens; SHA256 lookup of random 32-byte tokens for validation and private recoverable persisted grant material (0600 filesystem protection, not encryption) for lost-response bootstrap replay. Bump ownership version atomically, fence old orchestrator only; preserve worker attempts/resources/authorization. Authenticate current epoch/generation and operation-authorizing capability before ordinary replay; match existing key/kind/canonical command before new-command CAS/attempt-version/precondition checks, since a lost successful response carries pre-commit versions. New commands then check CAS/attempt state. Fencing is distinct from stale ownership. Old epoch never returns a secret. Installation bearer authenticates access, not domain actor authority.
- [ ] **Run pass:** Same module command; private bootstrap results survive process restart without appearing in read models.
- [ ] **Local commit:** `git add adaptive_planning/ownership.py adaptive_planning/plans.py adaptive_planning/app.py tests/test_service_ownership.py`; `git commit -m "feat: fence orchestrator ownership and protect retry grants"`.

### Task 6: Service configuration, explicit credential init, and route security

**Files:** Create `adaptive_planning/config.py`, `adaptive_planning/admin.py`, `tests/test_service_http.py`.
**Interfaces:** Consumes `DomainError`. Provides `Config(data_dir: Path, base_url: str='http://127.0.0.1:8790', stale_seconds: int=900)`, `load_config(environment: Mapping[str,str]) -> Config`, `initialize_installation(config: Config) -> None`, `load_service_token(config: Config) -> str`, `http_allowlist(config: Config) -> tuple[str,str]`, `RequestBoundary(app: ASGIApp, config: Config, token: str)`; `ASGIApp` uses Starlette's imported type only in service modules.

- [ ] **RED:** `test_explicit_init_private_token` asserts no token generated by import/read/start, explicit init writes `/data/service-token` mode `0600`, repeat init preserves bytes. `test_all_route_host_origin_and_mcp_auth` wraps a synthetic ASGI 200 app (not future build_app) and covers GET/POST/DELETE `/mcp`, missing/wrong bearer, invalid Host, missing Origin for CLI permitted, invalid supplied Origin rejected; valid dashboard GET needs no bearer. `test_logs_and_urls_are_secret_free` scans successful/error requests, including synthetic-token Pydantic/schema invalid inputs and exception/log paths.

```python
def test_explicit_init_private_token(self):
    with TemporaryDirectory() as directory:
        config = Config(Path(directory))
        token_path = config.data_dir/"service-token"
        self.assertFalse(token_path.exists())
        initialize_installation(config)
        original = token_path.read_bytes()
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        initialize_installation(config)
        self.assertEqual(token_path.read_bytes(), original)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_http -v`; missing boundary/init.
- [ ] **Implement:** Require existing private token at serving; fail permission/owner checks rather than echoing it. Validate Config.base_url as http with one loopback hostname (127.0.0.1 or localhost), explicit bound port, no userinfo/path/query/fragment. `http_allowlist(config: Config) -> tuple[str,str]` returns that exact hostname:port and origin; reject alternate ports/hostnames, wildcards and forwarding. Tests use actual bound ephemeral port; production default stays8790. Allowed supplied Origin is the validated origin; reject wildcard/forwarded-host bypasses. Constant-time credential comparison; protected middleware applies before all routes and every MCP method. No CORS grant.
- [ ] **Run pass:** Same module command; error responses contain structured public codes without request secrets.
- [ ] **Local commit:** `git add adaptive_planning/config.py adaptive_planning/admin.py tests/test_service_http.py`; `git commit -m "feat: secure local service initialization and HTTP boundaries"`.

### Task 7: Official MCP semantic adapters and lifecycle

**Files:** Create `adaptive_planning/transport.py`, `adaptive_planning/__main__.py`; modify `adaptive_planning/app.py`, `pyproject.toml`, `tests/test_service_http.py`, `tests/test_service_sdk.py`.
**Interfaces:** Consumes `http_allowlist`, `PlanningService.read_service_info`, `PlanningService.execute`, `list_plans`, `read_plan`, `read_events`, `list_ready`, `read_coordination`, `RequestBoundary`. Provides `build_mcp(service: PlanningService) -> MCPServer`, `build_app(config: Config) -> Starlette`, `main(argv: Sequence[str] | None=None) -> int`. Read tools: `read_service_info`, `list_plans`, `read_plan`, `read_events`, `list_ready`, `read_coordination`; Task 1 standalone SdkProbe uses direct setup; evolve tests/test_service_sdk.TestService to the shared service-fixture setUp in this task before self.f examples. Initial write tools: `create_plan`, `revise_plan`, `takeover`, each mapped to validated Command; later semantic tools added only with their domain gates.

- [ ] **RED:** `test_tools_use_service_and_private_mutation_result` SDK call creates/revises plan, metadata matches, reads omit token; invalid structured token-bearing input has no records and no token in adapter/schema errors/logs. `test_no_assignment_tool_in_planning_milestone` tool list omits all execution operations.

```python
# Temporary milestone assertion; Task 12 replaces this method.
def test_no_assignment_tool_in_planning_milestone(self):
    names = asyncio.run(list_tool_names(build_mcp(self.f.service)))
    self.assertIn("create_plan", names)
    self.assertNotIn("assign_step", names)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_sdk tests.test_service_http -v`; expected missing production MCP adapter.
- [ ] **Implement:** `allowed_host, allowed_origin = http_allowlist(config)` then `server.streamable_http_app(streamable_http_path='/mcp', json_response=True, stateless_http=True, max_request_body_size=12*1024*1024, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[allowed_host], allowed_origins=[allowed_origin]), host='0.0.0.0')`. Starlette lifespan enters `server.session_manager.run()`; mount MCP at `'/'` last after dashboard routes so `/mcp` has no redirect. Use Pydantic transport discrimination with `ConfigDict(hide_input_in_errors=True)` around stdlib DTO parsing; sanitize `.errors(include_input=False, include_context=False)` and exception responses with no input/context and log only whitelisted public fields; no handwritten JSON-RPC. Add project script `adaptive-planning="adaptive_planning.__main__:main"`. CLI commands `init`, `serve`, later `backup`, `restore`, `export`.
- [ ] **Run pass:** Both modules pass under `uv run --frozen`; assert POST `/mcp` does not redirect and both negotiated protocols still work.
- [ ] **Local commit:** `git add adaptive_planning/transport.py adaptive_planning/__main__.py adaptive_planning/app.py pyproject.toml tests/test_service_sdk.py tests/test_service_http.py`; `git commit -m "feat: expose shared planning through official MCP HTTP"`.

### Task 8: Multi-plan dashboard from committed read models

**Files:** Create `adaptive_planning/dashboard.py`, `tests/test_service_dashboard.py`; modify `assets/dashboard/app.js`, `assets/dashboard/index.html`, `assets/dashboard/style.css`, `tests/test_frontend_dom.js`, `adaptive_planning/app.py`.
**Interfaces:** Consumes `PlanningService.read_plan`, `list_plans`, `read_events`, `read_coordination`. Provides `dashboard_routes(service: PlanningService) -> list[BaseRoute]`, `render_plan_index(plans: tuple[PlanView,...]) -> str`; JSON endpoints `/api/plans`, `/api/plans/<planId>`, `/api/plans/<planId>/coordination?after=<cursor>&limit=100`, `/api/plans/<planId>/events?after=<sequence>&limit=100` use public projections only. Choose polling every 1000ms, one in-flight request; consume every page through last sequence before the next tick, no backlog loss, and full reread on reconnect/errors. Legacy SSE remains unchanged; no mutation endpoint.

- [ ] **RED:** `test_multiple_plans_and_committed_freshness` checks stable links, revision/published timestamp, optional workflow omitted when absent; injected rolled-back write never appears. Extend DOM fixtures for both body modes and renderer-compatible shape, then `<script>` titles/evidence, two active attempts, stale/holds/reviews, and assert text content renders safely with no execution; existing legacy DOM tests remain intact.

```python
def test_multiple_plans_and_committed_freshness(self):
    other = self.f.create(creation_id="second-plan")
    html = render_plan_index(self.f.service.list_plans())
    self.assertIn("/plans/"+self.plan_id, html)
    self.assertIn("/plans/"+other.current_metadata.plan_id, html)
    self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision, self.p.current_metadata.revision)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_dashboard -v`; `node tests/test_frontend_dom.js`; expected missing service routes/new DOM view.
- [ ] **Implement:** External app.js reads body data-mode="legacy"|"service"; absent mode defaults legacy. Service body includes escaped data-plan-id and public API URLs, no inline JavaScript config. Reuse legacy graph rendering with explicit service adapter; fetch every committed coordination page, restart on cursor revision conflict; show all active steps, reservations, origin/relay, evidence/findings, last published state, recovery controls as read-only descriptions. State that host edits are not controlled. CSP permits only packaged same-origin scripts/styles, disallows inline scripts/frame embedding; use textContent/escaped templates for untrusted strings. No progress ledger in browser storage.
- [ ] **Run pass:** Module, DOM suite and `node --check assets/dashboard/app.js` pass; manual browser synthetic index/plan checks record viewport, screenshot, safe rendering, and reconnect behavior.
- [ ] **Local commit:** `git add adaptive_planning/dashboard.py adaptive_planning/app.py assets/dashboard/app.js assets/dashboard/index.html assets/dashboard/style.css tests/test_service_dashboard.py tests/test_frontend_dom.js`; `git commit -m "feat: render durable multi-plan dashboard"`.

### Task 9: Packaged assets and reproducible persistent container

**Files:** Create `Dockerfile`, `compose.yaml`, `.dockerignore`, `tests/test_service_packaging.py`; modify `pyproject.toml`; create `docs/operations.md`.
**Interfaces:** Consumes `build_app`, `main`. Provides wheel with force-included `assets/dashboard` under `adaptive_planning/assets/dashboard`, CLI entry `adaptive-planning = adaptive_planning.__main__:main`, image command serving `0.0.0.0:8790`; volume `adaptive-planning-data:/data`.

- [ ] **RED:** `test_wheel_assets_and_import_safety` installs wheel in temporary environment and runs outside checkout, asset route works and import has no DB/listener side effects. `test_container_contract` parses config and asserts UID 10001, named volume, loopback port, no forbidden mounts, restart policy.

```python
def test_container_contract(self):
    compose = Path("compose.yaml").read_text()
    dockerfile = Path("Dockerfile").read_text()
    self.assertIn("127.0.0.1:8790:8790", compose)
    self.assertIn("unless-stopped", compose)
    self.assertIn("USER 10001", dockerfile)
    self.assertNotIn("docker.sock", compose)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_packaging -v`; missing artifact/container contract.
- [ ] **Implement:** Base `python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641`; pin uv 0.10.12, `uv sync --frozen --no-dev`, copy only package/assets/lock/build metadata, own `/data` by 10001. No token in build/environment image layers. Compose `127.0.0.1:8790:8790`, no bind-mounted checkout. Document explicit init (`docker compose run --rm adaptive-planning init`) before `docker compose up -d`; it never prints token. Asset lookup uses importlib.resources in service and retained root lookup in legacy.
- [ ] **Run pass:** `uv build`; module tests; `docker compose config`; `docker compose build`; explicit init/start; `docker inspect` verifies mount/UID/port/restart. Restart and `docker compose down` then `up -d` preserve plan ID/revision/URL; never use `down -v`. For linux/arm64 and linux/amd64 record build image ID/digest/platform, container uname/interpreter version and synthetic service SDK request plus asset GET; unavailable platform runtime remains explicit acceptance gap.
- [ ] **Local commit:** `git add Dockerfile compose.yaml .dockerignore pyproject.toml docs/operations.md tests/test_service_packaging.py`; `git commit -m "feat: package persistent loopback planning container"`.

### Task 10: Three-client planning interoperability gate

**Files:** Create `docs/clients/codex.md`, `docs/clients/antigravity.md`, `docs/clients/copilot.md`, `tests/test_service_interop.py`; modify `docs/operations.md`.
**Interfaces:** Consumes Task 7 tools and Task 9 service; provides documented client schemas and `test_three_sessions_share_planning_events` automated SDK equivalent implemented by test-only `async run_planning_interop(service: PlanningService) -> tuple[MutationReceipt,...]`, starting three actual official-client sessions against ephemeral production app. Manual installed-client results are an evidence table with version, surface, negotiated protocol, tool list, plan ID, committed revisions, dashboard URL.

- [ ] **RED:** Automated test opens three distinct SDK sessions: call read_service_info before create, then revise distinct facts sequentially; assert one plan, all three events, same epoch/generation/dashboard. Add docs schema tests for URL and secret mechanisms, excluding placeholder credentials from real client files.

```python
def test_three_sessions_share_planning_events(self):
    receipts = asyncio.run(run_planning_interop(self.f.service))
    self.assertEqual(len(receipts), 3)
    self.assertEqual(len({r.current_metadata.plan_id for r in receipts}), 1)
    self.assertEqual(len({r.current_metadata.revision for r in receipts}), 3)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_interop -v`; missing docs/check.
- [ ] **Implement:** Document current supported shapes with links below. Codex CLI: `codex mcp add adaptivePlanning --url http://127.0.0.1:8790/mcp --bearer-token-env-var ADAPTIVE_PLANNING_TOKEN`; TOML `url`/`bearer_token_env_var`. Name the tested surface explicitly: CLI environment tests do not prove Codex Desktop inherits terminal variables. Desktop requires verified GUI environment provisioning or documented private user TOML `http_headers` with Authorization; do not assume terminal exports reach an already running GUI. Record actual surface and method without token contents. AGY `serverUrl` plus literal `headers.Authorization`, no documented environment interpolation; provision via private 0600 raw config edit, never real-token CLI arguments. Copilot CLI user `~/.copilot/mcp-config.json`: `type: 'http'`, `url`, `headers: {'Authorization':'Bearer ${ADAPTIVE_PLANNING_TOKEN}'}`, `tools:['*']`; verify actual expansion: installed CLI uses environment-provided synthetic installation token, successful authenticated initialize/list/call proves expansion; literal ${...} or absent value fails authentication without token logging. Record actual surface/config schema, no claim from schema alone. VS Code has separate `servers`/`inputs`, password prompt `${input:adaptive-planning-token}`. Any actual client configuration requires execution authorization and private provisioning; docs use synthetic values only.
- [ ] **Run pass:** Module passes. With authorized setup, each installed local client lists tools, reads installation info to discover current epoch/host ID, reads canonical plan, and commits a **planning-only create/revise event** visible in dashboard; close creator session and resume from another. Record negotiated protocol, not assumed version. This is not the finished-worker checkpoint acceptance check; repeat it in Task 20. Missing client remains a release blocker.
- [ ] **Local commit:** `git add docs/clients/codex.md docs/clients/antigravity.md docs/clients/copilot.md docs/operations.md tests/test_service_interop.py`; `git commit -m "docs: prove shared planning across three local clients"`.

## Milestone 2 — Safe coordinated execution and adaptive acceptance

Working deliverable: complete domain-gated concurrent assignments, worker checkpoints, holds, review and acceptance. Add a semantic MCP tool only after its corresponding tests pass. The full release still waits for Milestone 3 recovery/operations/instruction acceptance.

### Task 11: Registered physical resources and cross-plan overlap

**Files:** Create `adaptive_planning/resources.py`, `tests/test_service_resources.py`; modify `adaptive_planning/app.py`, `adaptive_planning/transport.py`, `tests/service_fixtures.py`.
**Interfaces:** Provides TEST-ONLY `seed_orphan_use(fixture: ServiceFixture, resource_id: ResourceId) -> ReservationView`: Store.write inserts absent_host_work ID="seeded-orphan" and a scoped unresolved writer_active use for that canonical ID, null plan/attempt, repository_exclusive according to kind, with public event/projection; no command bypass in production. Consumes `Store.write`, `ResourceClaim`, `ExecutionContext`. Provides `reconcile_resources(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, private shared `register_claim(connection: sqlite3.Connection, claim: ResourceClaim) -> ResourceId`, `register_resource(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `resolve_context(connection: sqlite3.Connection, context: ExecutionContext) -> tuple[ResourceId,...]`, `conflicting_uses(connection: sqlite3.Connection, resource_ids: tuple[ResourceId,...], repository_exclusive: bool) -> tuple[str,...]`.

- [ ] **RED:** `test_alias_device_change_and_path_reuse` registers same inode/different aliases -> one ID; changed inode/same unresolved path cannot evade held ID; sensitive `/A` versus `/a` distinct, attested insensitive aliases same. `test_repository_workspace_external_non_git_overlap` tests repository-exclusive conflicts with descendants, separate worktrees permitted, external conflicts across plans, known non-Git workspace allowed, external-only allowed, required missing identity rejected.

```python
def test_alias_device_change_and_path_reuse(self):
    first = self.f.register(workspace("/synthetic/A",100),self.p)
    alias = self.f.register(workspace("/synthetic/alias",100),self.p)
    self.assertEqual(first,alias)
    lower = self.f.register(workspace("/synthetic/a",101),self.p)
    self.assertNotEqual(first,lower)

def test_reconcile_keeps_old_unresolved_uses(self):
    left=replace(workspace("/synthetic/alias-left",800),device=None,inode=None)
    right=replace(left,canonical_path="/synthetic/alias-right")
    a=self.f.register(left,self.p)
    b=self.f.register(right,self.p)
    use=seed_orphan_use(self.f,a)
    canonical=workspace("/synthetic/canonical",800)
    command=self.f.command("reconcile_resources",{"resource_ids":(a,b),"canonical_claim":canonical,"reason":"host-attested both paths refer to same physical workspace"},receipt=self.p)
    self.f.service.execute(command)
    for rid in (a,b,self.f.register(canonical,self.p)):
        conflicts=self.f.store.read(lambda db: conflicting_uses(db,(rid,),False))
        self.assertIn(use.use_id,conflicts)
    other=self.f.register(workspace("/synthetic/contradictory",801),self.p)
    before=self.f.store.read(lambda db: tuple(db.execute("SELECT retired_id,survivor_id FROM resource_redirects ORDER BY retired_id")))
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("reconcile_resources",{"resource_ids":(a,other),"canonical_claim":canonical,"reason":"contradictory physical identity"},receipt=self.p))
    after=self.f.store.read(lambda db: tuple(db.execute("SELECT retired_id,survivor_id FROM resource_redirects ORDER BY retired_id")))
    self.assertEqual(after,before)

```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_resources -v`; missing registry/overlap.
- [ ] **Implement:** Ordinary `register_resource` requires the current orchestrator; its private `register_claim` helper registers identity only and performs the same host/physical/alias/conflict checks when called inside import bootstrap. It never reserves execution use or confirms stopped use. Reject claim.host_id != read_service_info().host_id (`test_resource_cannot_invent_host_namespace`); resolve by physical identity OR canonical alias on shared host; keep conflicting claims held pending explicit reconciliation. Repository-known/unknown-workspace write claims reserve entire repository; unknown required repository blocks. Explicit non-repository is distinct from unknown repository. reconcile_resources requires explicit same-host physical equivalence/reason and consistent kind/case/physical claims; reject unclear/contradictory claims/cycles. Choose canonical_claim-matching survivor if among candidates, else lexical-min ID; preserve retired IDs/history/aliases/physical keys via durable redirects. Resolve redirects in all queries/FKs; never change any use liveness/unresolved state or release. Ambiguous registration fails until reconciliation; test_reconcile_keeps_old_unresolved_uses conflicts by either old/new ID/path and contradictory merge rolls back. Use database unique indexes plus conservative overlap predicate over every unresolved normal OR store-level orphan use; never stat host paths in container.
- [ ] **Run pass:** Same module command; rolled-back registration leaves no alias/orphan use.
- [ ] **Local commit:** `git add tests/service_fixtures.py adaptive_planning/resources.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_resources.py`; `git commit -m "feat: register conservative cross-plan resource identities"`.

### Task 12: Atomic assignment and frozen attempt definitions

**Files:** Create `adaptive_planning/attempts.py`, `tests/test_service_attempts.py`; modify `adaptive_planning/schema.py`, `adaptive_planning/plans.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`, `tests/test_service_sdk.py`.
**Interfaces:** Consumes `resolve_context`, `conflicting_uses`, `validate_service_graph`, `validate_execution_scope`, `retire_step`. Provides `assign_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `require_attempt(connection: sqlite3.Connection, command: Command) -> AttemptView` (current epoch/generation/token authentication; NEW-command version/definition/lifecycle checks occur after replay), `ready_steps(connection: sqlite3.Connection, plan_id: PlanId) -> tuple[ReadyStep,...]`, `current_attempt(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId) -> AttemptView` (public scoped lookup, not capability authentication), `cancel_attempt(connection: sqlite3.Connection, plan_id: PlanId, attempt_id: AttemptId, reason: str) -> None`; extends retire_step for claimed work.

- [ ] **RED:** `test_same_step_one_assignment_winner` barrier/two DB connections gives one attempt/token/reservation/event; `test_independent_workers_cross_plan_conflicts` separate worktrees succeed, shared writable resource conflicts; `test_attempt_definition_frozen` title/dependencies/check/document-version revision is rejected while claimed unless explicit cancellation/reassignment preserves old attempt; `test_ready_is_advisory` resource acquired after list_ready makes assignment fail.

```python
def test_same_step_one_assignment_winner(self):
    barrier = threading.Barrier(2)
    def assign():
        barrier.wait()
        try:
            return self.f.assign(self.p)
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: assign(),range(2)))
    self.assertEqual(sum(isinstance(r,MutationReceipt) for r in results),1)
    self.assertEqual(len(self.f.service.read_plan(self.plan_id).coordination.attempts),1)

def test_plan_only_denies_assignment(self):
    names=asyncio.run(list_tool_names(build_mcp(self.f.service)))
    self.assertIn("assign_step",names)
    p=self.f.create("plan-only",creation_id="planning-only")
    with self.assertRaises(DomainError):
        self.f.assign(p)
    self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).coordination.attempts,())
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_attempts tests.test_service_sdk -v`; missing assignment.
- [ ] **Implement:** Recheck pending state, accepted prerequisites, holds, complete scope gate, live/stale ownership and resources inside assignment transaction; call Task 2 scope validator and persist first-step latch atomically with assignment. Expose assignment only after this gate passes. Freeze full StepDefinition; random private attempt token, semantic version 1; step remains pending until start. Token allows only own start/activity/checkpoint, not edits/acceptance/review. Direct worker envelopes use expected_revision=None/ownership_version=None; capability matrix forbids extra domain tokens. Explicit retirement of claimed work records logical cancellation/fences its token, rejects simultaneous edits to its stored frozen definition, preserves definition/evidence/attempt, and retains every unresolved resource and latch. For in-progress work validate cancellation to blocked, then blocked-to-retired separately in the same transaction; strict legacy direct in-progress-to-retired remains rejected. Completed history cannot retire. At this task keep start unavailable until Task 14 gates exist. Place test_same_step_one_assignment_winner in tests/test_service_attempts.py; replace Task 7 temporary no-assignment method in tests/test_service_sdk.py with test_plan_only_denies_assignment: tool now listed, plan-only assignment refused with no attempt/resources; keep this valid through final discovery, no feature flag.
- [ ] **Run pass:** Same module command, test injected event failure leaves no attempt/reservation; public data never contains token.
- [ ] **Local commit:** `git add adaptive_planning/attempts.py adaptive_planning/schema.py adaptive_planning/plans.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_attempts.py tests/test_service_sdk.py`; `git commit -m "feat: atomically assign frozen bounded work"`.

### Task 13: Global execution scope and lasting first-step latch

**Files:** Create `tests/test_service_scope.py`; modify `adaptive_planning/core.py`, `adaptive_planning/plans.py`, `adaptive_planning/attempts.py`, `tests/service_fixtures.py`.
**Interfaces:** Consumes Task 2 `validate_execution_scope`, `renew_authorization`, and Task 12 `assign_step`; provides scope regression coverage and TEST-ONLY `seed_accepted_result(fixture: ServiceFixture, plan_id: PlanId, step_id: StepId, artifact: ArtifactRef) -> SeededResult`; `SeededResult(plan_id: PlanId, step_id: StepId, attempt_id: AttemptId, result_version: ResultVersion, artifact: ArtifactRef)` lives in tests/service_fixtures.py.

- [ ] **RED:** `test_plan_only_no_assignment` no resources or attempts created; `test_first_step_global_latch_survives_every_transition` pure transition matrix plus existing assignment/restart/takeover gates preserve one logical-step winner; release/replace integration belongs Tasks 17/21; `test_first_step_renewal_accounts_for_assigned_work` derives latch from assigned work (still pending graph), rejects two claimed logical steps. `test_scope_change_requires_grounded_note` same stored-note ordinary revision/resume cannot renew; explicit AuthorizationChange with grounded note derives interval/baseline/claimed server-side, even for same mode.

```python
def test_scope_change_requires_grounded_note(self):
    p = self.f.create("first-step",creation_id="scope-plan")
    before = self.f.service.read_plan(p.current_metadata.plan_id).draft
    revision = self.f.command("revise_plan",{"draft":before,"reason":"resume same instructions","authorization_change":None,"retire_steps":()},receipt=p)
    updated = self.f.service.execute(revision)
    after = self.f.service.read_plan(p.current_metadata.plan_id).draft.authorization
    self.assertEqual(after,before.authorization)
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("revise_plan",{"draft":replace(before,authorization=replace(after,claimed="b")),"reason":"tamper","authorization_change":None,"retire_steps":()},receipt=updated))

def test_first_step_renewal_accounts_for_assigned_work(self):
    self.f.assign(self.p,"a")
    before=self.f.service.read_plan(self.plan_id).draft
    change=AuthorizationChange("first-step","synthetic actual authorization assertion")
    updated=self.f.service.execute(self.f.command("revise_plan",{"draft":before,"reason":"explicit mode change","authorization_change":change,"retire_steps":()},receipt=self.p))
    self.assertEqual(self.f.service.read_plan(self.plan_id).draft.authorization.claimed,"a")
    with self.assertRaises(DomainError):
        self.f.assign(updated,"b")
    other=self.f.create(creation_id="two-active")
    self.f.assign(other,"a")
    self.f.assign(other,"b")
    current=self.f.service.read_plan(other.current_metadata.plan_id).draft
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("revise_plan",{"draft":current,"reason":"explicit mode change","authorization_change":change,"retire_steps":()},receipt=other))
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_scope -v`; missing service enforcement.
- [ ] **Implement:** Scope latch resides on plan authorization, never attempt ownership. The test-only seed lazily imports Store/core and requires a temporary synthetic fixture with complete-task scope. It uses Store.write/schema/core to insert coherent synthetic historical accepted attempt, frozen definition, passing checkpoint/evidence and matching distinct-reviewer historical review with event sequence, result-version links and materialized snapshot/event/revision with same-plan FKs; no production command/bypass, no future accept_step call. Validate the assembled history and preserve scope. Historical baseline completions do not consume new interval; do not rewrite baseline during resume. Preserve original legacy v1-to-v2 interval semantics. No server operation claims a human approval occurred.
- [ ] **Run pass:** Module plus existing `TestPublishScopeAndClaims` acceptance 1, 2, 3, 5, 7 via full unittest discovery pass; latch assertions remain meaningful, no weakened tests.
- [ ] **Local commit:** `git add adaptive_planning/core.py adaptive_planning/plans.py adaptive_planning/attempts.py tests/service_fixtures.py tests/test_service_scope.py`; `git commit -m "feat: enforce global authorization interval boundaries"`.

### Task 14: Accepted output availability and semantic start/activity

**Files:** Create `tests/test_service_availability.py`; modify `adaptive_planning/attempts.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Consumes `require_attempt`, `Availability`, `conflicting_uses`, test-only `seed_accepted_result`. Provides `bind_availability(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `start_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `report_activity(connection: sqlite3.Connection, command: Command) -> MutationReceipt`. Bind operation requires orchestrator; start/activity require own attempt or current orchestrator plus its token for relay.

- [ ] **RED:** `test_dependents_need_final_checked_base_bindings` seeds coherent accepted history through Task 13 fixture (not future acceptance); accepted prerequisite alone does not start consumer; wrong result version/missing binding fails; all prerequisites bound to final checked base with ancestor/merge/cherry-pick/equivalent digest/evidence passes. `test_worker_version_not_global_revision` another worker activity advances plan revision but correct own attempt version succeeds. `test_relay_records_origin_and_relay` attribution is preserved.

```python
def test_worker_version_not_global_revision(self):
    a = self.f.assign(self.p,"a")
    b = self.f.assign(self.p,"b")
    self.f.start(self.p,b)
    self.f.start(self.p,a)
    attempts = self.f.service.read_plan(self.plan_id).coordination.attempts
    self.assertEqual({a.state for a in attempts},{"active"})

def test_dependents_need_final_checked_base_bindings(self):
    artifact=ArtifactRef("observation","synthetic-observation","v1",None,"durable measurement")
    result=seed_accepted_result(self.f,self.plan_id,"a",artifact)
    consumer=self.f.assign(self.p,"c")
    with self.assertRaises(DomainError):
        self.f.start(self.p,consumer)
    evidence=Evidence("input available",artifact,None,"read durable observation","passed",(),())
    binding=Availability("a",result.result_version,"observation","observed-base",None,evidence)
    self.f.service.execute(self.f.command("bind_availability",{"bindings":(binding,),"checked_base":"observed-base"},receipt=self.p,attempt=consumer))
    self.f.start(self.p,consumer)
    view=self.f.service.read_plan(self.plan_id)
    self.assertEqual(next(a.state for a in view.coordination.attempts if a.step_id=="c"),"active")
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_availability -v`; expected missing start/availability.
- [ ] **Implement:** Check generation/token before replay; NEW-command attempt-version/frozen-definition/elapsed-inactivity checks follow replay; increment only own attempt version for semantic updates, merge into current snapshot. Start sets attempt active and step in-progress; activity updates actual observation/timestamp and global revision, no fabricated heartbeat. Orchestrator refreshes public metadata and uses bounded CAS retry after reread/reconciliation; no separate structural revision is introduced. Missing/invalidation/hold blocks start even after assignment. Host check establishes integration; service validates recorded attestation only.
- [ ] **Run pass:** Same module command; all equivalent methods validate required fields; malformed attestations fail atomically.
- [ ] **Local commit:** `git add adaptive_planning/attempts.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_availability.py`; `git commit -m "feat: require accepted output availability before worker start"`.

### Task 15: Durable attributed checkpoints and immediate provisional holds

**Files:** Create `adaptive_planning/checkpoints.py`, `tests/test_service_checkpoints.py`; modify `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Consumes `require_attempt`, `Checkpoint`, `validate_service_graph`. Provides `record_checkpoint(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `affected_closure(snapshot: dict[str,JSONValue], steps: tuple[StepId,...]) -> tuple[StepId,...]`, `install_holds(connection: sqlite3.Connection, plan_id: PlanId, checkpoint_id: str, reassessment: Reassessment) -> None`.

- [ ] **RED:** `test_checkpoint_evidence_outcome_reassessment` evidence-free pass, missing reassessment and 51 evidence items fail without truncation; failed verification produces submitted/blocked rather than complete; essential evidence/findings survive restart. `test_material_holds_commit_with_checkpoint` uses `draft(steps=(step("a"), step("b"), step("c", ("b",)), step("d", ("c",))))`; active `b`, consumer `c` and transitive consumer `d` are held in same commit before proposer `a` review; test immediate concurrent readiness/start cannot slip through; concurrent acceptance coverage belongs Task 18. Unbounded effects hold new assign/start across plan (acceptance exercised in Task 18); unchanged explicit reason creates no hold. `test_interleaved_sibling_updates_preserved` no lost evidence. `test_checkpoint_retry_after_version_increment` exact successful checkpoint retry after its own attempt version and sibling plan revision advance returns original receipt with one checkpoint/event; later fenced token replay rejects in Task 21.

```python
def test_checkpoint_retry_after_version_increment(self):
    assigned = self.f.assign(self.p)
    active = self.f.start(self.p,assigned)
    command = self.f.command("record_checkpoint",{"checkpoint":checkpoint()},receipt=self.p,attempt=active,key="lost-checkpoint")
    first = self.f.service.execute(command)
    self.f.start(self.p,self.f.assign(self.p,"b"))
    retry = self.f.service.execute(command)
    self.assertTrue(retry.replayed)
    self.assertEqual(retry.committed,first.committed)
    self.assertEqual(sum(e.kind == "record_checkpoint" for e in self.f.service.read_events(self.plan_id)),1)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_checkpoints -v`; missing checkpoint/hold.
- [ ] **Implement:** Atomically append checkpoint/evidence, submit attempt, materialize status and holds, record origin/relay. Valid failed/blocker results retain reservations. Snapshot evidence bounded without discarding durable events. Check current epoch/generation and operation-authorizing capability first; exact committed replay returns BEFORE new-command frozen-definition/lifecycle/CAS/attempt-version checks. Fenced tokens always fail; stale but unfenced ownership does not turn replay into new progress. Preserve the stale recovery-checkpoint exception in Task 21. Submitting never satisfies dependency.
- [ ] **Run pass:** Same module command; injected failure between checkpoint and hold leaves neither visible.
- [ ] **Local commit:** `git add adaptive_planning/checkpoints.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_checkpoints.py`; `git commit -m "feat: publish checkpoints with atomic adaptive holds"`.

### Task 16: Orchestrator reassessment resolution and invalidated outputs

**Files:** Create `tests/test_service_holds.py`; modify `adaptive_planning/checkpoints.py`, `adaptive_planning/plans.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Consumes `affected_closure`, `install_holds`, `revise_plan`, `retire_step`, test-only `seed_accepted_result`. Provides `resolve_reassessment(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `invalidate_result(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `resolve_invalidation(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, internal `apply_result_invalidation(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId, result_version: ResultVersion, reason: str, evidence: tuple[Evidence,...]) -> str` returning invalidation ID. Also provides internal `invalidation_review_satisfied(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId, attempt_id: AttemptId, definition_version: DefinitionVersion, artifact: ArtifactRef, after_sequence: int | None) -> bool`: query existing review rows with scoped artifact/frozen-definition matches; a non-null sequence demands review after it. Task 16 reads coherent historical review rows seeded in Task 13, without depending on future record_review or accept_step APIs.

- [ ] **RED:** `test_held_sibling_and_transitive_consumers` resolution preserves sibling/descendant holds and blocks assign/start while unaffected proposer remains unheld; acceptance integration belongs Task 18. `test_corrective_prerequisite_requires_graph_revision` prose missing prerequisite cannot unlock old graph; applying valid draft installs actual dependency. `test_seeded_result_invalidation_holds_consumers` uses seed_accepted_result; `test_reject_requires_reason_and_preserves_history` uses test-only seeded accepted history, completed history stays immutable; invalidation holds consumers and rejects old availability.

```python
def test_held_sibling_and_transitive_consumers(self):
    p = self.f.create(steps=(step("a"),step("b"),step("c",("b",))),creation_id="hold-plan")
    a = self.f.start(p,self.f.assign(p,"a"))
    self.f.start(p,self.f.assign(p,"b"))
    self.f.submit(p,a,checkpoint(affected=("b",)))
    readiness = {s.step_id:s for s in self.f.service.list_ready(p.current_metadata.plan_id)}
    self.assertFalse(readiness["b"].ready)
    self.assertFalse(readiness["c"].ready)

def test_seeded_result_invalidation_holds_consumers(self):
    artifact=ArtifactRef("observation","synthetic-observation","v1",None,"durable measurement")
    result=seed_accepted_result(self.f,self.plan_id,"a",artifact)
    self.f.service.execute(self.f.command("invalidate_result",{"step_id":"a","result_version":result.result_version,"reason":"synthetic invalidated assumption","evidence":checkpoint().evidence},receipt=self.p))
    view=self.f.service.read_plan(self.plan_id)
    self.assertEqual(next(s["status"] for s in view.legacy_snapshot["steps"] if s["id"]=="a"),"complete")
    self.assertFalse(next(s.ready for s in self.f.service.list_ready(self.plan_id) if s.step_id=="c"))

def test_seeded_supersession_and_missing_proof_preserve_history(self):
    p=self.f.create(steps=(step("a",review_required=True),step("b",("a",),review_required=True),step("fix",review_required=True)),creation_id="seeded-invalidation")
    pid=p.current_metadata.plan_id
    artifact=checkpoint().artifact
    a=seed_accepted_result(self.f,pid,"a",artifact)
    b=seed_accepted_result(self.f,pid,"b",artifact)
    fix=seed_accepted_result(self.f,pid,"fix",replace(artifact,version="corrected"))
    before=copy.deepcopy(self.f.service.read_plan(pid).legacy_snapshot["steps"])
    invalid=self.f.service.execute(self.f.command("invalidate_result",{"step_id":"a","result_version":a.result_version,"reason":"failed assumption","evidence":checkpoint("failed").evidence},receipt=p))
    base={"invalidation_id":invalid.committed["invalidation_id"],"affected_step_id":"b","disposition":"revalidated","corrective_step_id":None,"corrected_result_version":b.result_version,"evidence":checkpoint().evidence,"reason":"recheck"}
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("resolve_invalidation",base,receipt=p))
    # Existing passing review predates invalidation, so cannot revalidate.
    corrected={**base,"disposition":"superseded","corrective_step_id":"fix","corrected_result_version":fix.result_version,"evidence":checkpoint(artifact_version="corrected").evidence}
    self.f.service.execute(self.f.command("resolve_invalidation",corrected,receipt=p))
    view=self.f.service.read_plan(pid)
    self.assertEqual(view.legacy_snapshot["steps"],before)
    self.assertEqual(view.coordination.result_versions["b"],b.result_version)
    self.assertTrue(any(h.step_id=="a" for h in view.coordination.holds))
    self.assertFalse(any(h.step_id=="b" and h.source_id==base["invalidation_id"] for h in view.coordination.holds))
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_holds -v`; missing resolution.
- [ ] **Implement:** Current orchestrator and plan CAS classify material effects; apply valid revised graph transactionally, reject with reason, or persist narrowed/unbounded holds. Resolution never alters active frozen definition silently. Explicit retire_steps reuses Task 12 cancellation/retirement helper, including evidence-bearing blocked steps; preserve evidence prefix/check/frozen attempt and unresolved resources/latch. Add test_evidence_bearing_retirement_preserves_history here for failed checkpoint; failed-review variant belongs Task 17 before Task 19. invalidate_result is current-orchestrator-only with nonempty evidence, atomically records invalidation plus producer-self hold and transitive consumer holds including accepted consumers; preserves accepted history. resolve_invalidation clears ONLY named affected-step hold (producer may be the named step): revalidated requires accepted artifact passing checks/matching required PASSED review recorded AFTER the invalidation and records validity without history rewrite; superseded requires distinct accepted corrective step/result/review, links it and leaves old result unusable (never substitute different-step UUID into result_versions[old_step]); replanned requires pending/blocked consumer actual revised definition removes invalid prerequisite/targets valid corrective result, no active frozen edit. Resolution evidence must name the exact revalidated or corrective artifact/version; generic passing evidence for another artifact is rejected. Missing/false proof rejects atomically. Producer hold also requires its own revalidated or superseded disposition; its old result stays invalid after supersession and only explicit revalidation restores validity. Repeat other affected steps individually. Future dependencies on invalid old results remain blocked and must explicitly target valid corrections. Late evidence never auto-invalidates/accepts; orchestrator invokes invalidate_result. Invalidated accepted outputs are records, not deletions; corrective work must resolve consumer holds. Dashboard identifies affected running workers; instructions require safe-boundary relay, no forced host interrupt promise.
- [ ] **Run pass:** Same module command and checkpoint module pass; bounded closure computed from authoritative graph.
- [ ] **Local commit:** `git add adaptive_planning/checkpoints.py adaptive_planning/plans.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_holds.py`; `git commit -m "feat: resolve adaptive holds without losing accepted history"`.

### Task 17: Artifact-bound per-task review and exclusive verification use

**Files:** Create `adaptive_planning/reviews.py`, `tests/test_service_reviews.py`; modify `tests/service_fixtures.py`, `adaptive_planning/attempts.py`, `adaptive_planning/resources.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Consumes `conflicting_uses`, `require_orchestrator`, `Review`, `retire_step`. Provides `reopen_attempt(connection: sqlite3.Connection, command: Command) -> MutationReceipt` in attempts.py, `record_review(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `reserve_verification(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `release_resource_use(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `review_satisfied(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId, attempt_id: AttemptId, definition_version: DefinitionVersion, artifact: ArtifactRef) -> bool`.
Provides TEST-ONLY `seed_store_hold(fixture: ServiceFixture, reason: str) -> None`: through Store.write/schema insert a coherent synthetic restore-inventory store_hold_reasons record in a temporary test store, without grants, resource release or production bypass. This fixture permits the Task 17 hold guard test before Task 23 implements the actual restore command.

- [ ] **RED:** `test_review_bound_to_current_artifact` matching v1 review satisfied, `review_satisfied(...,v2)` false; implementer/reviewer same label refused, findings durable. Acceptance rejection for v2 belongs Task 18. `test_review_handoff_never_frees_exclusive_workspace` read-only immutable artifact needs no write reservation; verifier tests use separate workspace or confirmed-stopped handoff, third assignment remains blocked throughout; implementer cannot resume declared write use simultaneously. Release without stopped confirmation retains obligation/latch. `test_writable_verification_rejected_during_store_hold` uses a seeded accepted artifact/open invalidation and synthetic store hold; registration remains possible, writable reservation fails held without use/event/metadata changes, and immutable review recording remains possible. Task 23 repeats this through actual backup/restore and orphan inventory.

```python
def test_review_bound_to_current_artifact(self):
    p = self.f.create(steps=(step("a",review_required=True),),creation_id="review-plan")
    a = self.f.start(p,self.f.assign(p))
    value = checkpoint()
    self.f.submit(p,a,value)
    review = Review("reviewer",value.artifact,"passed",(),value.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=p))
    view = self.f.service.read_plan(p.current_metadata.plan_id)
    self.assertEqual(view.coordination.reviews[0].review.artifact,value.artifact)
    self.assertEqual(view.coordination.reviews[0].attempt_id,view.coordination.attempts[0].attempt_id)
    other=self.f.create(steps=(step("a",review_required=True),),creation_id="other-review-plan")
    self.f.start(other,self.f.assign(other))
    other_view=self.f.service.read_plan(other.current_metadata.plan_id)
    attempt=other_view.coordination.attempts[0]
    satisfied=self.f.store.read(lambda db: review_satisfied(db,other_view.plan_id,"a",attempt.attempt_id,attempt.definition_version,value.artifact))
    self.assertFalse(satisfied)

def test_writable_verification_rejected_during_store_hold(self):
    value=checkpoint()
    accepted=seed_accepted_result(self.f,self.plan_id,"a",value.artifact)
    self.f.service.execute(self.f.command("invalidate_result",{"step_id":"a","result_version":accepted.result_version,"reason":"synthetic revalidation","evidence":value.evidence},receipt=self.p))
    seed_store_hold(self.f,"synthetic incomplete restore inventory")
    rid=self.f.register(workspace("/synthetic/held-review",703),self.p)
    before=self.f.service.read_plan(self.plan_id)
    events=self.f.service.read_events(self.plan_id)
    with self.assertRaises(DomainError) as error:
        self.f.service.execute(self.f.command("reserve_verification",{"step_id":"a","verifier":"reviewer","context":context(rid),"handoff_from":None,"stopped_reason":None},receipt=self.p))
    self.assertEqual(error.exception.code,"held")
    after=self.f.service.read_plan(self.plan_id)
    self.assertEqual(after.metadata,before.metadata)
    self.assertEqual(after.coordination.reservations,before.coordination.reservations)
    self.assertEqual(self.f.service.read_events(self.plan_id),events)
    review=Review("reviewer",value.artifact,"passed",(),value.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=self.p))
    self.assertTrue(self.f.service.read_service_info().store_hold)

def test_failed_review_retirement_preserves_evidence(self):
    p=self.f.create("first-step",steps=(step("a",review_required=True),),creation_id="failed-review")
    workspace_id=self.f.register(workspace("/synthetic/failed-review",500),p)
    assigned=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(workspace_id)},receipt=p))
    active=self.f.start(p,assigned)
    value=checkpoint()
    self.f.submit(p,active,value)
    review=Review("reviewer",value.artifact,"failed",("synthetic defect",),value.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=p))
    before=self.f.service.read_plan(p.current_metadata.plan_id)
    self.f.service.execute(self.f.command("revise_plan",{"draft":before.draft,"reason":"replace unsuccessful review route","authorization_change":None,"retire_steps":(RetirementRequest("a","preserve failed result"),)},receipt=p))
    after=self.f.service.read_plan(p.current_metadata.plan_id)
    self.assertEqual(after.legacy_snapshot["steps"][0]["status"],"retired")
    self.assertEqual(after.legacy_snapshot["steps"][0]["evidence"],before.legacy_snapshot["steps"][0]["evidence"])
    self.assertEqual(after.coordination.reviews,before.coordination.reviews)
    self.assertTrue(after.coordination.reservations)
    self.assertEqual(before.draft.authorization.claimed,"a")
    self.assertEqual(after.draft.authorization,before.draft.authorization)
    with self.assertRaises(DomainError) as error:
        self.f.service.execute(self.f.command("report_activity",{"origin_worker":"worker-a","relay":None,"observed_activity":"late mutation"},receipt=p,attempt=active))
    self.assertEqual(error.exception.code,"fenced")
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_reviews -v`; missing review/use APIs.
- [ ] **Implement:** Orchestrator relays review, never shares worker mutation capability. Review records artifact identity/version and verification actor/resources before dispatch. Before creating/activating writable verification use or changing writer handoff, reserve_verification checks store_hold and rejects held atomically with no use/event/metadata change; recording observed immutable review evidence and read-only inspection remain available. Do not treat an accepted result's invalidation hold alone as a verification prohibition, since revalidation requires review; ordinary resource conflicts still apply. Read-only artifact review creates no writable reservation. Accepted-artifact review is allowed only while that result/consumer has an open invalidation; append the new review without changing terminal attempt/completed-step state, and failed review keeps holds. Revalidation never reuses a pre-invalidation review. Handoff changes active writer inside same retained reservation, after confirmation of prior stopped use; use IDs remain attributable. Scope every step-keyed query to plan and validate current attempt/frozen definition; ReviewView preserves provenance. `test_review_bound_to_current_artifact` additionally rejects plan A/a review satisfying plan B/a even matching artifact. Review labels enforce only recorded distinction; do not certify model independence. Failed review retains submitted attempt/current token and records failed review/blocked logical step; rejected is reserved for explicit resolve_attempt reject. reopen_attempt requires current orchestrator, reason, unchanged definition/scope/resources/no hold: submitted->active, same ID/token/resources/latch, own version+1, step in-progress. Reacquire existing implementation writer_active only after all conflicting verification/handoff uses are explicitly stopped/released; same attempt ID cannot bypass them. No terminal/stale/held/accepted reopening. Prior checkpoints/reviews stay immutable but cease to be current; resubmission carries new artifact version and fresh review. test_failed_review_retirement_preserves_evidence exercises revise retire_steps before Task 19; evidence/findings/frozen attempt, unresolved resources and latch survive. No release on submission/review.
- [ ] **Run pass:** Same module command; independent connection tests cannot acquire workspace between handoff transitions.
- [ ] **Local commit:** `git add tests/service_fixtures.py adaptive_planning/reviews.py adaptive_planning/attempts.py adaptive_planning/resources.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_reviews.py`; `git commit -m "feat: bind review verdicts and preserve exclusive verification use"`.

### Task 18: Explicit acceptance, result versions, and derived plan state

**Files:** Create `tests/test_service_acceptance.py`; modify `tests/service_fixtures.py`, `adaptive_planning/attempts.py`, `adaptive_planning/plans.py`, `adaptive_planning/reviews.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Provides TEST-ONLY `seed_terminal_history(fixture: ServiceFixture, plan_id: PlanId, step_id: StepId, count: int) -> None`: Store.write inserts count coherent rejected attempts/frozen definitions/failed checkpoints/artifact-bound failed reviews with two8KB findings per review and events, same-plan FKs, no current grant/use/hold or graph evidence mutation; reprojects summary/page without discarding authority. Consumes `current_attempt`, `review_satisfied`, `CoordinationView`; orchestrator acceptance validates scoped current attempt/version/definition but never requires or permits a worker token. Provides `accept_step(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `derive_plan_status(view: PlanView) -> Literal['planning','running','stopped','blocked','complete']`.

- [ ] **RED:** `test_acceptance_requires_checkpoint_current_artifact_review_and_no_hold` rejects absent checkpoint, failed result, stale attempt, changed artifact, missing review and held step; pass records result version and complete step. `test_unaffected_proposer_accepted_while_sibling_held` checks concurrent accept cannot pass installed hold, proposer accepts with holds elsewhere, held active sibling rejects. `test_old_review_cannot_accept_changed_artifact` pins v1 review/v2 acceptance failure. `test_acceptance_keeps_unstopped_obligations` accepted logical prerequisite with unresolved writable use retains reservation and prevents global completion. `test_many_historical_rows_do_not_block_safety_reducers` seeds near-limit graph and many terminal histories, releases/clears holds successfully and pages all retained records; `test_failed_review_same_attempt_fix_handoff` proves stopped verifier handoff before reopen; `test_status_precedence_and_independent_running_work` explicit stop wins, blocked sibling with live independent attempt remains running, started no executable/live work blocked, pristine plan planning, all accepted/no obligations complete.

```python
def test_acceptance_keeps_unstopped_obligations(self):
    p=self.f.create(steps=(step("a"),),creation_id="single-writable-step")
    plan_id=p.current_metadata.plan_id
    resource_id=self.f.register(workspace("/synthetic/work",100),p)
    assigned=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(resource_id)},receipt=p))
    active=self.f.start(p,assigned)
    value=checkpoint()
    self.f.submit(p,active,value)
    self.f.accept(p,active,value)
    view=self.f.service.read_plan(plan_id)
    self.assertEqual(view.legacy_snapshot["steps"][0]["status"],"complete")
    self.assertTrue(view.coordination.reservations)
    self.assertNotEqual(view.status,"complete")
    use=view.coordination.reservations[0]
    self.f.service.execute(self.f.command("release_resource_use",{"use_id":use.use_id,"resource_ids":use.resource_ids,"reason":"synthetic recorded stopped use","stopped":True},receipt=p))
    self.assertEqual(self.f.service.read_plan(plan_id).status,"complete")

def test_failed_review_same_attempt_fix_handoff(self):
    p=self.f.create(steps=(step("a",review_required=True),),creation_id="fix-loop")
    rid=self.f.register(workspace("/synthetic/fix",600),p)
    assigned=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=p))
    active=self.f.start(p,assigned)
    v1=checkpoint()
    old=self.f.command("record_checkpoint",{"checkpoint":v1},receipt=p,attempt=active,key="fix-v1")
    self.f.service.execute(old)
    use=self.f.service.read_plan(p.current_metadata.plan_id).coordination.reservations[0]
    verification=self.f.service.execute(self.f.command("reserve_verification",{"step_id":"a","verifier":"reviewer","context":context(rid),"handoff_from":use.use_id,"stopped_reason":"implementation writer stopped"},receipt=p))
    vid=verification.committed["use_id"]
    failed=Review("reviewer",v1.artifact,"failed",("fix required",),v1.evidence,vid)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":failed},receipt=p))
    self.assertTrue(self.f.service.execute(old).replayed)
    reopen=self.f.command("reopen_attempt",{"reason":"authorized fix"},receipt=p,attempt=active)
    with self.assertRaises(DomainError) as error:
        self.f.service.execute(reopen)
    self.assertEqual(error.exception.code,"resource_conflict")
    other=self.f.create(steps=(step("a"),),creation_id="third-fix-writer")
    def third_writer():
        return self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=other))
    with self.assertRaises(DomainError):
        third_writer()
    self.f.service.execute(self.f.command("release_resource_use",{"use_id":vid,"resource_ids":(rid,),"reason":"verification stopped","stopped":True},receipt=p))
    with self.assertRaises(DomainError):
        third_writer()
    self.f.service.execute(self.f.command("reopen_attempt",{"reason":"authorized fix"},receipt=p,attempt=active))
    with self.assertRaises(DomainError):
        third_writer()
    v2=checkpoint(artifact_version="v2")
    resubmit=self.f.command("record_checkpoint",{"checkpoint":v2},receipt=p,attempt=active)
    self.assertEqual(resubmit.attempt_token,assigned.private.attempt_token)
    self.f.service.execute(resubmit)
    passed=Review("reviewer",v2.artifact,"passed",(),v2.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":passed},receipt=p))
    self.f.accept(p,active,v2)
    view=self.f.service.read_plan(p.current_metadata.plan_id)
    self.assertEqual(view.coordination.total_counts["attempts"],1)
    self.assertEqual(view.coordination.attempts[0].attempt_id,assigned.committed["attempt_id"])
    self.assertEqual(view.legacy_snapshot["steps"][0]["status"],"complete")

def test_single_accepted_node_invalidation_prevents_complete(self):
    p=self.f.create(steps=(step("a",review_required=True),),creation_id="invalid-last-node")
    a=self.f.start(p,self.f.assign(p))
    value=checkpoint()
    self.f.submit(p,a,value)
    review=Review("reviewer",value.artifact,"passed",(),value.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=p))
    self.f.accept(p,a,value)
    view=self.f.service.read_plan(p.current_metadata.plan_id)
    self.assertEqual(view.status,"complete")
    result=view.coordination.result_versions["a"]
    invalid=self.f.service.execute(self.f.command("invalidate_result",{"step_id":"a","result_version":result,"reason":"later failed assumption","evidence":checkpoint("failed").evidence},receipt=p))
    view=self.f.service.read_plan(p.current_metadata.plan_id)
    self.assertNotEqual(view.status,"complete")
    self.assertTrue(any(h.step_id=="a" and h.kind=="invalidation" for h in view.coordination.holds))
    resolution={"invalidation_id":invalid.committed["invalidation_id"],"affected_step_id":"a","disposition":"revalidated","corrective_step_id":None,"corrected_result_version":result,"evidence":value.evidence,"reason":"passing current artifact recheck"}
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("resolve_invalidation",resolution,receipt=p))
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=p))
    self.f.service.execute(self.f.command("resolve_invalidation",resolution,receipt=p))
    self.assertEqual(self.f.service.read_plan(p.current_metadata.plan_id).status,"complete")

def test_many_historical_rows_do_not_block_safety_reducers(self):
    p=self.f.create(steps=(step("a"),),creation_id="bounded-history")
    pid=p.current_metadata.plan_id
    initial=self.f.service.read_plan(pid).draft
    self.f.service.execute(self.f.command("revise_plan",{"draft":replace(initial,facts=tuple("x"*9216 for _ in range(1000))),"reason":"near-limit content","authorization_change":None,"retire_steps":()},receipt=p))
    rid=self.f.register(workspace("/synthetic/history",720),p)
    a=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=p))
    self.f.start(p,a)
    value=checkpoint()
    self.f.submit(p,a,value)
    self.f.accept(p,a,value)
    seed_terminal_history(self.f,pid,"a",2000)
    view=self.f.service.read_plan(pid)
    use=view.coordination.reservations[0]
    self.f.service.execute(self.f.command("release_resource_use",{"use_id":use.use_id,"resource_ids":use.resource_ids,"reason":"writer stopped","stopped":True},receipt=p))
    invalid=self.f.service.execute(self.f.command("invalidate_result",{"step_id":"a","result_version":view.coordination.result_versions["a"],"reason":"recheck output","evidence":value.evidence},receipt=p))
    resolution={"invalidation_id":invalid.committed["invalidation_id"],"affected_step_id":"a","disposition":"revalidated","corrective_step_id":None,"corrected_result_version":view.coordination.result_versions["a"],"evidence":value.evidence,"reason":"passing current artifact recheck"}
    self.f.service.execute(self.f.command("resolve_invalidation",resolution,receipt=p))
    cursor=None
    reviews=0
    while True:
        page=self.f.service.read_coordination(pid,after=cursor)
        reviews+=sum(i.kind=="review" for i in page.items)
        cursor=page.next_cursor
        if cursor is None:
            break
    self.assertEqual(reviews,2000)
    self.assertEqual(self.f.service.read_plan(pid).status,"complete")

```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_acceptance -v`; missing acceptance.
- [ ] **Implement:** Current orchestrator only; read current_attempt under the transaction without worker-capability authentication; validate checkpoint, attempt, evidence, matching artifact/review, prerequisites and reassessment resolution before complete. Stable accepted result UUID binds output references; reserve/release obligation semantics independent of logical acceptance. Release only listed confirmed-stopped uses with reason. Preserve explicit imported stop/block controls; no implicit Git integration record.
- [ ] **Run pass:** Same module command plus availability/holds/reviews pass; test_accepted_consumer_superseded_preserves_history and test_pending_consumer_replanned_dependency use real accepted corrective outputs; missing-proof rejection leaves all holds. test_live_accept_bind_start_and_invalidation repeats Task 14/16 seeded cases through actual checkpoint/accept/bind/start and material invalidation; independent consumer assignment follows acceptance, start still needs availability. Repeat live path in Task 20.
- [ ] **Local commit:** `git add tests/service_fixtures.py adaptive_planning/attempts.py adaptive_planning/plans.py adaptive_planning/reviews.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_acceptance.py`; `git commit -m "feat: gate explicit acceptance and derive truthful plan state"`.

### Task 19: Final-review gate and failed-review corrective graph

**Files:** Modify `adaptive_planning/reviews.py`, `adaptive_planning/plans.py`; add cases to `tests/test_service_reviews.py`, `tests/test_service_acceptance.py`.
**Interfaces:** Consumes `accept_step`, `revise_plan`, `record_review`, `retire_step`. Provides `validate_final_gate(draft: PlanDraft) -> None`; consumes existing revise/review/accept APIs rather than a second acceptance mechanism.

- [ ] **RED:** `test_final_review_not_circular_task_gate` implementation prerequisites accept without plan-wide final review; final-review step depends on accepted implementation, then its review/checkpoint/acceptance gates completion. `test_failed_final_review_corrective_history` failed findings retained, old review step explicitly retired, authorized corrective task added, new final gate depends on corrective plus accepted prior work and binds corrected artifact; old accepted history unchanged.

```python
def test_final_review_not_circular_task_gate(self):
    p = self.f.create(steps=(step("a"),step("final",("a",),final_gate=True)),creation_id="final-plan")
    a = self.f.start(p,self.f.assign(p))
    value = checkpoint()
    self.f.submit(p,a,value)
    self.f.accept(p,a,value)
    readiness = {s.step_id:s.ready for s in self.f.service.list_ready(p.current_metadata.plan_id)}
    self.assertTrue(readiness["final"])
    self.assertNotEqual(self.f.service.read_plan(p.current_metadata.plan_id).status,"complete")
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_reviews tests.test_service_acceptance -v`; expected missing final-gate validation.
- [ ] **Implement:** Represent final review as ordinary final_gate StepDefinition with artifact-bound evidence, not a plan-global prerequisite of all earlier acceptance. Reject malformed cycles via core. Retire failed gate via RevisePlanPayload.retire_steps=(RetirementRequest(old_id,reason),), reusing tested cancellation/retirement helper; preserve failed findings and prior accepted history; first-step policy still limits corrective work unless actual authorization changes.
- [ ] **Run pass:** Same two modules pass, asserted final complete remains blocked until new gate accepted and obligations clear.
- [ ] **Local commit:** `git add adaptive_planning/reviews.py adaptive_planning/plans.py tests/test_service_reviews.py tests/test_service_acceptance.py`; `git commit -m "feat: model final review and authorized corrective gates"`.

### Task 20: Repeat full three-client worker checkpoint interoperability

**Files:** Modify `adaptive_planning/transport.py`, `tests/test_service_interop.py`, `docs/clients/codex.md`, `docs/clients/antigravity.md`, `docs/clients/copilot.md`, `docs/operations.md`.
**Interfaces:** Exposes only tested full semantic operations from Tasks 11–19; Test-only `async run_worker_interop(service: PlanningService) -> tuple[Event,...]` starts three actual official-client sessions against ephemeral production app; SDK/installed clients submit actual worker checkpoints, not entire replacement snapshots.

- [ ] **RED:** `test_kind_capability_matrix` tests every orchestrator-only kind against attempt-only authority and extra-token rejection; `test_direct_worker_after_takeover_and_old_relay_fenced` pins worker/relay separation and accepted replay-only policy. `test_three_clients_full_worker_checkpoint_cycle` three distinct sessions register/assign/start bounded synthetic independent steps; each submits checkpoint with evidence/reassessment, orchestrator accepts, required review example passes; event origins survive interleaving. Repeat live accept/bind/start/invalidate checks; consumer missing availability still rejects. Plan-only still denies execution even though tools now exist.

```python
def test_three_clients_full_worker_checkpoint_cycle(self):
    events = asyncio.run(run_worker_interop(self.f.service))
    checkpoints = [e for e in events if e.kind == "record_checkpoint"]
    self.assertEqual({e.origin_worker for e in checkpoints},{"codex-worker","agy-worker","copilot-worker"})
    self.assertEqual(len(checkpoints),3)

def test_kind_capability_matrix(self):
    for kind in ORCHESTRATOR_KINDS:
        with self.subTest(kind=kind),self.assertRaises(DomainError) as error:
            require_kind_capabilities(kind,None,None,"synthetic-attempt-token")
        self.assertEqual(error.exception.code,"forbidden")
    for kind in WORKER_KINDS:
        with self.subTest(kind=kind),self.assertRaises(DomainError):
            require_kind_capabilities(kind,None,"synthetic-orchestrator-token","synthetic-attempt-token")

def test_direct_worker_after_takeover_and_old_relay_fenced(self):
    assigned=self.f.assign(self.p)
    active=self.f.start(self.p,assigned)
    relayed=replace(checkpoint(),relay="orchestrator")
    old_relay=self.f.command("record_checkpoint",{"checkpoint":relayed},receipt=self.p,attempt=active)
    current=self.f.service.execute(self.f.command("takeover",{"expected_ownership_version":self.p.current_metadata.ownership_version,"reason":"explicit takeover"},receipt=self.p))
    with self.assertRaises(DomainError) as error:
        self.f.service.execute(old_relay)
    self.assertEqual(error.exception.code,"fenced")
    direct=self.f.command("record_checkpoint",{"checkpoint":checkpoint()},receipt=self.p,attempt=active,key="direct-after-takeover")
    self.assertIsNone(direct.orchestrator_token)
    self.f.service.execute(direct)
    self.f.accept(current,active,checkpoint())
    self.assertTrue(self.f.service.execute(direct).replayed)
    with self.assertRaises(DomainError):
        self.f.service.execute(self.f.command("report_activity",{"origin_worker":"worker-a","relay":None,"observed_activity":"after acceptance"},receipt=current,attempt=active))
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_interop -v`; expected incomplete full-operation wiring.
- [ ] **Implement:** Each owning task registers its semantic tool only after its domain tests pass; this task verifies complete tested catalog, including resource/availability/review/hold/release operations; keep `PlanningService.execute` central. Docs distinguish orchestrator and attempt capability handling, read projections, and installation credential. Both negotiated protocols run full worker cycle through official SDK.
- [ ] **Run pass:** Module passes; each installed Codex, Antigravity and local Copilot surface lists tools, reads same plan, commits its own visible **finished-worker checkpoint**, then orchestrator accepts with applicable gate. Record actual versions/protocol/surface/plan/event IDs. Repeat close/reopen client, service restart and container recreation, preserving assignments/evidence/revisions/URLs and avoiding redispatch of accepted work. Task 10 planning event proof does not satisfy this gate.
- [ ] **Local commit:** `git add adaptive_planning/transport.py tests/test_service_interop.py docs/clients/codex.md docs/clients/antigravity.md docs/clients/copilot.md docs/operations.md`; `git commit -m "test: prove three-client coordinated checkpoint interoperability"`.

## Milestone 3 — Recovery, transfer, operations, instructions, and release

Working deliverable: restart versus restore are distinguishable, imported and stale work share recovery rules, clients follow the adaptive loop and outage policy, and all design acceptance checks have recorded evidence. No release until all three milestones pass.

### Task 21: Stale ownership, explicit recovery, relay, and late evidence

**Files:** Create `adaptive_planning/recovery.py`, `tests/test_service_recovery.py`; modify `adaptive_planning/attempts.py`, `adaptive_planning/checkpoints.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`, `adaptive_planning/dashboard.py`.
**Interfaces:** Consumes `require_attempt`, `install_holds`, `release_resource_use`, `retire_step`, `cancel_attempt`. Provides `sweep_stale(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `mark_stale(connection: sqlite3.Connection, plan_id: PlanId, now: datetime, threshold_seconds: int=900) -> tuple[AttemptId,...]`, `resolve_attempt(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `append_late_evidence(connection: sqlite3.Connection, command: Command) -> MutationReceipt`. Provide authenticated orchestrator-only sweep_stale for target plan; reads NEVER mutate. Dashboard computes overdue warning only. Only explicit sweep_stale persists stale; one event listing all newly stale attempts and one revision per changed sweep batch; each affected attempt version increments and preserves prior state/token/resources, repeat sweep no new event/revision. No unrelated mutation or read performs a hidden sweep. NEW progress rejects elapsed inactivity with code stale even before sweep; heartbeat cannot reset overdue ownership. Unfenced late recovery checkpoint follows explicit sweep with latest semantic version. Never releases uses. Elapsed inactivity applies to assigned, active and submitted attempts; a submitted result awaiting review does not bypass the gate. confirm-owner must record actual current ownership observation, restore prior_state, update last_activity to that explicit observation time and advance the semantic attempt version, without changing tokens, checkpoints, latch, unrelated holds or reservations. Only its stale/recovery hold clears.

- [ ] **RED:** `test_stale_heartbeat_does_not_reactivate` advance FakeClock 901 seconds then authenticated sweep_stale -> stale/hold, prior state retained; heartbeat cannot reactivate. `test_isolated_replacement_keeps_old_resources` replace workspace X with Y; old X/shared DB remain blocked to third plan; old token fenced. `test_checkpoint_retry_fenced_after_replacement` replays Task 15 key after replacement and rejects old token. `test_stale_checkpoint_and_fenced_late_evidence` unfenced stale attempt appends recovery checkpoint without acceptance/hold release; fenced evidence only via current orchestrator, append-only, cannot progress. `test_relay_no_fabricated_activity` worker/relay evidence preserved, no checkpoint reports unobserved liveness. `test_submitted_overdue_requires_explicit_confirmation` rejects reopen/accept before sweep and succeeds only after sweep plus actual ownership confirmation restores submitted state; reopening/resubmission then retains one attempt.

```python
def test_submitted_overdue_requires_explicit_confirmation(self):
    active=self.f.start(self.p,self.f.assign(self.p))
    self.f.submit(self.p,active,checkpoint())
    self.f.clock.advance(901)
    for kind,payload in (("reopen_attempt",{"reason":"fix returned result"}),("accept_step",{"step_id":"a","artifact":checkpoint().artifact,"stopped_resource_ids":(),"stopped_reason":None})):
        with self.assertRaises(DomainError) as error:
            self.f.service.execute(self.f.command(kind,payload,receipt=self.p,attempt=active))
        self.assertEqual(error.exception.code,"stale")
    self.f.service.execute(self.f.command("sweep_stale",{},receipt=self.p))
    before=self.f.service.read_plan(self.plan_id).coordination.attempts[0]
    self.assertEqual(before.prior_state,"submitted")
    self.f.service.execute(self.f.command("resolve_attempt",{"resolution":"confirm-owner","reason":"synthetic observed worker confirms current ownership","next_step_state":"pending","stopped_resource_ids":(),"replacement_assignee":None,"replacement_context":None,"attached_context":None},receipt=self.p,attempt=active))
    confirmed=self.f.service.read_plan(self.plan_id).coordination.attempts[0]
    self.assertEqual(confirmed.state,"submitted")
    self.assertGreater(confirmed.version,before.version)
    self.f.service.execute(self.f.command("reopen_attempt",{"reason":"observed worker dispatched for correction"},receipt=self.p,attempt=active))
    value=checkpoint(artifact_version="v2")
    self.f.submit(self.p,active,value)
    self.f.accept(self.p,active,value)
    view=self.f.service.read_plan(self.plan_id)
    self.assertEqual(len(view.coordination.attempts),1)
    self.assertEqual(view.coordination.attempts[0].state,"accepted")

def test_stale_heartbeat_does_not_reactivate(self):
    active=self.f.start(self.p,self.f.assign(self.p))
    before=self.f.service.read_plan(self.plan_id)
    self.f.clock.advance(901)
    self.assertEqual(self.f.service.read_plan(self.plan_id).metadata,before.metadata)
    with self.assertRaises(DomainError) as error:
        self.f.service.execute(self.f.command("report_activity",{"origin_worker":"worker-a","relay":None,"observed_activity":"returned"},receipt=self.p,attempt=active))
    self.assertEqual(error.exception.code,"stale")
    self.assertEqual(self.f.service.read_plan(self.plan_id).metadata,before.metadata)
    self.f.service.execute(self.f.command("sweep_stale",{},receipt=self.p))
    swept=self.f.service.read_plan(self.plan_id)
    self.assertEqual(swept.coordination.attempts[0].state,"stale")
    self.assertEqual(swept.coordination.attempts[0].prior_state,"active")
    self.f.service.execute(self.f.command("sweep_stale",{},receipt=self.p))
    self.assertEqual(self.f.service.read_plan(self.plan_id).metadata,swept.metadata)
    recovery=self.f.submit(self.p,active,checkpoint())
    self.assertTrue(recovery.committed)
    self.assertNotEqual(self.f.service.read_plan(self.plan_id).status,"complete")
    self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.attempts[0].state,"stale")
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_recovery -v`; missing recovery.
- [ ] **Implement:** Resolve confirm-owner/reject/release/replace explicitly, preserve every old attempt and independent unresolved use. Replacement declares resources and cannot reuse unresolved shared resource; current-orchestrator confirmation and explicit stopped-use/safety reason required to release. Released step state explicit; next_step_state retired reuses retire_step and its validated logical cancellation/retirement sequence. Latch unchanged. No host cleanup/interruption. Normal reopen retains epoch/tokens/current state; stale evaluation uses clock only.
- [ ] **Run pass:** Module plus attempts/reviews/checkpoint tests pass; separate connection cannot acquire X during Y replacement; late notes excluded from accepted result.
- [ ] **Local commit:** `git add adaptive_planning/recovery.py adaptive_planning/attempts.py adaptive_planning/checkpoints.py adaptive_planning/app.py adaptive_planning/transport.py adaptive_planning/dashboard.py tests/test_service_recovery.py`; `git commit -m "feat: recover stale ownership without releasing unknown work"`.

### Task 22: Explicit legacy authority handoff and recovery import

**Files:** Create `adaptive_planning/imports.py`, `tests/test_service_imports.py`; modify `adaptive_planning/recovery.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`.
**Interfaces:** Consumes `validate_published_snapshot`, `mark_stale`, `resolve_attempt`, `issue_orchestrator`, `replay_request`, `register_claim`. Provides `import_plan(connection: sqlite3.Connection, command: Command) -> MutationReceipt`, `create_unresolved_attempt(connection: sqlite3.Connection, plan_id: PlanId, step_id: StepId, context: ExecutionContext | None, *, source: str) -> AttemptId` in recovery, reused by import and restore. Also provides private `resolve_context_claims(connection: sqlite3.Connection, context: ExecutionContextClaims) -> ExecutionContext` and test-only `active_legacy_snapshot() -> dict[str,JSONValue]`: copy legacy_proposal(), set first step step-1 to in-progress/status running, publish via prepare_publication at injected2026-10-06T00:00:00Z/UUID00000000-0000-4000-8000-000000000001/revision0/authorization_note="synthetic legacy authorization"/limits10MB+100. Import's committed public receipt includes `source_directory_id: ResourceId` after canonical resolution.

- [ ] **RED:** `test_first_plan_import_by_claim` starts with zero plans/resources/grants; import yields one plan, canonical source identity and initial grant; exact retry and new-key retry by resolved ID share that plan. Both-present/neither-present source inputs reject; validation/conflict failures leave no plan, grant, mapping, or orphan registration. `test_source_identity_not_digest_tuple_identity` same `(host_id, registered_source_directory_id, original_task_id)` with new key identical bytes yields same plan; changed digest/tuple gives handoff conflict, never duplicate/overwrite. `test_active_import_preserves_latch_and_holds` v2 source revision 17 with `scope.sinceRevision=12` imports at service revision 17, retains scope 12/baseline/claimed and original tuple provenance; next mutation is revision 18. In-progress import gets unresolved attempt without worker token, conservative reservation; missing required identity installs store-wide safety hold identifying each unresolved imported attempt, protecting other plans too. Conflicting resource rejects all import records. `test_import_bootstrap_lost_response_current_grant_only` lost response/restart/key and source-new-key retries retain initial current grant; takeover makes both retries private=None, old epoch rejected. Changed content remains atomic handoff_conflict. `test_corrupt_and_unmigrated_v1_rejected` corrupt input rejected, explicit legacy v1 migration first.

```python
def test_first_plan_import_by_claim(self):
    with TemporaryDirectory() as directory:
        fresh=make_service(Path(directory))
        self.assertEqual(fresh.service.list_plans(),())
        self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM resources").fetchone()[0]),0)
        self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM capabilities").fetchone()[0]),0)
        info=fresh.service.read_service_info()
        source=prepare_publication(legacy_proposal(),None,timestamp="2026-10-06T00:00:00Z",generation="00000000-0000-4000-8000-000000000001",expected_revision=0,expected_generation=None,authorization_note="synthetic legacy authorization",max_file_bytes=10*1024*1024,max_history=100)
        payload={"host_id":info.host_id,"source_directory_id":None,"source_directory_claim":replace(workspace("/synthetic/legacy",300),host_id=info.host_id),"original_task_id":source["taskId"],"original_snapshot":source,"provenance":DocumentRef("legacy.json",None,None,"v1",None),"writers_stopped":"confirmed synthetic handoff","known_contexts":{}}
        original=fresh.command("import_plan",payload,key="first-import")
        first=fresh.service.execute(original)
        self.assertIsNotNone(first.private)
        self.assertTrue(first.private.orchestrator_token)
        self.assertEqual(len(fresh.service.list_plans()),1)
        self.assertEqual(fresh.service.execute(original).private,first.private)
        by_id={**payload,"source_directory_id":first.committed["source_directory_id"],"source_directory_claim":None}
        retry=fresh.service.execute(fresh.command("import_plan",by_id,key="by-id-retry"))
        self.assertEqual(retry.current_metadata.plan_id,first.current_metadata.plan_id)
        self.assertEqual(retry.private,first.private)
        self.assertEqual(fresh.store.read(lambda db: db.execute("SELECT count(*) FROM resources").fetchone()[0]),1)

def test_source_identity_not_digest_tuple_identity(self):
    source=None
    generation="00000000-0000-4000-8000-000000000001"
    for revision in range(1,18):
        source=prepare_publication(legacy_proposal(),source,timestamp="2026-10-06T00:00:00Z",generation=generation,expected_revision=revision-1,expected_generation=None if revision==1 else generation,authorization_note="synthetic legacy authorization" if revision in (1,12) else None,max_file_bytes=10*1024*1024,max_history=100)
    directory_id=self.f.register(workspace("/synthetic/legacy",300),self.p)
    payload={"host_id":"synthetic-host","source_directory_id":directory_id,"source_directory_claim":None,"original_task_id":source["taskId"],"original_snapshot":source,"provenance":DocumentRef("legacy.json",None,None,"v17",None),"writers_stopped":"confirmed synthetic handoff","known_contexts":{}}
    original=self.f.command("import_plan",payload,key="import-one")
    first=self.f.service.execute(original)
    retry=self.f.service.execute(self.f.command("import_plan",payload,key="import-two"))
    self.assertEqual(first.current_metadata.plan_id,retry.current_metadata.plan_id)
    self.assertEqual(first.current_metadata.revision,17)
    self.assertEqual(first.private.orchestrator_token,retry.private.orchestrator_token)
    reopened=make_service(self.f.directory,self.f.clock)
    self.assertEqual(reopened.service.execute(original).private.orchestrator_token,first.private.orchestrator_token)
    takeover=reopened.command("takeover",{"expected_ownership_version":first.current_metadata.ownership_version,"reason":"synthetic explicit takeover"},receipt=first)
    reopened.service.execute(takeover)
    fenced=reopened.service.execute(original)
    self.assertIsNone(fenced.private)
    self.assertEqual(fenced.committed_metadata.revision,17)
    self.assertGreater(fenced.current_metadata.ownership_version,first.current_metadata.ownership_version)
    self.assertIsNone(reopened.service.execute(reopened.command("import_plan",payload,key="import-three")).private)
    changed=copy.deepcopy(payload)
    changed["original_snapshot"]["objective"]="changed after handoff"
    before=reopened.service.read_events(first.current_metadata.plan_id)
    with self.assertRaises(DomainError) as error:
        reopened.service.execute(reopened.command("import_plan",changed,key="changed-source"))
    self.assertEqual(error.exception.code,"handoff_conflict")
    self.assertEqual(reopened.service.read_events(first.current_metadata.plan_id),before)

def test_active_import_context_claim_and_attachment(self):
    for known in (True,False):
        with self.subTest(known=known),TemporaryDirectory() as directory:
            f=make_service(Path(directory))
            claims=ExecutionContextClaims("non-repository",None,workspace("/synthetic/import-live",710),False,(),True,None)
            source=active_legacy_snapshot()
            payload={"host_id":f.service.read_service_info().host_id,"source_directory_id":None,"source_directory_claim":workspace("/synthetic/legacy-source",711),"original_task_id":source["taskId"],"original_snapshot":source,"provenance":DocumentRef("legacy.json",None,None,"v1",None),"writers_stopped":"legacy publisher stopped; host worker remains unconfirmed","known_contexts":{"step-1":claims} if known else {}}
            imported=f.service.execute(f.command("import_plan",payload))
            other=f.create(steps=(step("a"),),creation_id="other-import-plan")
            attempt=f.service.read_plan(imported.current_metadata.plan_id).coordination.attempts[0]
            self.assertEqual(attempt.state,"stale")
            self.assertIsNone(imported.private.attempt_token)
            if not known:
                with self.assertRaises(DomainError) as error:
                    f.assign(other)
                self.assertEqual(error.exception.code,"held")
                resolution={"resolution":"attach-context","reason":"identified live workspace","next_step_state":"blocked","stopped_resource_ids":(),"replacement_assignee":None,"replacement_context":None,"attached_context":claims}
                f.service.execute(replace(f.command("resolve_attempt",resolution,receipt=imported),attempt_id=attempt.attempt_id))
            self.assertFalse(f.service.read_service_info().store_hold)
            rid=f.register(claims.workspace_claim,other)
            with self.assertRaises(DomainError) as error:
                f.service.execute(f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=other))
            self.assertEqual(error.exception.code,"resource_conflict")
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_imports -v`; missing handoff.
- [ ] **Implement:** Import is a plan-creation bootstrap authenticated by installation credential/current epoch, with no existing orchestrator grant required. Require exactly one existing source-directory ID or physical workspace-kind source-directory claim; reject both/neither. Exact-key replay follows Task 5 before new-operation preconditions. For a new key, resolve the ID or call private `register_claim` inside the import transaction before source-identity deduplication. Registration neither reserves execution use nor releases existing reservations; host/physical/alias/conflict rules are identical to ordinary registration. Any validation/conflict failure rolls back new registrations with the entire import. Return canonical source_directory_id and issue initial PrivateGrant via issue_orchestrator. Matching source-identity dedup with another key returns the original grant only while it remains current; after supersession return private=None plus original committed receipt/current metadata, never renewed authority. Preserve this after restart; stale epochs return no grant. Require recorded writers-stopped handoff of obsolete legacy JSON/publisher authority and provenance; this is NOT an execution-resource stop assertion and does not release imported host work; service accepts snapshot content only, never reads directory. Preserve source revision as the initial service counter, source scope.sinceRevision unchanged, and original digest/generation/tuple/evidence/scope as provenance; service generation is its own UUID. Do not reset imported revision to 1. These comparison metadata are not unique key components. Resolve ExecutionContextClaims through repository-first register_claim, inject canonical repository ID into workspace claim, validate same identities/aliases/case rules, then reserve actual contexts atomically; conflict rolls back whole import. attach-context registers/reserves for existing tokenless imported/restored unresolved attempt, preserves stale/recovery/history/latch, and clears identity hold only after ALL unidentified obligations are covered. Normal resolve variants require attached_context=None. Share stale-native resolution code path; no new interval or reverified-evidence claim. Service cannot fence old JSON publisher; docs require clients switch authority together.
- [ ] **Run pass:** Module and service scope/recovery modules pass; reopen retains unique source map and active reservations.
- [ ] **Local commit:** `git add adaptive_planning/imports.py adaptive_planning/recovery.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_imports.py`; `git commit -m "feat: import legacy plans with explicit authority handoff"`.

### Task 23: Consistent backup, guarded migrations, and epoch-changing restore

**Files:** Create `tests/test_service_admin.py`; modify `adaptive_planning/admin.py`, `adaptive_planning/schema.py`, `adaptive_planning/store.py`, `adaptive_planning/recovery.py`, `adaptive_planning/app.py`, `adaptive_planning/transport.py`, `docs/operations.md`.
**Interfaces:** Consumes `Store.write`, `create_unresolved_attempt`, `require_orchestrator`. Provides `backup_database(store: Store, destination: Path) -> dict[str,JSONValue]`, `export_database(store: Store, destination: Path) -> None`, `restore_database(config: Config, source: Path, *, preserve_current: Path) -> dict[str,JSONValue]`, `migrate_database(store: Store, backup_directory: Path) -> None`, `resolve_store_hold(connection: sqlite3.Connection, command: Command) -> MutationReceipt`. Admin filesystem paths are local administrator arguments, never MCP inputs.

- [ ] **RED:** `test_backup_consistent_and_newer_schema_refused` SQLite backup retains coherent events/snapshot; schema > supported refuses untouched; migration failure retains pre-migration backup. `test_restore_holds_absent_postbackup_work` backup, create later assignment/shared use, restore older backup: new installation epoch/all plan generations, tokens invalid, restored nonterminal attempts stale-unresolved, store hold true, cutoff disclosed. Old-key replay fails; clients reread read_service_info before takeover/reconciliation. All assign/start/reopen/accept and writable reserve_verification denied until inventory_complete plus claim-capable absent-work inventory covers identities; omission/unknown-running cannot count stopped. `test_backup_marker_and_export_redaction` serving marked backup rejects without changes and public JSON has no token/private-grant bytes; `test_normal_restart_not_restore` epoch unchanged on reopen. `test_restored_invalidation_verification_waits_for_inventory` accepted artifact/open invalidation permits immutable review evidence during the hold, but writable verification refuses held before use/event changes; after inventory a free workspace works and an orphan workspace still conflicts. The absent-work test also pages from another plan before orphan release and rejects its old cursor after release while that plan revision stays unchanged.

```python
def test_normal_restart_not_restore(self):
    epoch=self.f.epoch
    generation=self.p.current_metadata.generation
    reopened=make_service(self.f.directory,self.f.clock)
    self.assertEqual(reopened.epoch,epoch)
    self.assertEqual(reopened.service.read_plan(self.plan_id).metadata.generation,generation)

def test_restore_holds_absent_postbackup_work(self):
    backup=self.f.directory/"backup.sqlite"
    backup_database(self.f.store,backup)
    late=self.f.create(creation_id="post-backup-plan")
    claim=workspace("/synthetic/post-backup-work",701)
    rid=self.f.register(claim,late)
    late_attempt=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=late))
    self.f.start(late,late_attempt)
    restore_database(Config(self.f.directory),backup,preserve_current=self.f.directory/"pre-restore.sqlite")
    restored=make_service(self.f.directory,self.f.clock)
    self.assertTrue(restored.service.read_service_info().store_hold)
    owner=restored.create(steps=(step("a"),),creation_id="restore-inspector")
    work=HostWork("post-backup-worker","worker-a","running",(),(claim,),checkpoint().evidence)
    cutoff=restored.service.read_service_info().restore_cutoff
    restored.service.execute(restored.command("resolve_store_hold",{"backup_cutoff":cutoff,"inventory_complete":True,"absent_work_inventory":(work,),"resource_resolutions":(),"reason":"complete declared inventory"},receipt=owner))
    self.assertFalse(restored.service.read_service_info().store_hold)
    canonical=restored.register(claim,owner)
    command=restored.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(canonical)},receipt=owner)
    with self.assertRaises(DomainError) as error:
        restored.service.execute(command)
    self.assertEqual(error.exception.code,"resource_conflict")
    page=restored.service.read_coordination(owner.current_metadata.plan_id)
    orphan=next(i.value for i in page.items if i.kind=="reservation" and i.value.absent_work_id==work.work_id)
    self.assertIsNone(orphan.plan_id)
    observer=restored.create(creation_id="orphan-page-observer")
    observer_id=observer.current_metadata.plan_id
    seed_terminal_history(restored,observer_id,"a",2)
    observed=restored.service.read_plan(observer_id)
    first_page=restored.service.read_coordination(observer_id,limit=1)
    self.assertIsNotNone(first_page.next_cursor)
    restored.service.execute(restored.command("release_resource_use",{"use_id":orphan.use_id,"resource_ids":orphan.resource_ids,"reason":"explicit orphan writer stopped","stopped":True},receipt=owner))
    self.assertEqual(restored.service.read_plan(observer_id).metadata,observed.metadata)
    with self.assertRaises(DomainError) as error:
        restored.service.read_coordination(observer_id,after=first_page.next_cursor,limit=1)
    self.assertEqual(error.exception.code,"conflict")
    self.assertGreater(restored.service.read_plan(observer_id).coordination.store_sequence,observed.coordination.store_sequence)
    restored.service.execute(restored.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(canonical)},receipt=owner))

def test_restored_invalidation_verification_waits_for_inventory(self):
    p=self.f.create(steps=(step("a",review_required=True),),creation_id="restore-review")
    pid=p.current_metadata.plan_id
    value=checkpoint()
    active=self.f.start(p,self.f.assign(p))
    self.f.submit(p,active,value)
    review=Review("reviewer",value.artifact,"passed",(),value.evidence,None)
    self.f.service.execute(self.f.command("record_review",{"step_id":"a","review":review},receipt=p))
    self.f.accept(p,active,value)
    backup=self.f.directory/"review-backup.sqlite"
    backup_database(self.f.store,backup)
    late=self.f.create(creation_id="unknown-review-writer")
    claim=workspace("/synthetic/absent-review-writer",704)
    rid=self.f.register(claim,late)
    worker=self.f.service.execute(self.f.command("assign_step",{"step_id":"a","assignee":"worker-a","context":context(rid)},receipt=late))
    self.f.start(late,worker)
    restore_database(Config(self.f.directory),backup,preserve_current=self.f.directory/"review-pre-restore.sqlite")
    restored=make_service(self.f.directory,self.f.clock)
    state=restored.service.read_plan(pid)
    owner=restored.service.execute(restored.command("takeover",{"expected_ownership_version":state.coordination.ownership_version,"reason":"inspect restored review result"},receipt=p))
    restored.service.execute(restored.command("invalidate_result",{"step_id":"a","result_version":state.coordination.result_versions["a"],"reason":"verify restored result","evidence":value.evidence},receipt=owner))
    orphan_rid=restored.register(claim,owner)
    free_rid=restored.register(workspace("/synthetic/free-review",705),owner)
    payload={"step_id":"a","verifier":"reviewer","context":context(orphan_rid),"handoff_from":None,"stopped_reason":None}
    before=restored.service.read_plan(pid)
    events=restored.service.read_events(pid)
    with self.assertRaises(DomainError) as error:
        restored.service.execute(restored.command("reserve_verification",payload,receipt=owner))
    self.assertEqual(error.exception.code,"held")
    after=restored.service.read_plan(pid)
    self.assertEqual(after.metadata,before.metadata)
    self.assertEqual(after.coordination.reservations,before.coordination.reservations)
    self.assertEqual(restored.service.read_events(pid),events)
    restored.service.execute(restored.command("record_review",{"step_id":"a","review":review},receipt=owner))
    self.assertTrue(restored.service.read_service_info().store_hold)
    work=HostWork("absent-review-worker","worker-a","running",(),(claim,),value.evidence)
    restored.service.execute(restored.command("resolve_store_hold",{"backup_cutoff":restored.service.read_service_info().restore_cutoff,"inventory_complete":True,"absent_work_inventory":(work,),"resource_resolutions":(),"reason":"complete review inventory"},receipt=owner))
    with self.assertRaises(DomainError) as error:
        restored.service.execute(restored.command("reserve_verification",payload,receipt=owner))
    self.assertEqual(error.exception.code,"resource_conflict")
    free=restored.service.execute(restored.command("reserve_verification",{**payload,"context":context(free_rid)},receipt=owner))
    self.assertTrue(free.committed["use_id"])

def test_backup_marker_and_export_redaction(self):
    backup=self.f.directory/"backup.sqlite"
    backup_database(self.f.store,backup)
    before=backup.read_bytes()
    with self.assertRaises(DomainError):
        Store(backup,self.f.clock)
    self.assertEqual(backup.read_bytes(),before)
    export=self.f.directory/"public.json"
    export_database(self.f.store,export)
    text=export.read_text()
    self.assertNotIn(self.p.private.orchestrator_token,text)
    for field in ("orchestrator_token","attempt_token","private_grants","service-token"):
        self.assertNotIn(field,text)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_admin -v`; missing backup/restore behavior.
- [ ] **Implement:** Use SQLite connection backup API; mark copy installation.authority_kind="backup" and refuse serving it before any mutation. restore validates backup then atomically sets authority_kind="authority" with new epochs/generations/fencing/holds. Raw copying a live authority DB outside supported backup cannot be detected; operations prohibit it. Export redacted JSON public data/events/registry only, excluding capabilities/private grants/request-private results/service credential; private0600 backup retains recoverable grants. Use atomic admin replacement while server stopped, integrity/foreign-key validation before serve; retain current data/export for reconciliation. Before serving restored data mint epochs, invalidate capabilities, impose store-wide hold in transaction; new takeover permits inspection/reconciliation but not execution, including creating/activating writable reserve_verification. Preserve immutable review recording; accepted-artifact invalidation alone can still permit revalidation after the store hold clears. All store-wide hold/inventory/orphan changes append global-sequence events, so foreign-plan cursors invalidate without manufacturing revisions on every plan. resolve_store_hold uses current orchestrator of any authorizing plan (Command.plan_id); bootstrap plan while held is allowed, execution is not. In that transaction register HostWork.resource_claims, union canonical IDs with existing IDs, persist absent_host_work and store-level unresolved uses with null plan/attempt plus absent_work_id FK. Complete known running/unknown inventory may clear global hold while durable orphan reservations remain conflicting; incomplete/unidentified obligations keep hold. Empty ResourceResolution.resource_ids with absent_work_id selects that work’s full canonical set; nonempty IDs must be subset. Later orphan release requires current orchestrator of any plan plus explicit stopped assertion; cannot release another plan’s normal uses. Never infer absent work stopped. Orphan repository IDs/claims reserve repository-exclusive scope against descendant workspaces; other orphan identities reserve exclusive normal overlap. Global queries include all unresolved orphan/normal uses through redirects. Pre-migration backup precedes schema changes; rollback uses compatible image/data or verified backup.
- [ ] **Run pass:** Module passes; Linux container backup/restore/restart/newer-schema refusal exercise same named volume with synthetic data. Record absent-work inventory and rejected old capabilities. No ordinary update command deletes volume.
- [ ] **Local commit:** `git add adaptive_planning/admin.py adaptive_planning/schema.py adaptive_planning/store.py adaptive_planning/recovery.py adaptive_planning/app.py adaptive_planning/transport.py tests/test_service_admin.py docs/operations.md`; `git commit -m "feat: guard database restore and migration epochs"`.

### Task 24: Preserve bundled legacy regressions and service/legacy coexistence

**Files:** Modify `tests/test_service_core.py`, `tests/test_service_imports.py`, `docs/operations.md`; modify `scripts/live_plan.py` only for proven compatibility correction.
**Interfaces:** Consumes existing publisher/serve/recovery CLI; no legacy file becomes a writable service view. Provides `test_transferred_plan_not_dual_authority` instruction/schema check, plus unchanged full regression evidence.

- [ ] **RED:** `test_legacy_standard_library_cli_and_service_coexist` additionally requires the new explicit authority-handoff instruction/schema reference absent before this task; standalone CLI serves legacy assets outside checkout without SDK import; explicit transferred source is described as MCP-only authority and no write-through appears. New test fails if package extraction or instructions violate coexistence; do not force an artificial failure in passing old tests.

```python
def test_legacy_standard_library_cli_and_service_coexist(self):
    script=Path("scripts/live_plan.py").resolve()
    with TemporaryDirectory() as directory:
        result=subprocess.run([sys.executable,str(script),"--help"],cwd=directory,capture_output=True,text=True)
    self.assertEqual(result.returncode,0)
    self.assertIn("publish",result.stdout)
    operations=Path("docs/operations.md").read_text()
    self.assertIn("Transferred legacy plans use MCP as their only publisher authority",operations)
    self.assertIn("imports UNIQUE(host_id,source_directory_id,original_task_id)",operations)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_core tests.test_service_imports -v`; expected missing required handoff instruction/schema reference; passing old regressions need no artificial RED.
- [ ] **Implement:** Add the exact transferred-authority sentence and imports unique-key schema reference asserted above to docs/operations.md. Correct only concrete compatibility gaps; keep patchable storage I/O/fault points and serializer/CAS semantics. Preserve comprehensive corrupt-state recovery ordering: assemble/validate before backup, retry exhaustion keeps old bytes/backup, publishers/recovery serialize through existing lock.
- [ ] **Run pass:** `uv run --no-project --python 3.10 python -B -m unittest tests.test_service_core -v`, locked Python discovery and frontend DOM suite. Explicitly retain `TestPublishScopeAndClaims.test_acceptance_1_lasting_claim`, `_2_existing_interval_pending_to_complete_claims`, `_3_two_simultaneous_nonbaseline_active_or_complete_reject`, `_5_v1_migration`, `_7_stale_generation_cas_and_creation_conflict`; `TestPlanValidationV2.test_acceptance_9_evidence_and_retirement`; `TestRecoveryV2.test_acceptance_8_recover_comprehensive`, `test_acceptance_8_recover_retry_exhaust_preserves_old_bytes_and_backup`; corrective `SnapshotRegressions.test_concurrent_publish_exactly_one_cas_winner`, `test_legacy_semantics_match_real_baseline_and_migrate`, `RecoveryRegressions.test_assembled_recovery_validation_precedes_backup`, `test_recovery_publish_really_serialize`. Verify exact existing method names before invoking individually; full discovery is the required stable command.
- [ ] **Local commit:** `git add tests/test_service_core.py tests/test_service_imports.py docs/operations.md`; only if corrected, `git add scripts/live_plan.py`; `git commit -m "test: preserve legacy publication and recovery contracts"`.

### Task 25: Framework-neutral adaptive loop, outage policy, and operational docs

**Files:** Modify `SKILL.md`, `references/live-dashboard.md`, `docs/clients/codex.md`, `docs/clients/antigravity.md`, `docs/clients/copilot.md`, `docs/operations.md`; create `tests/test_service_instructions.py`.
**Interfaces:** Provides one generic instruction contract: publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work. Workflow document references/phase metadata are optional; no required Superpowers sequence.

- [ ] **RED:** `test_instruction_contract_and_unavailable_behavior` checks both skill/reference require checkpoint/evidence/reassessment after named step and verification; default dashboard link, ASCII explicit only; no service-mode per-task launch. Synthetic outage walkthrough: assigned bounded worker finishes verification and retains unpublished evidence, new assignments/dependents wait, same-key retry/reread/reconcile, no new server/canonical plan. `test_missing_mcp_worker_relay_contract` start/activity/checkpoint/review relay preserves worker + relay identity and never invents checks/heartbeat.

```python
def test_instruction_contract_and_unavailable_behavior(self):
    for path in ("SKILL.md","references/live-dashboard.md"):
        text=Path(path).read_text()
        self.assertIn("checkpoint",text)
        self.assertIn("reassessment",text)
        self.assertIn("unpublished",text)
        self.assertIn("explicit request",text)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_instructions -v`; current instructions fail service contract.
- [ ] **Implement:** Update skill and reference together; explain authoritative MCP versus detailed design/implementation docs and temporary workflow notes. Persist domain grants only in explicitly private client/session working records outside service public views, or use explicit takeover on new session if no valid grant is retained; do not assume automatic client-session persistence. Resume reads events/unfinished ownership/holds/reviews and explicitly sweeps before dispatch/recovery; an overdue worker first sweeps, then confirms ownership or publishes unfenced recovery checkpoint using latest version. Takeover is not scope renewal. Document authorization/fencing/enforcement boundary, worktree isolation, resource declaration, five-minute observable activity/15-minute stale defaults, safe pause relay, independent review, full client setup, Mac sleep/Docker availability, backup/update/rollback and explicit legacy handoff. No upstream Superpowers patch. Logs/secrets guidance stays in operational docs, not user-facing workflow implementation details.
- [ ] **Run pass:** Module passes; manually follow instructions using non-Superpowers document/data workflow, missing MCP worker relay and stopped Docker. Capture accurate last published revision/unpublished evidence; no default ASCII or spawned fallback server.
- [ ] **Local commit:** `git add SKILL.md references/live-dashboard.md docs/clients/codex.md docs/clients/antigravity.md docs/clients/copilot.md docs/operations.md tests/test_service_instructions.py`; `git commit -m "docs: make shared adaptive checkpoint service the default"`.

### Task 26: Final independent review, complete acceptance matrix, and release gate

**Files:** Create `tests/test_service_acceptance_matrix.py`; modify `adaptive_planning/transport.py`, `docs/operations.md` with actual evidence/limitations.
**Interfaces:** Provides `test_release_contract_coverage` table-driven checks that exposed mutation tools correspond to gated handlers and public reads contain no secrets; Provides `registered_handlers(service: PlanningService) -> Mapping[str,Callable]` for actual central registration and `validate_release_contract` in transport; no new scheduling feature. Independent reviewer consumes spec, source diff, contracts, exact checks and results in fresh context.

- [ ] **RED:** Add `test_release_contract_coverage` against new `validate_release_contract(tool_handlers: Mapping[str,Callable], public_view: PlanView) -> None`, rejecting registered ungated/unknown mutation handler and mixed public/private serialization; assert every semantic tool routes through central execute and all expected operations are present. Initially fail because the new production release-contract validator is absent; passing tests include negative registration/secret fixtures rejected by that validator.

```python
def test_release_contract_coverage(self):
    view=self.f.service.read_plan(self.plan_id)
    handlers=registered_handlers(self.f.service)
    validate_release_contract(handlers,view)
    with self.assertRaises(DomainError):
        validate_release_contract({**handlers,"unguarded":lambda command:None},view)
```

- [ ] **Run fail:** `uv run --frozen python -B -m unittest tests.test_service_acceptance_matrix -v`; expected missing validate_release_contract; on PASS the negative fixture proves detection.
- [ ] **Implement:** Implement `validate_release_contract` in `adaptive_planning/transport.py`, checking actual centralized handler registration and redacted public serialization; fill acceptance evidence below with command, date, tested version/surface, result and artifact references. Use Opus scoped reviewer seats only when chosen workflow requires them (none automatically added to inline); obtain final fresh substantive independent Astra review; fix findings within authorized execution scope, add decisive regression only for concrete failure, rerun affected checks. A failed review never becomes a passing gate through narrative.
- [ ] **Run pass:** `uv run --frozen python -B -m unittest discover -s tests -p 'test_*.py'`; `node --check assets/dashboard/app.js`; `node tests/test_frontend_dom.js`; `uv build`; installed wheel outside checkout; Linux container and actual client/browser checks below. All required evidence present; unresolved required client/version/security/recovery failures block release.
- [ ] **Local commit:** `git add adaptive_planning/transport.py tests/test_service_acceptance_matrix.py docs/operations.md`; `git commit -m "test: record complete shared service acceptance evidence"`. Commit fixes separately using only their exact changed files. No deployment, merge, push or release is implied by local verification.

## Acceptance evidence matrix

Every row is **NOT RUN during planning**. Executor replaces this status only with actual evidence; docs and SDK unit tests cannot stand in for installed-client or Linux demonstrations.

| Design check | Exact automated tests/tasks | Additional required evidence and expected result |
| --- | --- | --- |
| 1: three-client tools/read/checkpoint | Tasks 1,10,20; `test_streamable_http_two_protocols`, `test_three_clients_full_worker_checkpoint_cycle` | Installed Codex/AGY/local Copilot each list tools/read same plan/commit finished-worker checkpoint; record client surface/version and actual negotiated protocol. Task 10 proves planning interoperability only. |
| 2: persistence/resume | Tasks 3,9,20; `test_restart_preserves_epoch_and_events` | Close creator, `docker compose restart`, then `docker compose down` / `up -d`; unchanged named volume, plan IDs/generation/revision/assignments/evidence/URLs; accepted work not redispatched. |
| 3: assignment/resources/availability | Tasks 11–14; `test_same_step_one_assignment_winner`, `test_alias_device_change_and_path_reuse`, `test_repository_workspace_external_non_git_overlap`, `test_dependents_need_final_checked_base_bindings` | Barrier/separate connections produce one winner; test independent worktrees, repository-exclusive overlap, non-Git and external-only, unknown required identity rejection. |
| 4: idempotency/fencing | Tasks 4–5,14; `test_creation_id_survives_changed_request_key`, `test_request_replay_and_cas`, `test_lost_bootstrap_response_current_grant_only`, `test_concurrent_takeover_one_winner`, `test_fenced_mutation_cannot_replay`, `test_kind_capability_matrix`, `test_direct_worker_after_takeover_and_old_relay_fenced` | Lost-response replay original receipt/current metadata distinct; stale key/payload/epoch/token no partial events; no secrets in read/events/logs. |
| 5: frozen definitions/adaptation/review | Tasks 12,15–19; `test_attempt_definition_frozen`, `test_material_holds_commit_with_checkpoint`, `test_held_sibling_and_transitive_consumers`, `test_unaffected_proposer_accepted_while_sibling_held`, `test_review_bound_to_current_artifact`, `test_writable_verification_rejected_during_store_hold`, `test_restored_invalidation_verification_waits_for_inventory`, `test_final_review_not_circular_task_gate`, `test_failed_final_review_corrective_history`, `test_unassigned_retirement`, `test_failed_review_retirement_preserves_evidence`, `test_failed_review_same_attempt_fix_handoff`, `test_single_accepted_node_invalidation_prevents_complete`, `test_seeded_supersession_and_missing_proof_preserve_history` | Active held sibling cannot accept; transitive work cannot advance; unaffected proposer can accept; final review failure retains findings/corrective graph. |
| 6: evidence/interleaving | Task 15; `test_checkpoint_evidence_outcome_reassessment`, `test_interleaved_sibling_updates_preserved` | Dashboard/browser shows both independent workers' actual evidence/outcomes/reassessment, failed check never completion. |
| 7: scope | Tasks 13,22; `test_plan_only_no_assignment`, `test_first_step_global_latch_survives_every_transition`, `test_active_import_preserves_latch_and_holds` | Multiworker/release/reassignment/restart/import retain same interval/latch; takeover/resume gives no additional authorization. |
| 8: stale/relay/resources | Tasks 17,21; `test_stale_heartbeat_does_not_reactivate`, `test_isolated_replacement_keeps_old_resources`, `test_stale_checkpoint_and_fenced_late_evidence`, `test_submitted_overdue_requires_explicit_confirmation`, `test_review_handoff_never_frees_exclusive_workspace` | Advance injected clock, no sleeps; old X/shared DB unavailable after Y replacement; UI says host edits uncontrolled; origin/relay explicit. |
| 9: dashboard | Task 8 plus M2 projections; `test_multiple_plans_and_committed_freshness`, `test_many_historical_rows_do_not_block_safety_reducers`, DOM synthetic script/two-worker fixture | Browser index/plan viewport/screenshot, optional workflow omission, multiple active work/holds/reviews, CSP and escaped evidence, reconnect committed state. |
| 10: unavailable behavior | Task 25; `test_instruction_contract_and_unavailable_behavior`, `test_missing_mcp_worker_relay_contract` | Stop Docker/service during synthetic assigned step; bounded verification and unpublished evidence retained, no new assignment/server/plan/default ASCII. |
| 11: legacy/import/restore | Tasks 22–24; full legacy discovery, `test_source_identity_not_digest_tuple_identity`, `test_import_bootstrap_lost_response_current_grant_only`, `test_first_plan_import_by_claim`, `test_active_import_preserves_latch_and_holds`, `test_restore_holds_absent_postbackup_work`, `test_normal_restart_not_restore`, `test_active_import_context_claim_and_attachment`, `test_backup_marker_and_export_redaction` | Linux backup/restore/newer-schema refusal, preserved pre-restore export; new epochs/invalid caps/store hold until absent-work inventory reconciled. |
| 12: container/auth | Tasks 6,9; `test_all_route_host_origin_and_mcp_auth`, `test_logs_and_urls_are_secret_free`, `test_container_contract` | `docker inspect` confirms UID10001/loopback/named-volume/no forbidden mounts; HTTP wrong/missing bearer and bad Host/Origin refused; logs/URLs secret-free. |
| Review Focus 1 | Tasks 4–5 lost bootstrap/fencing tests | Superseded receipt has `private=None`, old cached mutations never grant authority. |
| Review Focus 2 | Task 11 alias/device/path/reconcile tests | Conservative matching on both physical identity and canonical path, attested case behavior. |
| Review Focus 3 | Tasks 15–16 atomic hold tests | Concurrent operation cannot pass between checkpoint and holds; no wait for proposer acceptance. |
| Review Focus 4 | Task 23 restore absent-work test | Whole-store hold protects work absent from backup; inventory must address unknown host work. |
| Review Focus 5 | Tasks 21,25 relay/outage tests | No fabricated heartbeat, checkpoint or host stop; no silent fallback server/plan. |

## Execution preflight and handoff

Implementation has not started. Use the existing isolated checkout `/Users/aaat/orca/workspaces/adaptive-planning/mcp-linux-desing`; load `superpowers:using-git-worktrees` during future execution to verify it is isolated and preserve dirty README/docs. Do not create another worktree or commit during planning. Before execution, reread applicable AGENTS.md, spec, this plan, and documentation via `/Users/aaat/.agents/skills/find-docs/SKILL.md` whenever technology behavior/version choices matter. Check current usage windows and available controls before costly delegation; unknown quota is not exhaustion.

Recommended execution is subagent-driven for the coupled state/concurrency/recovery tasks; execution method has not been selected. Preserve the selected Superpowers path's templates, verdicts, fix budgets, and ledger. Do not add per-task reviewers to inline execution. Codex owns diagnosis, design, integration, acceptance, and disposition of findings.

All external model entry sessions, including launch/resume/changed assignments, go through Orca. Load `/Users/aaat/.agents/skills/orca-cli/SKILL.md` and its version-matched guide first. Use coordinator-owned custom terminals with explicit underlying provider commands, inspect effective arguments/configuration/hooks/tools/workspace before work, and preserve permission restrictions. Record owned terminal/job and provider conversation identities, settings, submitted packet, completion evidence, and cleanup. A send receipt is not completion. Structured/headless/unattended/continuation paths need separate verification; supervised terminals do not establish them. Exclude Gemini CLI; Claudex is not a dependency. Never launch a provider directly or bypass a denied action.

Preferred substantial implementation is `gemini-3.8-flash-high`, entry effort `high`, with AGY's real `/boost` inside Orca after loading `agy-delegation/SKILL.md`. Disclose `reasoning=high`; never add rejected `--effort high`. Wait for readiness/catalog, select `/model gemini-3.8-flash-high` before the task, and verify successful propagation without override errors. Record argv/selection. A future `--model` launch requires the same pre-task verification. Stop mismatch/error; corrected retry requires a fresh session and new evidence. Disclose internal managed workers as provider-managed when controls are unexposed; one active managed batch, no nested batches. No Google implementation occurs during planning.

Written-plan and required task/scoped reviewer seats use fresh Copilot `claude-opus-5.5`, explicit reasoning `high`, inside Orca. Disclose provider/model/effort/planned context before entry; select and verify the supported client context tier separately. Use an exact explicit session name, for example underlying `copilot --name adaptive-planning-mcp-plan-review --model claude-opus-5.5 --reasoning-effort high`, plus the installed supported context-tier option verified before launch; record actual argv/runtime model set to avoid unselected title helpers. Provide a scoped requirements/source/evidence snapshot with all tool operations denied by verified controls, unrelated MCPs/hooks/custom instructions/automatic routing and remote export/control disabled. An unrestricted read tool is insufficient. No source reads or writes beyond verified scope; if safe controls cannot be enforced, stop. Capture reviews locally, preserve required compliance/quality verdicts and fix budgets, and use fresh fix-review sessions until the selected workflow permits continuation and that route is verified. Never duplicate routine reviewer seats with native agents. Confirm completion and settle timeouts before retry; close only owned resources.

Final substantive independent whole-change review uses fresh native Astra: `gpt-6-astra` `medium`, or `high` for concurrency/security/recovery risk. Earlier distinct bounded Astra judgment remains available. Opus or managed-team review does not replace this final review. Native fallback for an unavailable required external route must disclose its exact model/effort/Orca-control limitation; confirmed provider exhaustion is a separate fact, and unknown allowance or a denied operation is not exhaustion. Native task workers use only explicitly selected `gpt-6.1-sol` efforts; substantive `ultra` requires exposed descendant model/effort/concurrency controls, otherwise the authorized controlled-team or coupled `max` alternative. One active Astra, one active Opus reviewer, four visible workstreams including ordinary external entries; do not launch Opus during a managed Google batch. Track Codex, Google, and Copilot allowance separately; no paid overflow/billing/reset changes. Announce every delegation with explicit model/effort/context/reason. User policy overrides stale skill guidance; existing task authorization does not waive external/destructive boundaries.

Primary SDK wheel/AST planning evidence includes mcp2.3.0 SHA256 `dd0c44c089d16453e8ae31a3877a0054d7a2314caaa81f5e0541b9b1734b2377`, not runtime proof. [Protocol modes](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v2.3.0/docs/protocol-versions.md) defines auto/legacy. Primary documentation for future verification: [SDK v2 migration](https://py.sdk.modelcontextprotocol.io/v2/migration), [Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [AGY configuration](https://antigravity.google/docs/mcp#mcp-configuration-properties), [Copilot CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), [Copilot MCP setup](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers), [VS Code MCP schema](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

Self-review completed during planning: all twelve acceptance checks and all five Review Focus conditions map to tasks and concrete evidence; DTO names/signatures and shared fixture references are consistent; extraction preserves strict legacy v2 and standard-library use; Milestone 1 does not claim worker checkpoint proof; no feature-code bodies are duplicated. This plan has 26 tasks; test methods contain concrete arrangements rather than implementation bodies. Actual unknowns: installed-client availability and runtime negotiation, real Docker arm64/amd64 build/run behavior, credential provisioning support in installed surfaces, and all implementation checks remain untested. The first transport/client gate must resolve availability before broader integration; final release requires all rows above.
