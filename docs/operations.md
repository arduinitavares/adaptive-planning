# Running the shared planning service

Run these commands from the directory containing `compose.yaml`, using Docker
Engine 28 or newer and Docker Compose. The service listens on `0.0.0.0:8790`
inside its container; the default Compose file publishes only `127.0.0.1:8790` on the host.
For this default local configuration, the dashboard example is
`http://127.0.0.1:8790/` and the MCP endpoint is `http://127.0.0.1:8790/mcp`.
Clients select a configured instance and discover its advertised URLs through
`read_service_info`; see the [instance rules](../SKILL.md#select-and-retain-one-instance).

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

## Independent deployments

Each instance owns a separate database and installation credential.
Clients connected to one instance share that instance's plans and reservations.
Independent instances do not synchronize saved state. Operators must coordinate
physical resources that are used through different instances.

On separate Docker hosts, installations can use the same default port.
On the same host, configure distinct published ports and distinct data volumes
before initialization. The explicit volume name `adaptive-planning-data` is
used as written. A different Compose project name does not isolate it.
See Docker's [volume name rules](https://docs.docker.com/reference/compose-file/volumes/#name).
Review the merged Compose configuration for each instance. Verify its port
bindings, volume identity and service URLs before starting it. Reuse a volume
only for its existing installation. This guide adds no deployment abstraction.

Actual endpoints and credentials belong in private deployment and client
configuration. A private preference can name a configured MCP entry for new
plans. An existing plan retains its selected instance until an explicit
migration decision; this service provides no cross-instance migration or replication.

## Optional trusted-LAN access

LAN access is opt-in through `compose.lan.yaml` on any Docker host. Choose that
host's actual RFC1918 private IPv4 address in a private `.env`; the overlay adds
that exact address on port 8790 alongside the existing `127.0.0.1:8790:8790`
mapping. Any device on that trusted LAN can read the dashboard at the chosen
`http://<host-private-IP>:8790/` URL. MCP at the same base URL plus `/mcp` and
mutations require the service bearer.
Plain HTTP is a trusted-LAN-only mode, not internet exposure. Do not publish
the port on a global host address or forward it from the internet. Clients
connect directly over the LAN; Jump Desktop is not the transport.

Before any build, update, or start, record the existing service's Compose project
name (from its `com.docker.compose.project` container label) and confirm that
its `/data` mount uses the installation's `adaptive-planning-data` volume.
Preserve both identities. Do not start a second service or initialize another
installation. From the existing checkout, copy the example only if no `.env`
already exists:

```sh
if [ ! -e .env ]; then cp .env.example .env; fi
```

Edit the private `.env` to set `ADAPTIVE_PLANNING_LAN_IP` to the Docker host's
actual address (`10/8`, `172.16/12`, or `192.168/16`). The example leaves it empty
so LAN exposure requires an operator choice. If the existing deployment used a
project override, uncomment `COMPOSE_PROJECT_NAME` and set it to that exact
existing name. If it used the default project name, retain the same project
directory and default identity. Keep installation values and credentials out of
tracked files; `.env.example` contains only generic settings.

Docker Compose loads `.env` for configuration interpolation. The commands here
select it explicitly with `--env-file .env`; use `--env-file <private-file>` for
another private file. Shell variables override file values, so clear stale
exported LAN or project settings or align them deliberately. A `-p` option
overrides `COMPOSE_PROJECT_NAME`; if used, it must name the existing project.
See Docker's [interpolation rules](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
and [project-name rules](https://docs.docker.com/compose/how-tos/project-name/).

```sh
docker compose --env-file .env -f compose.yaml -f compose.lan.yaml config
```

Before starting, inspect the merged configuration: its project name must match
the existing deployment, and it must contain both exact host port mappings,
the existing volume, the local base URL, and `ADAPTIVE_PLANNING_LAN_BASE_URL`
equal to `http://<host-private-IP>:8790`. An absent or empty
`ADAPTIVE_PLANNING_LAN_IP` fails overlay interpolation. `.env` is not passed
wholesale into the container; the overlay's `environment` stanza explicitly
maps the LAN base URL.

Build the candidate image first using the existing
[update and rollback procedure](#deployment-update-and-rollback), preserving the
previous verified image and the same Compose project and volume. Record the new
image ID before starting it. Only after that build, enable the overlay with:

```sh
docker compose --env-file .env -f compose.yaml -f compose.lan.yaml up -d --no-build --pull never adaptive-planning
```

Verify the running image ID and installation continuity as described in that
procedure. Enabling LAN reuses the existing database and credential; it does
not rotate or copy them.

To stop the service while retaining its data:

```sh
docker compose --env-file .env -f compose.yaml -f compose.lan.yaml stop adaptive-planning
```

To disable LAN and resume the loopback-only configuration:

```sh
docker compose --env-file .env -f compose.yaml up -d --no-build --pull never --force-recreate adaptive-planning
```

Outside Compose, the Python service does not load `.env`; set
`ADAPTIVE_PLANNING_LAN_BASE_URL` in its actual process environment to plain HTTP with one
canonical RFC1918 IPv4 literal (`10/8`, `172.16/12`, or `192.168/16`) and an
explicit port matching `ADAPTIVE_PLANNING_BASE_URL`. Paths, trailing slashes,
credentials, queries, fragments, whitespace, DNS names, IPv6, wildcards, and
other addresses are rejected. Leaving the LAN variable unset retains the
default loopback-only boundary. The configured loopback URL remains accepted
with LAN enabled, so existing local client entries need no changes. A supplied
Origin must match the request's Host identity; forwarding headers and duplicate
Host/Origin/Authorization values remain rejected, and no CORS grants are added.

With LAN enabled, `read_service_info` and plan/dashboard links advertise the LAN
URL to every client. Disabling LAN returns advertised links to the local URL;
installation identity, plan IDs, and saved state remain shared. Provision the
existing bearer privately on each client host using the [Codex](clients/codex.md#optional-remote-lan-connection)
or [Antigravity CLI](clients/antigravity.md#optional-remote-lan-connection) examples.

### LAN acceptance scope, 2026-10-13

The LAN feature has automated coverage using a temporary database, synthetic
credentials, and the official MCP SDK against a loopback listener carrying
LAN Host/Origin headers. Actual Compose merge, deployment, cross-host
network reachability, and installed CLI/IDE calls require separate coordinator
verification. These instructions do not claim real Windows or IDE acceptance
and do not extend earlier dated local-client results to LAN.

## Persistence and lifecycle

All installation state, including `planning.sqlite` and `service-token`, stays in
the named volume `adaptive-planning-data` mounted at `/data`. The default deployment has no
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
4. Clients must not launch fallback servers or create competing authorities for
   the unavailable plan. Deliberately configured independent instances can serve
   other plans.
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

Record the previous verified image ID before rebuilding the mutable `adaptive-planning:local` tag.
Use the currently verified service container, rather than assuming the mutable tag
still identifies it. Set `rollback_image` to an unused, installation-owned local
tag, such as `adaptive-planning:rollback-update-id` (replace `update-id`), then:

```sh
verified_container=$(docker compose ps -q adaptive-planning)
previous_image_id=$(docker inspect --format '{{.Image}}' "$verified_container")
docker image tag "$previous_image_id" "$rollback_image"
docker image inspect --format '{{.Id}}' "$rollback_image"
```

Save the previous image ID and rollback tag in the update record. Confirm that the
tag's inspected ID equals the recorded container image ID before proceeding.
Retain that image and tag until the update is accepted; do not prune them.

To update the service deployment image:
1. Rebuild the image: `docker compose build adaptive-planning`. Record its image ID
   with `docker image inspect --format '{{.Id}}' adaptive-planning:local`.
2. Stop the current container: `docker compose down`.
3. Start that local image: `docker compose up -d --no-build --pull never adaptive-planning`.
4. Verify that the new container's inspected `.Image` equals the recorded new image ID.
   The named volume `adaptive-planning-data` is preserved intact. Verify schema version,
   epoch, and plan continuity via `read_service_info` and the dashboard at `http://127.0.0.1:8790/`.

If an update regresses, stop the failing container with `docker compose down` and
choose one of the following paths. Keep the installation's original Compose
project and named volume; never add `-v` to `down`.

Rollback using the retained verified image:
1. Recheck that `docker image inspect --format '{{.Id}}' "$rollback_image"` equals
   the saved previous image ID. Stop if the tag is absent or identifies another image.
2. Create a private `rollback.compose.yaml` override selecting that exact retained tag:

   ```yaml
   services:
     adaptive-planning:
       image: adaptive-planning:rollback-update-id
   ```

3. Inspect `docker compose -f compose.yaml -f rollback.compose.yaml config`: the
   selected image must be the retained tag and `/data` must still use
   `adaptive-planning-data`. The base file's `build` stanza remains, so prevent
   rebuilding or pulling when starting this image:
   `docker compose -f compose.yaml -f rollback.compose.yaml up -d --no-build --pull never adaptive-planning`.
   Keep using these same Compose files while this rollback image is selected.

Rollback from a verified source checkout:
1. Select the previous verified source commit in the installation checkout, then
   explicitly rebuild it with `docker compose build adaptive-planning`. A checkout
   followed only by `up` can reuse the regressed `adaptive-planning:local` image.
2. Record the rebuilt image ID using
   `docker image inspect --format '{{.Id}}' adaptive-planning:local`. Rebuilds may
   differ from the retained image; qualify the rebuilt image against the platform
   checks below rather than assume byte-identical output.
3. Use the source checkout's base Compose file, without the retained-image override:
   `docker compose up -d --no-build --pull never adaptive-planning`.

After either rollback path:
1. Obtain the container ID with `docker compose ps -q adaptive-planning`, including
   the same `-f` options when using the override. Check
   `docker inspect --format '{{.Image}}' "$service_container"` against the selected image ID
   recorded for that path. Stop and investigate any mismatch before accepting rollback.
2. Verify that `adaptive-planning-data` is still mounted at `/data`, and reread
   schema version, epoch, and existing plan continuity through MCP and the dashboard.
   Changing images does not undo database changes. If corruption or schema compatibility
   requires a backup restore, use the explicit administrative restore procedure with
   `adaptive_planning.admin restore`, `--preserve-current`, and recorded maintenance
   manifest recovery decisions.

Docker documents [tagging an image by ID](https://docs.docker.com/reference/cli/docker/image/tag/),
[Compose override selection](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/),
and the [`up` build and pull controls](https://docs.docker.com/reference/cli/docker/compose/up/).

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

Unsupported maintenance must not reassign plan rowids; do not run `VACUUM` or
`DELETE` plans under the compact plan-index collection fence. Supported migration,
backup, and restore operations must preserve rowid mapping or invalidate the
installation epoch.

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

## Two-client planning gate <a id="three-client-planning-gate"></a>

Configure the actual chosen surfaces using the [Codex](clients/codex.md) and
[Antigravity](clients/antigravity.md) guides. The [Copilot](clients/copilot.md) guide
is preserved for historical reference (superseded for the current gate by explicit user choice).
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
4. A fresh reopened session of one of these two products resumes that same plan,
   reads the latest canonical draft, and appends the third distinct fact through
   another revision using the same rules. All clients must discover the same
   epoch, host ID, generation and plan dashboard URL; the three successful
   commands have distinct committed revisions.
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
| Copilot | Historical guide example; superseded by user choice | Unsupported for current release gate | N/A | N/A | Excluded from current gate |

## Two-client worker checkpoint gate <a id="three-client-worker-checkpoint-gate"></a>

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
- Do not spawn a fallback server or create a competing authority for this plan.
  Deliberately configured independent instances can serve other plans.
  Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
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
| Copilot: historical guide example | Superseded by user choice; unsupported for current gate | Superseded | Superseded | Superseded |

Each lifecycle row must compare before/after assignments, evidence, revisions,
result identities and URLs while preserving accepted work without redispatch.
Rebuilding an app over a synthetic store is source evidence, not the actual
service/container lifecycle gate. Retain NOT RUN, failed or unsupported rows and
specific call/allowance gaps. This checkpoint grants no provider retry, account/
billing change or client activation to close those gaps. Raw test evidence stays
private; complete captures can contain diagnostic data and are not universally
secret free.

## Task 26 source contracts and release evidence

The following source evidence is historical. Its dates, versions and limits
apply to the recorded candidate. Later results are summarized in
[release verification](release-verification.md); they do not activate a reader's deployment.

This source candidate preserves the 29-tool catalog: six reads and 23 semantic
mutations. `registered_handlers(service)` issues one mapping consumed once by
`build_mcp`. Registration validates exact function identity, semantic name, read
or mutation role, and common issuance batch before SDK registration, without
requiring a plan or invoking handlers. Missing/unknown/replaced/swapped/copied
attributes/mixed batches refuse; copying the genuine mapping is valid. Every
mutation checks its kind, uses the real typed command parser, and resolves the
current `service.execute` at invocation. Weak references and short synchronized
bookkeeping make issuance collectible; this does not confine malicious trusted
Python code.

All six MCP read serializers and safe error `current_metadata` use the same
checked public projection. It accepts exact declared public DTO shapes and valid
JSON, detaches containers, and rejects typed private grants/receipts (including
receipts with `private=None`), unknown/internal DTOs or altered DTO/scalar/sequence shapes, cycles,
nonfinite numbers and nonstring keys with constant diagnostics. Valid arbitrary
public metadata and prose, including labels `private`, `orchestrator_token` and
`attempt_token`, remain intact. Intentional private mutation receipts retain
their grants. Already flattened JSON or a credential embedded in arbitrary
permitted prose needs a separate provenance/schema policy; this guard is not
universal secret detection or a dashboard serializer guard.

Verification was performed on 2026-10-09 from the accepted Task25 base
`bfe4adfe27622231562729c3e25f672f8cc425eb`. The final code SHA256 is
`91371443d8ed028fd62846a681fabf14efbc5748675d32f58eaea8979ff61851`;
new matrix test SHA256 is
`e1dd436ac9909f459a624e11cd2b7f012f288a47396c1377ee2eda8c52c4080e`.
Complete private capture frames retain exact argv, exit, UTC times, full combined
stdout/stderr bytes/hash, selected runtime and 71 before/after source hashes.
Operations evidence is appended afterward; the verified code and tests remain
identical. Private evidence filenames below identify coordinator-held records,
not files shipped in the package.

| Check and exact command | Actual result / evidence |
| --- | --- |
| `uv run --frozen python -B -m unittest tests.test_service_acceptance_matrix -v` | Initial RED: missing-validator assertion, 1 failed; expanded RED: 14 tests, 19 failures/6 derivative errors. `red-01.txt`, `red-02.txt`. Final scoped GREEN: 14 passed, clean unittest output, `green-03.txt`. |
| `uv run --frozen python -B -m unittest tests.test_service_acceptance_matrix tests.test_service_sdk tests.test_service_http tests.test_service_interop -v` | 75 passed; `green-02.txt`. Existing diagnostics and synthetic public interop summaries retained; this combined output is not pristine. |
| `uv run --frozen python -B .superpowers/final_python_suite.py` | 815 discovered, 814 passed, one actual-Windows-only handle-sharing skip; `final-python-01.txt`, body SHA256 `b9c1017467c5ef1923899d51a4d5fe534387e80840a0c905b3a610b8ad3dc777`. CPython3.13.15/SQLite3.53.1, subprocess-only selected runtime, downloads disabled. |
| Full discovery includes `tests.test_service_packaging.TestService.test_wheel_assets_and_import_safety` and `test_sdist_assets_and_import_safety` | Built and installed each artifact in new environments outside checkout; actual package imports, packaged assets, ASGI routes, console entry/CLI help and import safety passed in the same full capture. No checkout fallback; inner frozen/hash-pinned installer commands were not `--offline`. |
| `node --check assets/dashboard/app.js`; `node tests/test_frontend_dom.js` | Both exit0 on Node26.7.0; `final-node-01.txt`, `final-dom-01.txt`. DOM script is synthetic, not actual browser evidence. |
| `uv build --offline` | Exit0, uv0.10.12; `final-build-01.txt`. Wheel SHA256 `76132854f4307e70a59b921fb22547f35dd24269902a60ca2a5b00695aec6687`; sdist SHA256 `42f0e24ae333e77ef49aa8c6f2b4da922dfbdd9dd784f7646839a5611eb8576a`. |

The approved full-discovery wrapper forwards
`["unittest","discover","-s","tests","-p","test_*.py"]` to the standard
runner. Its SHA256 is
`5fd35b740dea8582f8ddf9d54d160d3530d8bc3e32c147c6d9f748a1a474a8a4`.
It limits only the output stream handler to WARNING while test-owned diagnostic
capture remains active. No warnings filter or production logging change was
applied. Expected negative-path warning/error tracebacks remain in the full
capture; the root audit counted 390 warning-containing lines, not 390 events.
The first noisy GREEN is preserved; only the final 14-test scoped capture is
qualified as clean output. A passed full suite is not a pristine-output claim.

The approved matrix contains 52 requested test labels: 45 exact method names
exist and seven older shorthand labels map to these actual executed tests.
These are explicit mappings, not claims that absent method names ran:

| Approved shorthand | Actual current test/evidence |
| --- | --- |
| `test_backup_marker_and_export_redaction` | `test_local_cli_produces_private_coherent_backup_and_public_export` in `tests/test_service_admin.py` |
| `test_isolated_replacement_keeps_old_resources` | `test_isolated_replacement_keeps_old_resources_and_fences_checkpoint_retry` in `tests/test_service_recovery.py` |
| `test_submitted_overdue_requires_explicit_confirmation` | `test_submitted_overdue_requires_explicit_confirmation_then_reopen_same_attempt` in `tests/test_service_recovery.py` |
| `test_restored_invalidation_verification_waits_for_inventory` | `test_actual_restore_preserves_open_invalidation_and_gates_writable_verification_until_inventory` in `tests/test_service_admin.py` |
| `test_restore_holds_absent_postbackup_work` | `test_typed_inventory_clears_hold_without_reconstructing_postbackup_registry`, the preceding real restored-invalidation guard, and qualified Linux admin exercise |
| `test_stale_checkpoint_and_fenced_late_evidence` | `test_recovery_checkpoint_is_event_only_and_confirmation_preserves_original_review`, replacement checkpoint fencing above, and `test_late_owner_evidence_only_appends_note_and_stop_report` |
| `test_normal_restart_not_restore` | Exact `test_restart_preserves_epoch_and_events`, qualified Linux admin `verify_restart`, and fresh owned SDK/container lifecycle observations |

The historical acceptance matrix below records automated evidence, bounded
owned-fixture observations, historical reuse and genuine-client release gates
from prior tested snapshots. The 52 approved test labels use the explicit seven
mappings above where necessary.

All records in this section—including full discovery, Linux image, browser, and
hash comparisons—are bound to those earlier verified snapshots (such as base
`bfe4adfe27622231562729c3e25f672f8cc425eb`). Full suite qualification, Linux
container image verification, and installed two-client acceptance for the current
candidate remain pending post-closure gates; these prior results do not attest to
subsequent source or asset changes.

| Design check / review focus | Prior snapshot evidence | Additional observation or remaining gate |
| --- | --- | --- |
| 1: two-client tools/read/checkpoint | Full discovery in prior snapshot passed official SDK/protocol and synthetic worker-cycle tests; matrix exercised all 29 actual handlers. | Fresh post-closure Codex/Antigravity acceptance remains pending; prior-candidate CLI proofs are recorded below. Copilot is excluded from the current gate. |
| 2: persistence/resume | Restart persistence test passed in prior snapshot. | Prior owned stop/start, restart and same-volume recreation results are recorded below. Actual installed-client close/reopen and the default Compose lifecycle gate remain OPEN. |
| 3: assignment/resources/availability | Full discovery passed barrier/concurrency, physical/path identity, overlapping repository/external-resource and checked-base tests. | These are scoped source-test fixtures; no additional user worktree or shared resource was activated. |
| 4: idempotency/fencing | Full discovery in prior snapshot passed lost-bootstrap-response, request replay/CAS, capability and takeover/fencing tests; matrix confirmed real parse/dispatch and read/error token boundaries. | Prior outage replay used the retained original key/body; it did not simulate an actually lost server response. |
| 5: frozen definitions/adaptation/review | Full discovery passed frozen-definition, atomic material holds, review binding, corrective history, retirement and invalidation tests. | Qualified Linux admin restore/hold observations are described below. Synthetic reviewers establish API behavior, not independent human/AI review. |
| 6: evidence/interleaving | Full discovery in prior snapshot passed checkpoint and interleaving tests. | Prior owned browser observations below retain two independent local workers' actual arithmetic outcomes, evidence and reassessment; failed work stays blocked. |
| 7: scope | Full discovery passed plan-only/global-latch/active-import tests. | No takeover, restart or import in this candidate grants additional task authorization. |
| 8: stale/relay/resources | Full discovery passed injected-clock, replacement/fenced evidence and exclusive-workspace handoff tests. | The browser shows that host edits are uncontrolled. No actual host stop or elapsed-time heartbeat is invented. |
| 9: dashboard | Full discovery in prior snapshot passed dashboard freshness/projection tests; Node DOM script passed. | Prior owned browser viewport/CSP/escaping/holds/reviews/reconnect observations are recorded below with explicit bounds. Historical Task8 evidence is qualified separately. |
| 10: unavailable behavior | Both instruction/unavailable and relay tests passed in prior full discovery. | Prior owned fixture literally stopped its service during assigned work and retained unpublished observations without creating a fallback server/plan. |
| 11: legacy/import/restore | Full discovery in prior snapshot passed legacy and import/restore tests, including the mapped shorthand methods. | Accepted Linux core/admin backup/export/restore/newer-schema refusal is reused only for unchanged directly exercised source and runtime bindings. |
| 12: container/auth | Full discovery passed Host/Origin/auth, logs/URLs and container-contract tests. | Prior owned Linux inspect and authenticated protocol observations are recorded below. Known-token scans have stated coverage; they are not universal secret detection. |
| Review Focus 1: superseded private receipts/fencing | Lost-bootstrap and cached-mutation tests passed; new matrix preserves intentional private mutation receipts and rejects them on reads. | No restored/cached grant is treated as authority. |
| Review Focus 2: physical/canonical resource identity | Alias/device/path/reconciliation tests passed. | Source-fixture attestations retain conservative matching and explicit case behavior. |
| Review Focus 3: atomic checkpoint/holds | Concurrent atomic-hold tests passed. | Browser observation is additional committed-state evidence, not the atomicity proof. |
| Review Focus 4: absent work after restore | Actual restored-invalidation and typed-inventory tests passed. | Qualified unchanged Linux admin exercise retains the whole-store hold until inventory reconciles absent host work. |
| Review Focus 5: relay/outage honesty | Relay and unavailable tests passed. | Actual owned outage observations are bounded local checks with explicit origin; no fabricated host stop, heartbeat or AI-client checkpoint. |

The prior owned Linux/browser fixture ran once under each separately approved
producer version. Final exact command
`uv run --frozen python -B .superpowers/native-linux-release-v5.py` exited0;
`linux-release-04.txt` preserves all 989 child-output bytes, SHA256
`ba197984af1ef8322c69e10b4dbc6078399c4faa7c1f4d17e026a463b98fb53e`.
Public observations are in
`native-linux-public-evidence-f5c8eeeeddf74cd88054ea8a107bd895.json`, SHA256
`de21c9be874e7a235a68949976cb248ca796aae8af13b339422df33e3fdb57fa`.
The prior 71-path snapshot SHA256 was
`0571695bc9c4fa3e6d99d0dc0a6fbb9f786293d5ef681a0da36a4f3183cfe766`;
all bindings stayed identical during that run. The later operations append was
outside that built snapshot; product code/tests and all 22 installed
production/asset byte hashes matched that completed Python and runtime evidence.
Verification of current candidate bytes and assets remains pending post-closure
execution.

The prior tested uniquely owned image ID was
`sha256:8d48534cd7f44d8d42308a9d70b66a1e670767a04cf3d986d3a9f2eb21b50835`.
Actual Docker context was `desktop-linux` through its Unix socket. Inspect
confirmed Linux/arm64, UID/GID10001, read-only root, loopback-only
`http://127.0.0.1:64405`, a fresh named volume and no host bind mounts. The real
container port, external base URL, metadata/dashboard URLs and allowed Host
used that observed ephemeral port; this did not activate the default8790
service. The installed package loaded from `/app/.venv`, with CPython3.13.16,
SQLite3.53.4, serialized thread safety3, MCP2.3.0 and Pydantic2.13.5.
SQLite loaded `/usr/local/lib/libsqlite3.so.0`, SHA256
`504724703dbad6b3820e6db9de8607cd0d14419bab5d3b830f008d4ee293d09a`;
source ID and complete compile options are retained in the observation record.

All six actual authenticated MCP reads succeeded. The actual SDK listing
contained exactly29 tools with no next cursor; negotiated protocol was
`2026-07-28`. Literal stop/start and restart preserved the same container ID;
stop/remove/recreate used a different container ID and the same owned volume.
Each of the three completed lifecycle comparisons retained both exact plan
DTOs, event lists, metadata/URLs and accepted results without redispatch.
The unavailable-service exercise retained actual local CSV verification and
unpublished observations during real read/publish `ConnectError`, then reread,
sweep/reconcile and replayed the identical command body/key without duplicate
events. Its separately recorded review is explicitly a local simulated role.
These owned CLI operations are bounded source/runtime evidence, not the actual
installed-client or default `docker compose` lifecycle gate, nor real Mac sleep.

In that tested run, the browser was isolated headless Google Chrome153.0.8010.50, using
installed Playwright CLI0.1.22/Playwright1.64.0-alpha-1790635538000 with no new
browser/dependency download or global profile/configuration change. At
1440x1000 it observed two plans on the index with optional workflow omitted,
two independently connected/authorized active local workers, and both actual
arithmetic outcomes: sum5 passed, product6 failed the expected7 check. Both
original checkpoint evidence/reassessment payloads appeared intact in the
browser DOM; failed work stayed blocked, with two submitted attempts, one hold
and one original passing local simulated review. Escaped script/image-looking
text remained text, the actual CSP prohibited inline/eval execution, and no
fixture execution flag or executable evidence element appeared. The actual
service outage displayed the reconnect banner and retained revision8/store
sequence14; reconnect and same-volume recreation hid the banner and retained
those values plus holds/reviews. Viewport screenshots are retained; lower
holds/reviews were checked in the parsed DOM and are not a full-page visual
claim. User-opened-details retention, exhaustive interaction/storage inspection
and exact polling timing were not newly established.

There were60 captured Docker operations and40 captured browser CLI operations.
Cleanup independently inspected the exact owner/source/image/port/volume
bindings, then closed only the private named browser and removed only the owned
container and volume; subsequent registry/inspect calls showed absence. The
private credential directory is absent; uniquely owned images and private
captures are retained. Each main run had a finite900s total, subprocess30s
(build600s), async-call12s and readiness20s bounds, with separate180s cleanup;
synchronous children have their own timeout and are not preempted by asyncio.
Bearer and issued grants stayed in memory or private0600 files. Published
observations and selected outputs were checked for known issued values; before
token acquisition the known-secret set was empty. The reused private SDK helper
disables client logging, so absent client logs do not establish leak-free
production logging. Production logging and log-absence tests are unchanged.

Earlier failed owned attempts are preserved: `linux-release-01.txt` failed
initial readiness because the actual configured container port differed from
the publish target; `linux-release-02.txt` failed after successful runtime,
worker/browser and offline observations (no exception leaf was preserved; the
subsequent URL assertion correction followed unchanged source); and
`linux-release-03.txt` passed all six reads/first-plan equality before the
captured `TypeError` at the SDK listing assertion. Its `Client.list_tools`
result was a `ListToolsResult`, requiring `.tools`. Each corrected producer was
fully inspected and approved separately before a single execution. No product
change, automatic retry or prior-attempt PASS was manufactured.

Accepted Task23 Linux admin evidence
`task-23-linux-exercise-c230c48992ee.json` and its root validation are reused only
for directly exercised unchanged core/admin backup/export/restore,
newer-schema refusal, absent-work inventory hold and normal-restart behavior.
Exactly27 of its30 source bindings still match; transport, operations and import
tests differ, so the old image establishes neither the new transport nor a
whole-current-source PASS. The actual admin exercise source SHA256 remains
`749ae4beffcaf18c8e82be57e59ffcab776bd0aea3c2363c938543b087d4ff75`;
that tested runtime independently confirmed the same Linux Python/SQLite
versions and loaded-library identity. Historical Task8 browser records retain
14 passed/5 UNKNOWN at their original Chrome153.0.8010.50/Python3.14.3 runtime.
Only index/style assets and the HTTP test still match that browser catalog;
changed dashboard JavaScript/production/test sources prevent carrying those
functional PASS claims into this candidate. Prior browser observations rely on
the bounded observations from that run; historical unknowns are retained, and
current browser verification is recorded in the dated closure evidence below.

See the [dated release verification](release-verification.md) for the later
source and CLI observations and their scope limits.

## Current closure evidence, 2026-10-13

See the [dated release verification](release-verification.md). These historical
results do not establish activation or runtime acceptance for another installation.

## Local activation evidence, 2026-10-13

See the [dated release verification](release-verification.md). These historical
results do not establish activation or runtime acceptance for another installation.

## Native client acceptance completed, 2026-10-13

See the [dated release verification](release-verification.md). These historical
results do not establish activation or runtime acceptance for another installation.
