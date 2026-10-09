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

### Mac sleep and Docker availability

On macOS hosts, Mac sleep can suspend Docker and make the endpoint unavailable.
Retain the named volume `adaptive-planning-data` across wake and restart, then
verify canonical state continuity once the endpoint is reachable.

During Mac sleep or temporary Docker downtime:
1. Active MCP requests fail with transport errors (connection refused or timeout).
2. Assigned bounded workers that are already executing may finish their local verification
   steps and retain unpublished evidence in their private client working records.
3. Downstream dependent steps and new assignments must wait.
4. Clients must not launch fallback servers or create competing local canonical plans.
   Report the last published revision as **LAST KNOWN**, offline/stale until canonical
   reads succeed again.
5. Upon host wake, wait for Docker and the service to become available. An unexpectedly
   exited service may restart under `restart: unless-stopped`; a deliberately stopped
   service requires an explicit operator start.
6. Once the endpoint is reachable, reread service identity, canonical events, ownership,
   and paged coordination. Reconcile the epoch, generation, current attempt version, and
   any holds before publishing retained evidence. Preserve the original immutable command
   body and request key and retry that exact saved command without rebuilding metadata or
   payload. A fenced or stale command requires explicit owner recovery; reread after
   publication or exact replay before dispatching more work.

### Deployment update and rollback

To update the service deployment image:
1. Rebuild the image: `docker compose build`.
2. Stop the current container: `docker compose down`.
3. Start with the updated image: `docker compose up -d`.
4. The named volume `adaptive-planning-data` is preserved intact. Verify schema version,
   epoch, and plan continuity via `read_service_info` and the dashboard at `http://127.0.0.1:8790/`.

If an updated image fails or introduces regressions, execute rollback:
1. Stop the failing container: `docker compose down`.
2. Revert to the previously verified image or commit in `compose.yaml` (or checkout).
3. Start the previous service: `docker compose up -d`.
4. If database corruption occurred or rollback requires restoring a backup snapshot, follow
   the explicit administrative restore procedure using `adaptive_planning.admin restore`
   with `--preserve-current` and recorded maintenance manifest recovery decisions.

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

