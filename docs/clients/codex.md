# Codex client setup

Configure installation bearer credentials separately from each endpoint.
Codex CLI and Desktop require their own runtime verification.

## Select and resume an instance

The examples below use two distinct named connections. The loopback URL is
a local example. `192.168.1.10` is a synthetic RFC1918 address, not a deployment
requirement. Replace each example URL with the operator-supplied endpoint.
Provision each installation's own bearer credential privately.

Use the [instance selection and resume rules](../../SKILL.md#select-and-retain-one-instance).
For a new plan, an explicit task choice precedes a private project preference,
then a private user preference, then the single configured instance.
Ask before creating a plan when several services remain eligible.
If a selected or preferred entry is missing or unavailable, report the gap.
Ask before selecting another instance for a new plan.
Do not silently fall back. Report a missing configuration when none is available.

For new and resumed plans, use `read_service_info` through the selected MCP entry.
Use its `dashboard_url` and returned plan URLs.

For an existing plan, retain its MCP entry, `host_id`, last observed `epoch`,
and `plan_id` in a private task/session working record.
Read the expected plan before resuming. Compare `(host_id, epoch)` for aliases
through trusted connections; a URL or host ID alone is insufficient.
Do not create duplicate canonical plans through different aliases.
These checks do not provide cryptographic proof against copied databases.
Follow restore/recovery on an epoch change with the same host ID.
Stop plan mutations on an unexpected host ID. Obtain an explicit recovery
or migration decision. A preference change does not move an existing plan.

Independent instances do not automatically share plans, grants, events, or
resource reservations. During an outage, retain the affected plan's authority.
Its assigned bounded worker can retain unpublished local verification;
new assignments and dependents wait. Other deliberately configured instances
can serve other plans. Operators coordinate resources across instances.

For **Codex CLI**, the supported environment-based registration is:

```sh
codex mcp add adaptivePlanningLocal --url http://127.0.0.1:8790/mcp --bearer-token-env-var ADAPTIVE_PLANNING_TOKEN
```

Its user TOML entry is:

```toml
[mcp_servers.adaptivePlanningLocal]
url = "http://127.0.0.1:8790/mcp"
bearer_token_env_var = "ADAPTIVE_PLANNING_TOKEN"

[mcp_servers.adaptivePlanningRemote]
url = "http://192.168.1.10:8790/mcp"
bearer_token_env_var = "ADAPTIVE_PLANNING_REMOTE_TOKEN"
```

Provision `ADAPTIVE_PLANNING_TOKEN` privately into the environment of the actual
CLI process before launch. The command names the variable; it never needs the
token value in its arguments. Do not print the environment or credential.

**Codex Desktop is a distinct surface to verify.** A terminal export and successful
CLI test do not prove that an already running Desktop process inherited the
variable. Desktop needs verified GUI environment provisioning, or a private user
TOML entry using a literal Authorization header instead of `bearer_token_env_var`:

```toml
[mcp_servers.adaptivePlanningLocal]
url = "http://127.0.0.1:8790/mcp"
http_headers = { Authorization = "Bearer SYNTHETIC_ONLY" }

[mcp_servers.adaptivePlanningRemote]
url = "http://192.168.1.10:8790/mcp"
http_headers = { Authorization = "Bearer SYNTHETIC_REMOTE_ONLY" }
```

`SYNTHETIC_ONLY` is an example value. Never install that placeholder in real client
configuration. With execution authorization, edit the private user configuration
through a local channel, preserve existing entries, and enforce file mode `0600`
and a private parent directory. Keep real values out of CLI arguments, repository
files, transcripts and reports. Record only the surface and provisioning method.

The [official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
documents `url`, `bearer_token_env_var` and static `http_headers`. Its
`env_http_headers` mapping is a separate mechanism, not this bearer-token helper.
The [command reference](https://learn.chatgpt.com/docs/developer-commands)
documents `/mcp` and `/mcp verbose` for session diagnostics. A configured entry or
status panel alone does not prove authenticated discovery or a committed event.

Complete the [two-client planning gate](../operations.md#two-client-planning-gate)
using the actual chosen CLI or Desktop surface. Record the installed version,
observed MCP negotiation, fresh tool list, installation identity, plan ID,
committed revision, dashboard URL and creator-close/resume evidence. App-server
initialization is separate from downstream MCP negotiation; its status schema
does not provide a negotiated `protocolVersion` field. The public
[app-server API](https://learn.chatgpt.com/docs/app-server) advertises explicit MCP
calls, but schema availability alone does not prove installed runtime dispatch,
gating or inference-free behavior. Verify that route before using it for the gate.

## Optional remote LAN connection

After the operator enables the [LAN overlay](../operations.md#optional-trusted-lan-access),
obtain that instance's base URL. The stock overlay uses port 8790. A customized
deployment must advertise its configured port. Use the exact operator-supplied
URL, without a trailing slash, in the selected remote entry. Keep the local
entry intact when adding an independent remote instance.

For a new Codex CLI entry, this PowerShell example uses a distinct name:

```powershell
$env:ADAPTIVE_PLANNING_SERVER_URL = Read-Host "Base server URL supplied by the operator"
codex mcp add adaptivePlanningRemote --url "$env:ADAPTIVE_PLANNING_SERVER_URL/mcp" --bearer-token-env-var ADAPTIVE_PLANNING_REMOTE_TOKEN
```

In static TOML, enter the actual endpoint as a literal `url` in the selected
entry. The server's `.env` does not populate client configuration.
PowerShell expands the URL in this CLI command. Do not assume interpolation
in a static TOML `url`. Provision that instance's bearer into
`ADAPTIVE_PLANNING_REMOTE_TOKEN` privately in the actual CLI environment.
Preserve unrelated entries and settings if the named remote entry already exists.
Changing an entry's endpoint does not authorize resuming a plan on a different
instance. Apply the selection and identity checks above before plan mutations.

Freshly discover tools and call `read_service_info` through the selected entry.
Use its advertised links. Desktop environment provisioning requires its own
verification. Plain HTTP is a trusted-LAN mode. Dashboard reads are public on
that network; MCP and mutations require the installation bearer.

## Instruction contract, domain grants, and session lifecycle

Codex operates under the generic framework-neutral instruction contract:
`publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work`.
Workflow document references and phase metadata are optional; no specific Superpowers sequence is required.

### Domain grants and session persistence

Persist domain grants (orchestrator tokens, attempt tokens) only in explicitly
private client/session working records outside service public views. Do not
assume automatic client-session persistence across Codex restarts or new sessions.
If a new session begins without a retained valid orchestrator grant, perform an
explicit `takeover` with the current generation and expected ownership version.
Takeover establishes new coordination authority but is not scope renewal; execution
boundaries remain strictly bounded by user instructions.

### Resume and stale recovery sequence

Ordinary session resume must read events, unfinished ownership, holds, and reviews,
and obtain an authorized owner `sweep_stale` before dispatch or recovery. A direct
worker asks the owner to sweep. If its attempt is overdue, reread the latest version
after the sweep, then the owner confirms ownership or an unfenced current worker
publishes a recovery checkpoint.

### Worktree isolation and resource obligations

Enforce strict worktree isolation for each worker and declare physical resource claims
(repository, workspace, external) to avoid uncoordinated concurrent mutations. Workers
must maintain observable activity at least every five minutes; attempts reaching
fifteen minutes of inactivity reach the default stale threshold. Owner sweep and
explicit recovery are required. The service coordinates declared resources and
capability fences; workers and operators enforce actual filesystem isolation and stops.

### Missing-MCP worker relay and safe pause

When a dispatched worker lacks direct MCP tools, an authorized relay submits
`start_step`, `report_activity`, and `record_checkpoint` using both orchestrator
and attempt grants. The relay faithfully preserves the worker's identity in
`origin_worker` and the relay's identity in `relay`, and never invents checks or
heartbeat signals. For review, `record_review` records the original independent
reviewer in `Review.reviewer` and the submitting actor as orchestrator; the `Review`
DTO has no relay field. If work must pause, the relay records a safe pause without
fabricating progress.

### Outage policy

If the MCP service or dashboard becomes unavailable during execution:
- An assigned bounded worker finishes verification locally and retains unpublished evidence in its private working record.
- New assignments and dependents wait.
- Do not spawn a fallback server or create a competing authority for this plan.
  Deliberately configured independent instances can serve other plans. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

## Worker checkpoints and client verification

The installation bearer authenticates MCP requests. A plan's private owner grant
authorizes owner operations; assignment returns a separate private attempt grant.
Direct worker `start_step`, `report_activity` and `record_checkpoint` commands
carry only that attempt token, the exact attempt/version/definition, null global
revision/ownership fields, and the assigned worker as both actor and origin.
A relay names the exact current owner actor and requires both grants. Takeover
fences old owner relays while preserving current direct worker grants. Public
plan, event and coordination reads expose targets and proof, never either grant.

Submit a bounded finished checkpoint with the full artifact, successful evidence
and reassessment. A checkpoint leaves the attempt submitted; current owner
acceptance requires the same full artifact, current target/CAS and any required
latest original passing review. Resolve provisional reassessment holds explicitly.
Empty acceptance stop IDs retain physical resource obligations; release only
named resources supported by explicit stopped-use evidence. Retry the immutable
original command/key after uncertainty. Accepted worker grants permit exact
authenticated replay of committed commands, and refuse new activity.

Consumers need availability bound to the acceptance-returned result UUID, exact
artifact and final checked base. Invalidation holds producers and affected
consumers until their named remediation; required revalidation uses a current
post-invalidation review. Resource identity reconciliation, visible tools and
readiness do not grant execution or renew authorization. `plan-only` still
refuses assignment. The [worker gate](../operations.md#two-client-worker-checkpoint-gate)
lists the current 29-tool source catalog (six reads and 23 mutations), with no
disabled future kinds. At the historical Task 20 source-only checkpoint,
that catalog had synthetic SDK evidence; genuine installed Codex discovery and
worker acceptance were unverified.

Historical SDK actor labels do not prove an installed Codex or model ran.
See the [dated source and client coverage limits](../release-verification.md).
Record the actual surface/version, tool discovery, checkpoint sequence and
acceptance IDs from an authorized installed-client run.

## Historical client verification

See the [dated release verification](../release-verification.md).
These source and CLI observations apply only to their recorded versions and
flows. Verify the selected installed client surface before accepting setup.
