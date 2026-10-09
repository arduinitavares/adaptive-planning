# Codex client setup

The service MCP endpoint is `http://127.0.0.1:8790/mcp`; the dashboard is
`http://127.0.0.1:8790/`. Configure the installation bearer credential separately
from the URL. These are documentation examples, not an accepted client setup.
The configuration instructions below retain Task 10 preparation. Task 20's
source checkpoint does not refresh installed versions, activate configuration,
or establish CLI/Desktop runtime compatibility.

For **Codex CLI**, the supported environment-based registration is:

```sh
codex mcp add adaptivePlanning --url http://127.0.0.1:8790/mcp --bearer-token-env-var ADAPTIVE_PLANNING_TOKEN
```

Its user TOML entry is:

```toml
[mcp_servers.adaptivePlanning]
url = "http://127.0.0.1:8790/mcp"
bearer_token_env_var = "ADAPTIVE_PLANNING_TOKEN"
```

Provision `ADAPTIVE_PLANNING_TOKEN` privately into the environment of the actual
CLI process before launch. The command names the variable; it never needs the
token value in its arguments. Do not print the environment or credential.

**Codex Desktop is a distinct surface to verify.** A terminal export and successful
CLI test do not prove that an already running Desktop process inherited the
variable. Desktop needs verified GUI environment provisioning, or a private user
TOML entry using a literal Authorization header instead of `bearer_token_env_var`:

```toml
[mcp_servers.adaptivePlanning]
url = "http://127.0.0.1:8790/mcp"
http_headers = { Authorization = "Bearer SYNTHETIC_ONLY" }
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

Complete the [three-client planning gate](../operations.md#three-client-planning-gate)
using the actual chosen CLI or Desktop surface. Record the installed version,
observed MCP negotiation, fresh tool list, installation identity, plan ID,
committed revision, dashboard URL and creator-close/resume evidence. App-server
initialization is separate from downstream MCP negotiation; its status schema
does not provide a negotiated `protocolVersion` field. The public
[app-server API](https://learn.chatgpt.com/docs/app-server) advertises explicit MCP
calls, but schema availability alone does not prove installed runtime dispatch,
gating or inference-free behavior. Verify that route before using it for the gate.

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
- Do not spawn a fallback server or create a new canonical plan. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

## Worker checkpoints and the remaining client gate

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
refuses assignment. The [worker gate](../operations.md#three-client-worker-checkpoint-gate)
lists the current 29-tool source catalog (six reads and 23 mutations), with no
disabled future kinds. That catalog has synthetic SDK evidence; genuine installed
Codex discovery and worker acceptance remain unverified.

Task 20 exercises three synthetic official SDK clients per negotiated protocol:
auto `2026-07-28` and legacy `2025-11-25`. `codex-worker` is an actor label, not
a Codex run. The genuine Codex finished-worker and close/reopen/service-restart/
container-recreation rows are **NOT RUN**; actual client runs remain **0** and
full Task 20 acceptance remains **false**. Historical Task 10 planning probes
do not satisfy this gate. Record the actual chosen surface/version, negotiated
protocol, shared plan, visible checkpoint sequence and owner acceptance/review
IDs only from a later authorized installed-client run.