The base's Debian `libsqlite3-0` 3.40.1-2+deb12u2 lacks the upstream WAL-reset fix.
A separate builder uses the **same pinned Python base**, downloads the upstream
[SQLite3.53.4 amalgamation](https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz),
and checks SHA3-256
`454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338`
and SHA256
`0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c`
before extraction. GCC builds the shared library with `-O2 -fPIC -shared`,
SONAME `libsqlite3.so.0`, `SQLITE_THREADSAFE=1`, and pthread/dl/math linkage;
no SQLite features are omitted. Only that library enters `/usr/local/lib` in
the final image; compiler, source and build packages stay in the builder.
`ldconfig` selects it for ordinary Python `sqlite3`, with no Python package or
lockfile substitution. The build asserts actual Python3.13.16, SQLite3.53.4,
serialized thread safety, source ID
`2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`,
and WAL/FK/JSON/backup functionality. Record actual loaded runtime/library and
compile options for each target architecture; source/build flags alone do not
establish runtime qualification. The installation maintainer owns future SQLite
security/fix pin updates and requalification. SQLite's
[compile guide](https://www.sqlite.org/howtocompile.html) and
[compile options](https://www.sqlite.org/compile.html) describe the amalgamation
and feature choices.

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

## Local maintenance and schema upgrades

The authority schema is version2. Startup refuses older/newer schemas, marked
backups, corrupt or empty existing files, symlinks, and pending maintenance before
opening SQLite on the original image or changing its permissions. Inspecting an
existing image costs O(database size) temporary disk space and I/O: a private
`0700` OS temporary directory outside the installation holds a disposable `0600`
copy of the database and any WAL/rollback journal. Only that copy undergoes
SQLite recovery and validation, and it is explicitly removed on exit.

Every usable `Store` owns a shared lifetime lock on its installation directory.
Operations hold independent shared `flock` gate descriptors, including committed
listener callbacks. Cold migration, restore and recovery require exclusive
ownership; a closed-but-not-disposed app, sibling Store, or another process can
block them. Use `with Store(...) as store:` or call `store.close()` explicitly.
Close is idempotent, drains operations/callbacks, and rejects further use. An app
created with `build_app` closes its Store on lifespan exit; call
`app.state.close()` when disposing an app whose lifespan never started. A Store
inherited across `fork` is fenced; construct a fresh one in the child.

These guarantees require cooperative local processes and a filesystem supporting
POSIX `flock`, SQLite locking, private regular files, hard links, same-filesystem
rename and directory fsync. Unsupported locking refuses. They do not coordinate
arbitrary SQL writers, directory replacement, network filesystems or external
workers. Lock/SQLite contention is bounded (two seconds), and online backup has
a ten-second progress deadline. Blocking kernel filesystem calls or callbacks
that never return remain outside those cooperative deadlines. Qualify the actual
Python/SQLite runtime before WAL concurrency; use a SQLite release containing the
[documented WAL-reset fix](https://www.sqlite.org/wal.html#wal_reset_bug), including
3.51.3 or newer, or the 3.50.7 backport.

The explicit local command is `python -m adaptive_planning.admin`. In a checkout,
use its frozen runner. Choose new destinations in private existing directories:

```sh
uv run --frozen python -B -m adaptive_planning.admin backup --data-dir /private/planning --destination /private/backups/snapshot.sqlite
uv run --frozen python -B -m adaptive_planning.admin export --data-dir /private/planning --destination /private/backups/public.json
```

Backup uses SQLite's coherent backup API; its epoch, event sequence and opaque
cutoff come from the completed image. It is a private marked backup, never a
serving authority. Export reads one snapshot and writes an explicit public JSON
projection. It excludes credentials, capabilities, private request/grant receipts,
and imported/restored private origins; unknown event kinds retain only their
public envelopes. Neither command overwrites a destination or an authority,
credential, gate or maintenance-manifest alias. A published valid file remains
available if directory fsync reports `durability_uncertain`; inspect that exact
destination rather than retrying over it.

Stop serving and dispose **all** Store owners before cold operations:

```sh
uv run --frozen python -B -m adaptive_planning.admin migrate --data-dir /private/planning --backup-directory /private/backups
uv run --frozen python -B -m adaptive_planning.admin restore --data-dir /private/planning --source /private/backups/snapshot.sqlite --preserve-current /private/backups/before-restore.sqlite
```

Migration supports genuine1→2 images, retaining a marked pre-migration backup
before transforming a private stage. It preserves epochs, generations, native
grants, private receipts, import maps, event high-water sequence, rowids and every
physical use history. Unprovable legacy orphan parent/member identity refuses
with the original untouched and pre-backup retained. A valid current2 migration
is a no-op that creates no backup or manifest. The local
`migrate_database(store, backup_directory)` wrapper consumes/closes its supplied
Store even if a sibling owner subsequently blocks migration.

Restore requires an admitted existing schema2 authority and a closed marked
same-host backup (schema1 may upgrade inside the stage). A quiesced source can
include committed WAL or a rollback journal: restore materializes the same
admitted private clone through bounded SQLite backup into a closed standalone
stage and retains the source's exact existing cutoff. The original main file,
sidecars, permissions and directory entries remain untouched. Restore lineage
binds every original file before clone recovery; SHM is recorded as cache
evidence and SQLite rebuilds it privately. A standalone source keeps its raw
main-file SHA-256. For a source with sidecars, the digest is SHA-256 of the bytes
`adaptive-planning-source-file-set-v1` followed by one NUL byte and UTF-8 JSON of
the ordered existing suffixes `""`, `"-journal"`, `"-shm"`, `"-wal"`. Each JSON
entry has `suffix`, `bytes` and `sha256`; keys are sorted and separators are
`,` and `:` without spaces. The private origin records this `file-set-v1`
binding and seals it with the restore event. It retains
`preserve_current` solely as private reconciliation evidence; it does not recover
post-backup registry entries or reconstruct absent workers. Restore rotates the
installation epoch and every plan generation and revokes retained capabilities.
It preserves native/imported constructor history and records exact private
restore lineage. Nonterminal ownership becomes stale/unresolved, including an
explicit transition and source-owned hold for already-stale native attempts.
Accepted history remains terminal. Old worker commands/replays are fenced.

Repeated restore validates the unique current sealed origin and its entire
epoch/host-linked ancestry on the same admitted source snapshot, even when all
attempts are terminal or absent. The supported lineage contains at most 128
ancestors. A malformed, cyclic, missing, contradictory, or deeper lineage is
refused before publishing `preserve_current` or replacing the authority; source
and current installation entries remain unchanged. Valid deeper histories are
conservatively unsupported and require an explicitly designed archival route.

Publication records the exact operation, original/preserved/staged image
identities and prepared/normalized/published phases in the private durable
`.planning-maintenance.json`. SQLite checkpoint/journal normalization precedes
closed-stage replacement; main-file rename and WAL changes are not one atomic
file-set transaction. Any pending manifest prevents normal startup. Preserve it
and its recorded files after interruption. Read its `operation_id` privately and
choose one explicit recovery decision:

```sh
uv run --frozen python -B -m adaptive_planning.admin recover --data-dir /private/planning --operation-id EXACT_RECORDED_ID --decision finish
uv run --frozen python -B -m adaptive_planning.admin recover --data-dir /private/planning --operation-id EXACT_RECORDED_ID --decision rollback
```

Recovery validates only that manifest and its exact images/adjacent crash phase;
it never guesses a newest backup. Rollback restores the recorded preserved epoch
and history while pending maintenance has blocked serving. An interrupted
rollback decision remains bound to its recorded stage and can resume. If current
authority is corrupt or contradicts the manifest, automatic restore/recovery
refuses. Keep the service offline, preserve a forensic copy of the entire private
directory (database, sidecars, credential, manifest and recorded images), and
have the installation owner explicitly select a verified image and manual offline
reconstruction procedure. Do not delete sidecars or initialize over the evidence.

## Restored inventory and ownership reconciliation

A current owner first rereads service epoch and plan generation, then takes over
using fresh request identity and exact current ownership/CAS assertions. An
inspection plan can be bootstrapped while held. Old-epoch creation retries fail;
an explicit new-epoch creation using an old creation ID creates a separate plan.
Resume existing plans by their plan ID and current generation.

The typed MCP `resolve_store_hold` command takes the exact restored
`backup_cutoff`, explicit `inventory_complete`, `absent_work_inventory`,
`resource_resolutions` and an observation `reason`. This is an administrator's
assertion about local absent work, not process discovery. Every `HostWork`
supplies its real `work_id`, actor, running/unknown/stopped state, canonical IDs
and/or complete host-attested claims, plus observation evidence. One work can
contain repository, workspace and external members. Declarations retain their
producer, previous members and actual physical history; completeness never
implies stopped. Resource-less/unknown identity keeps an exact source-owned
reason. Only the same work ID/actor with full claims and evidence can attach its
identity. Omitting a retained obligation rejects complete inventory.

Complete known inventory clears only the current restore reason and positively
covered ancestor inventory reasons. The shared store hold remains true for a
flag or any independent unresolved reason, including older imported identities.
Imported unknown context must attach through its genuine source before terminal
recovery; that resolves only its exact historical reason. Restored native/imported
logical release, reject, replace or retirement uses positive retained lineage
and clears only exact source-owned stale holds. It does not infer physical stops.

Known absent work stays physically unresolved after inventory clears, so an
overlapping assignment still conflicts. `ResourceResolution` selects exactly
one actual use/work; empty IDs select its full remaining orphan cohort, while
nonempty IDs select a distinct canonical subset. `stopped` is an explicit
assertion; `unresolved` keeps the obligation, and `isolated` is unsupported.
Existing `release_resource_use` accepts a retained real orphan member/anchor
only with the **full** remaining cohort and explicit stopped assertion. Subset
stops retain the stable original anchor and unselected conflicts. Inventory
cannot release another plan's ordinary uses. Global sequence changes invalidate
foreign coordination cursors without manufacturing foreign plan revisions.

`tests/service_admin_linux_exercise.py` provides `exercise` and
`verify-restart` phases for a coordinator-owned synthetic named volume. Run them
in different nonroot10001 Linux containers with network disabled and the tests
mounted read-only. Their concise JSON records actual runtime and domain checks;
the synthetic private state file stays `0600` in `/data`. This exercise does not
activate installed clients or accept/deploy an existing installation.

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

The current accepted Task 24 source catalog contains 29 tools, including six reads:
`read_service_info`, `list_plans`, `read_plan`, `read_events`, `list_ready` and
`read_coordination`. Twenty-three mutations are enabled: `create_plan`, `revise_plan`,
`takeover`, `register_resource`, `reconcile_resources`, `assign_step`,
`bind_availability`, `start_step`, `report_activity`, `record_checkpoint`,
`resolve_reassessment`, `invalidate_result`, `resolve_invalidation`,
`record_review`, `accept_step`, `reserve_verification`, `release_resource_use`
`reopen_attempt`, `import_plan`, `sweep_stale`, `resolve_attempt`,
`append_late_evidence` and `resolve_store_hold`. No declared future kinds remain
disabled at this checkpoint. The current interop suite expects all 29 tools and
an empty disabled-kind set through synthetic SDK clients. This source/SDK evidence
does not establish genuine installed-client tool discovery or worker acceptance;
those gates remain open with zero actual client runs.

### Roles, checkpoints and reconciliation

The service provides one generic framework-neutral instruction contract:
`publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work`.
Workflow document references and phase metadata are optional; no specific Superpowers sequence is required.

The authoritative MCP service stores the canonical plan graph, step lifecycle,
assignments, physical resource uses, audit events, and verifiable evidence references.
Detailed design/implementation documents and temporary workflow notes remain distinct.
Persist domain grants (orchestrator tokens, attempt tokens) only in explicitly private
client/session working records outside service public views. Do not assume automatic
client-session persistence across sessions or restarts. If no valid grant is retained
in a new session, perform explicit `takeover` with current generation and expected ownership
version. Takeover establishes new coordination authority but is not scope renewal.

Ordinary session resume must read events, unfinished ownership, holds, and reviews,
and obtain an authorized owner `sweep_stale` before dispatch or recovery. A direct
worker asks the owner to sweep. If its attempt is overdue, reread the latest version
after the sweep, then the owner confirms ownership or an unfenced current worker
publishes a recovery checkpoint.

Enforce worktree isolation for each assigned worker and declare physical resource claims
(repository, workspace, external) to avoid uncoordinated concurrent mutations. Workers
must maintain observable activity at least every five minutes; attempts reaching fifteen
minutes of inactivity reach the default stale threshold. Owner sweep and explicit
recovery are required. The service enforces declared resource coordination and
capability fences, not filesystem permissions or termination of host processes.

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
current owner CAS and `relay` equal to the exact current owner actor. Relays
must faithfully preserve worker identity in `origin_worker` and relay identity
in `relay`, and never invent checks, heartbeats, or activity signals. If work
must pause, the relay records a safe pause without fabricating progress.
Takeover fences old owners and relays while leaving coherent direct worker grants alive.
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

During an outage or service unavailability:
- An assigned bounded worker finishes verification and retains unpublished evidence locally in its private working record.
- New assignments and dependents wait.
- Do not spawn a fallback server or create a new canonical plan. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

A finished checkpoint carries outcome/check, full `ArtifactRef`, meaningful
artifact-bound evidence, origin/relay and reassessment. Its public
`checkpoint_id` is the actual global checkpoint event sequence represented as
a string. Passing submission leaves the attempt submitted and the step in progress.
Material reassessment creates provisional holds that the current owner must
classify explicitly. Acceptance requires the exact current attempt/version/
definition, full submitted artifact, passing checkpoint evidence, current
execution authorization and satisfied holds/prerequisites. A required review uses the
latest original matching verdict and evidence recorded by an independent reviewer;
the `Review` DTO has no relay field, and relayed reviews record the submitting actor
as orchestrator and original reviewer in `Review.reviewer`. A final gate also requires
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

### Legacy authority handoff and transferred plans

Transferred legacy plans use MCP as their only publisher authority. When migrating or importing an existing legacy live plan into the shared service, the legacy publisher must be explicitly stopped beforehand. The service records the original provenance, host ID, source directory, and original task ID.

The database enforces canonical single-import identity via `imports UNIQUE(host_id,source_directory_id,original_task_id)`. The MCP server cannot technically prevent an independently launched legacy publisher process from writing its own JSON files on disk; therefore, handoff is explicit: operators must stop the old legacy publisher, preserve the legacy source file and provenance, import once by canonical identity, and subsequently publish all updates to the transferred plan through MCP only. The service introduces no polling, mirroring, or write-through back to the legacy file.

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
