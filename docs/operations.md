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
