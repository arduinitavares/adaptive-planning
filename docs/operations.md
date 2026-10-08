# Running the shared planning service

Run these commands from the directory containing `compose.yaml`, using Docker
Engine 28 or newer and Docker Compose. The service listens on `0.0.0.0:8790`
inside its container; Compose publishes only `127.0.0.1:8790` on the host.
The dashboard is at `http://127.0.0.1:8790/` and the authenticated MCP endpoint
is `http://127.0.0.1:8790/mcp`.

## Build and initialize

Before starting, check that host port 8790 is available and inspect any existing
service container and volume named `adaptive-planning-data`. This volume name
is explicit, so changing the Compose project name does not isolate the data.
Reuse it only when it belongs to this installation; preserve unrelated resources.

```sh
docker compose config
docker volume inspect adaptive-planning-data
docker compose build
```

`docker volume inspect` reports a missing volume on a new installation. The init
and permission-check commands below create it through Compose.

The container runs as UID/GID `10001:10001`. The image creates `/data` owned by
that user with mode `0700`; the generated credential has mode `0600`. A new empty
named volume receives the image directory's contents and permissions. An existing
volume hides that directory, so image ownership does not establish its current
ownership. Verify the mounted directory before initialization:

```sh
docker compose run --rm --entrypoint /app/.venv/bin/python adaptive-planning -B -c 'import os, stat; s = os.stat("/data"); print({"uid": s.st_uid, "gid": s.st_gid, "mode": oct(stat.S_IMODE(s.st_mode)), "writable": os.access("/data", os.W_OK)})'
```

Expect UID and GID 10001, mode `0o700`, and `writable: True`. If a reused volume
does not match, stop and resolve its ownership with the installation owner before
initialization. Then initialize and start the service:

```sh
docker compose run --rm adaptive-planning init
docker compose up -d
```

Initialization is explicit and idempotent: it creates a private credential at
`/data/service-token`, preserves an existing valid credential, and prints only
`Installation initialized.` It does not start the server or print the credential.
Serving before initialization fails with `not_initialized`; ordinary startup
never generates a credential. The credential is not passed through build arguments, image
layers, Compose environment variables, or the URL. Configure trusted MCP clients
with the existing credential through a private local channel; do not print it or
put it in logs, shell history, source files, or shared client configuration.

## Persistence and lifecycle

All installation state, including `planning.sqlite` and `service-token`, stays in
the named volume `adaptive-planning-data` mounted at `/data`. The service has no
checkout, home-directory, Docker socket, or credential bind mount and no host edit
endpoint. Updating the image does not update or replace the volume.

```sh
docker compose restart adaptive-planning
docker compose down
docker compose up -d
```

Restart and `down` followed by `up -d` retain the volume. Keep the same data-volume
identity and verify that an existing plan's ID, revision, and dashboard URL remain
the same after each operation. Never use `docker compose down -v` for routine
shutdown, updates, or persistence checks: it removes the installation data.
`unless-stopped` restarts the service after an unexpected exit or daemon restart;
a deliberately stopped or removed service needs an explicit start.

## Build inputs and platform verification

The Python base is
`python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641`.
The builder copies uv `0.10.12` and installs production dependencies with
`uv sync --frozen --no-dev --no-editable`. The context admits only the Python
package, dashboard assets, and build metadata. Dashboard files are included in
both the wheel and source distribution; service asset reads use package resources.
Build-system requirements use the pinned Hatchling backend; their transitive
build dependencies are not hash-constrained by the service lockfile. These pins
do not promise byte-identical image rebuilds.

Verify each deployment platform separately. For `linux/arm64` and `linux/amd64`,
record the built image ID, digest when available, and platform; the running
container's `uname` and Python version; UID, mount, host port, and restart policy;
a synthetic request using the official MCP SDK; and a dashboard asset GET.
Verify plan ID, revision, and URL persistence across both lifecycle operations
above. Builder or registry platform listings establish availability, while a
successful build and runtime check establish that platform's operation. Record
an unavailable platform as a verification gap.

