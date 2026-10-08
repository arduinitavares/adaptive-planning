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

The automated equivalent is
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
   protocol version from the SDK or an app-server handshake. The current planning
   tools are `read_service_info`, `list_plans`, `read_plan`, `read_events`,
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