Docker's documentation describes [named-volume persistence and copy behavior](https://docs.docker.com/engine/storage/volumes/),
[loopback port publication](https://docs.docker.com/engine/network/port-publishing/),
and the [`unless-stopped` restart policy](https://docs.docker.com/reference/compose-file/services/#restart).

## Three-client planning gate

Configure the actual chosen surfaces using the [Codex](clients/codex.md),
[Antigravity](clients/antigravity.md) and [Copilot](clients/copilot.md) guides.
Configuration and service activation require execution authorization and private
credential provisioning. The example values belong only in documentation or an
isolated synthetic installation. They must not be copied as credentials into real
client files. Installation bearer authentication and the plan's private
orchestrator grant are separate capabilities; keep both out of public evidence.

The automated SDK planning scenario is
`tests.test_service_interop.run_planning_interop(service: PlanningService) -> tuple[MutationReceipt, ...]`.
It opens three distinct official SDK HTTP sessions against an ephemeral
`build_app` production stack sharing the supplied fixture's SQLite installation.
The first creates a `plan-only` plan, closes, and the other fresh sessions read
and revise distinct facts sequentially. It compares canonical state and public
dashboard plan/event responses, preserves authorization, records three committed
revisions, and closes its listener. This is SDK transport evidence, separate from
activation of the installed products. The public
[SDK client](https://py.sdk.modelcontextprotocol.io/v2/client/) and
[HTTP transport](https://py.sdk.modelcontextprotocol.io/v2/client/transports/)
documentation describe the public APIs used.

```sh
uv run --frozen python -B -m unittest tests.test_service_interop tests.test_service_sdk -v
```

For installed-client acceptance, the coordinator must carry out and record this
planning-only sequence through the actual products:

1. Each fresh client negotiates MCP, lists the actual tools, and calls
   `read_service_info({})` before constructing a command. Record the observed
   protocol, epoch, host ID and installation dashboard URL; do not assume a
   protocol version from the SDK or an app-server handshake. The planning subset
   used by this earlier gate is `read_service_info`, `list_plans`, `read_plan`, `read_events`,
   `list_ready`, `read_coordination`, `create_plan`, `revise_plan` and `takeover`.
2. The creator calls `create_plan({"command": ...})` with a fresh request key and
   creation ID, an explicit new-plan intent, a `plan-only` draft and the first
   distinct planning fact. Retain its `MutationReceipt.private.orchestrator_token`
   privately in memory. Read the canonical plan and events through `read_plan`
   and `read_events`, record only public committed metadata and the dashboard
   event, then close the creator session.
3. A fresh second product reads `read_plan({"plan_id": ...})`. Copy its canonical
   `draft`, append the second fact, and call `revise_plan({"command": ...})` with
   current metadata and the existing grant. Echo stored authorization exactly,
   including the interval and `since_revision` initialized at creation; do not
   rebuild it from the blank creation draft. Use `authorization_change: null`
   and `retire_steps: []` because this gate changes only planning facts.
4. A fresh third product resumes that same plan, reads the latest canonical draft,
   and appends the third distinct fact through another revision using the same
   rules. All clients must discover the same epoch, host ID, generation and plan
   dashboard URL; the three successful commands have distinct committed revisions.
5. Compare `read_plan` and `read_events` with public GETs of
   `/api/plans/<plan_id>` and `/api/plans/<plan_id>/events` and the plan page at
   `/plans/<plan_id>`. Record all three events and facts, plus creator-close and
   fresh-session resume evidence. This gate assigns no workers and records no
   finished-worker checkpoint; repeat checkpoint acceptance in Task 20.

Every mutation uses the typed command envelope: `kind`, `epoch`, `key`, `actor`,
`plan_id`, `generation`, `expected_revision`, `ownership_version`,
`orchestrator_token`, `attempt_id`, `attempt_token`, `attempt_version`,
`definition_version` and `payload`. For create, target metadata and both domain
tokens are null. For revise, populate plan ID, generation, revision and ownership
version from the latest canonical metadata, and the orchestrator token from the
private creator receipt. All attempt fields remain null. `create_plan.payload`
contains `creation_id`, `draft`, `new_plan_intent`; `revise_plan.payload` contains
`draft`, `reason`, `authorization_change`, `retire_steps`. A draft contains
`objective`, `acceptance_criteria`, `constraints`, `facts`, `assumptions`,
`next_check`, `authorization`, `steps`, `documents`, `workspace_refs`, `workflow`,
`horizon`, `stop_reason`, `block_reason`. The executable fixture supplies a
synthetic draft and DTO conversion without exposing private receipts.
`Authorization` contains `mode`, `interval`, `since_revision`, `note`, `baseline`
and `claimed`; the entire stored value is echoed unchanged for these revisions.

Retain complete wire/test evidence privately with bounded logging and private
permissions; publish only sanitized observations and evidence references. An SDK
test, config schema, connected status or launch receipt cannot fill an installed
client row. If a chosen surface does not expose trustworthy negotiation or cannot
perform a planning call, record the gap. Do not infer success from diagnostics.

Installed-client evidence remains **pending** here. Replace these cells only with
actual authorized observations; any missing client remains a release blocker.

| Client | Installed version / actual surface / provisioning method | Negotiated MCP / fresh tools / epoch / host ID | Shared plan ID / committed revision / dashboard URL | Creator close / fresh resume / evidence | Result |
| --- | --- | --- | --- | --- | --- |
| Codex | Pending: record CLI or Desktop and its actual credential method | Pending | Pending | Pending | Release blocker |
| Antigravity | Pending: record actual AGY surface and private literal-header method | Pending | Pending | Pending | Release blocker |
| Copilot | Pending: record CLI or VS Code; CLI additionally needs actual environment-expansion proof | Pending | Pending | Pending | Release blocker |

## Three-client worker checkpoint gate

Task 20 adds a source interoperability checkpoint to the earlier planning gate.
`tests.test_service_interop.run_worker_interop(service: PlanningService) -> tuple[Event, ...]`
returns that auto-mode run's three canonical finished-checkpoint events. The
named test calls it once, then runs a separate legacy cycle. Each cycle creates
three distinct official SDK `Client` objects against one owned ephemeral
production HTTP app, with actual bearer-authenticated wire requests and the
production wall clock. The observed, asserted negotiations are auto `2026-07-28`
and legacy `2025-11-25`. Creation, resource registration, assignment, direct
worker start/checkpoint, reassessment classification, required review, owner
acceptance and explicit resource release all traverse SDK `call_tool` and
central `PlanningService.execute`. The captured public proof identifies actual
synthetic plans, attempts, checkpoint/event sequences, revisions, artifacts,
accepted result UUIDs and the applicable original review UUID/sequence.

The source actors `codex-worker`, `agy-worker` and `copilot-worker` are synthetic
labels. They do not establish that any installed product or real model ran.
Separate wire regressions use actual acceptance to test missing availability,
full-artifact/final-base binding, consumer start, invalidation and named
post-invalidation revalidation. An unremediated affected consumer remains held.
Another regression proves that `plan-only` refuses assignment even when the
worker tools are visible. Resource paths, identity declarations and stopped-use
facts in these tests are synthetic; they certify no real host liveness.

The tested catalog at this source checkpoint contains six reads:
`read_service_info`, `list_plans`, `read_plan`, `read_events`, `list_ready` and
`read_coordination`. Eighteen mutations are enabled: `create_plan`, `revise_plan`,
`takeover`, `register_resource`, `reconcile_resources`, `assign_step`,
`bind_availability`, `start_step`, `report_activity`, `record_checkpoint`,
`resolve_reassessment`, `invalidate_result`, `resolve_invalidation`,
`record_review`, `accept_step`, `reserve_verification`, `release_resource_use`
and `reopen_attempt`. Five declared future kinds remain disabled: `import_plan`,
`sweep_stale`, `resolve_attempt`, `append_late_evidence` and `resolve_store_hold`.
Their appearance in the capability vocabulary is not tool availability.

### Roles, checkpoints and reconciliation

Every MCP request needs the installation bearer. It is separate from the private
domain grants returned by creation/takeover and assignment. Public reads show
canonical metadata, targets, events and paged coordination without grants;
listing tools and advisory readiness do not authorize work. Bootstrap commands
use neither domain token. Enabled owner operations require the current owner
grant and forbid attempt tokens. Missing required grants without forbidden
extras yield `forbidden`; unexpected extra tokens yield `invalid`.

Direct `start_step`, `report_activity` and `record_checkpoint` use only the
current attempt grant. Their actor and origin match the assignee, global
`expected_revision` and `ownership_version` are null, and their own attempt ID,
version and definition match the actual target. A relay requires both grants,
current owner CAS and `relay` equal to the exact current owner actor. Takeover
fences old owners and relays while leaving coherent direct worker grants alive.
Owner commands use current global revision/ownership and exact nonsecret target
fields where required, without a worker token.

Keep the original immutable command envelope and request key for an uncertain
retry. Authenticate before exact replay. Accepted worker grants become replay
only for their own already committed commands; a new key or new terminal
activity is refused. Changing the envelope to a later revision/version creates
a different request, even when the caller intended a retry. After uncertainty,
read canonical events and paged coordination to reconcile what committed before
dispatching anything else. Resource identity reconciliation records identity;
it neither proves stopped use nor renews scope or the first-step latch.

A finished checkpoint carries outcome/check, full `ArtifactRef`, meaningful
artifact-bound evidence, origin/relay and reassessment. Its public
`checkpoint_id` is the actual global checkpoint event sequence represented as
a string. Passing submission leaves the attempt submitted and the step in progress.
Material reassessment creates provisional holds that the current owner must
classify explicitly. Acceptance requires the exact current attempt/version/
definition, full submitted artifact, passing checkpoint evidence, current
execution authorization and satisfied holds/prerequisites. A required review uses the
latest original matching verdict and evidence; a final gate also requires
review. Neither role may bypass that review.

The owner supplies `accept_step.payload` with `step_id`, full `artifact`,
`stopped_resource_ids` and `stopped_reason`. Empty stop IDs retain physical
obligations, so accepted logical work can leave the plan blocked until explicit
resource release. Named stopped IDs/reason must support those original
implementation obligations; they do not stop unrelated verification uses.
Acceptance returns a durable result UUID distinct from the artifact's version,
checkpoint sequence and review UUID. The acceptance event preserves its
checkpoint sequence and applicable original review ID/sequence.

Before a dependent worker starts, the owner binds availability to the exact
accepted result UUID, full artifact and the consumer's final checked base.
Missing availability or a different full artifact refuses start/binding without
publication. Invalidation preserves result identities and holds the producer
plus affected consumers. Each named resolution must prove the appropriate exact
accepted artifact/result; required revalidation needs a passing original review
recorded after invalidation. Resolving the producer does not automatically clear
consumer holds, redispatch accepted work, grant authorization or renew scope.

### Genuine installed-client and lifecycle evidence remains open

The source test/docs checkpoint may integrate locally only after the
coordinator's source specification and code-quality review. It does not satisfy
the installed-client release gate. Actual Task 20 installed-client runs remain
**0**, `all_client_gate_passed=false` and `full_task_acceptance=false`. Release,
shared-default activation and full completion remain blocked. Existing Task 10
planning preparation/probes and any historical installed versions are separate
evidence; Task 20 did not refresh installed versions or activate client files.

For each later authorized actual surface, record the installed product/version,
chosen surface and private credential method; observed negotiated protocol,
fresh tools and installation epoch/host/endpoint; shared plan/generation/revision/
dashboard URL; own assigned step/attempt/version/definition; visible finished
checkpoint ID/event sequence/actor/origin/relay and evidence/artifact/reassessment;
current owner acceptance result UUID/event sequence and applicable original
review UUID/sequence. Publish sanitized observations and evidence references,
never grants or raw private wire captures. Launch receipts, configuration entries,
SDK results and connected status cannot fill these rows.

| Actual client | Finished checkpoint and owner acceptance | Close/reopen client | Service restart | Container recreation |
| --- | --- | --- | --- | --- |
| Codex: actual CLI/Desktop surface and version unobserved | NOT RUN — release blocker | NOT RUN | NOT RUN | NOT RUN |
| Antigravity: actual surface and version unobserved | NOT RUN — release blocker | NOT RUN | NOT RUN | NOT RUN |
| Copilot: actual CLI/VS Code surface and version unobserved | NOT RUN — release blocker | NOT RUN | NOT RUN | NOT RUN |

Each lifecycle row must compare before/after assignments, evidence, revisions,
result identities and URLs while preserving accepted work without redispatch.
Rebuilding an app over a synthetic store is source evidence, not the actual
service/container lifecycle gate. Retain NOT RUN, failed or unsupported rows and
specific call/allowance gaps. This checkpoint grants no provider retry, account/
billing change or client activation to close those gaps. Raw test evidence stays
private; complete captures can contain diagnostic data and are not universally
secret free.
