# Antigravity client setup

Antigravity's documented remote entries use `serverUrl` and literal headers
in its private user MCP configuration. CLI and IDE are separate test surfaces.

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

```json
{
  "mcpServers": {
    "adaptivePlanningLocal": {
      "serverUrl": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer SYNTHETIC_ONLY"
      }
    },
    "adaptivePlanningRemote": {
      "serverUrl": "http://192.168.1.10:8790/mcp",
      "headers": {
        "Authorization": "Bearer SYNTHETIC_REMOTE_ONLY"
      }
    }
  }
}
```

This is a synthetic documentation example. Never install synthetic placeholders in real
client configuration. The [official MCP documentation](https://antigravity.google/docs/mcp?tab=cli)
does not give a remote-header environment interpolation guarantee. Do not assume
that `${ADAPTIVE_PLANNING_TOKEN}` expands, or infer that interpolation is impossible
from that silence. The documented `env` mechanism for stdio processes is a separate
configuration shape.

With execution authorization, provision the literal installation Authorization
header by a private raw edit of the selected user MCP configuration. Preserve
unrelated entries, use a private parent directory and file mode `0600`, and keep
the value out of CLI arguments, repository files, logs and reports. Do not use a
real-token `agy mcp add` command. Record only the actual surface, config schema and
private provisioning method.

The documented `/mcp` manager shows connection states and diagnostic information.
Neither a connected indicator nor a configuration entry proves a tool call. The
inspected official [CLI reference](https://antigravity.google/docs/cli/reference)
does not establish a direct model-free MCP planning mutation command; that route
remains unverified. Record a capability or allowance gap honestly if it prevents
the actual installed client from completing the gate.

Complete the [two-client planning gate](../operations.md#two-client-planning-gate)
through the actual installed Antigravity surface. It must freshly discover the
planning tools, read installation info and the canonical shared plan, commit a
planning-only revision with a distinct fact, and expose that event through the
same dashboard. Record the negotiated MCP protocol rather than assuming a
version. An SDK session does not establish Antigravity acceptance.

## Optional remote LAN connection

After the operator enables the [LAN overlay](../operations.md#optional-trusted-lan-access),
obtain the instance's exact endpoint. The stock overlay uses port 8790.
Set `serverUrl` in the selected remote entry to that base URL followed by `/mcp`.
Keep a separate local entry intact. The server's `.env` does not populate client
configuration. Do not assume interpolation in a static JSON `serverUrl` or
the literal `headers` value.

Provision the remote instance's bearer privately in its selected header.
Preserve unrelated entries and settings. Keep the configuration private using
the host's access controls. Changing an endpoint does not move existing plans.
Apply the selection and identity rules above before plan mutations.

Freshly discover tools and call `read_service_info` through that entry.
Use its advertised dashboard and plan URLs. Plain HTTP is a trusted-LAN mode.
Dashboard reads are public on that network; MCP and mutations require bearer
authentication. These examples do not prove installed CLI or IDE acceptance.

## Instruction contract, domain grants, and session lifecycle

Antigravity executes under the generic framework-neutral instruction contract:
`publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work`.
Workflow document references and phase metadata are optional; no specific Superpowers sequence is required.

### Domain grants and session persistence

Persist domain grants (orchestrator tokens, attempt tokens) only in explicitly
private client/session working records outside service public views. Do not assume
automatic client-session persistence across Antigravity sessions or restarts.
If a new session begins without a retained valid orchestrator grant, perform an
explicit `takeover` specifying current generation and expected ownership version.
Takeover re-establishes coordination authority but is not scope renewal; execution
boundaries remain strictly governed by user instructions.

### Resume and stale recovery sequence

Ordinary resume reads events, unfinished ownership, holds, and reviews, and
obtains an authorized owner `sweep_stale` before dispatch or recovery. A direct
worker asks the owner to sweep. If its attempt is overdue, reread the latest version
after the sweep, then the owner confirms ownership or an unfenced current worker
publishes a recovery checkpoint.

### Worktree isolation and resource declarations

Isolate each assigned worker within its own worktree or directory and declare physical
resource claims (repository, workspace, external) to avoid write conflicts. Workers
must provide observable activity at least every five minutes; attempts exceeding
fifteen minutes of inactivity reach the default stale threshold. Owner sweep and
explicit recovery are required. The service coordinates declared resources and
capability fences; workers and operators enforce actual filesystem isolation and stops.

### Missing-MCP worker relay and safe pause

When a dispatched worker lacks direct MCP tools, an authorized relay submits
`start_step`, `report_activity`, and `record_checkpoint` using both orchestrator
and attempt grants. The relay preserves the worker's identity in `origin_worker`
and the relay's identity in `relay`, and never invents checks or heartbeat signals.
For review, `record_review` records the independent reviewer in `Review.reviewer`
and the submitting actor as orchestrator; the `Review` DTO has no relay field.
Safe pause relays truthfully record state without fabricating completion.

### Outage policy

If the MCP service or dashboard becomes unavailable during execution:
- An assigned bounded worker finishes verification locally and retains unpublished evidence in its private working record.
- New assignments and dependents wait.
- Do not spawn a fallback server or create a competing authority for this plan.
  Deliberately configured independent instances can serve other plans. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

## Worker checkpoints and client verification

Keep the installation bearer separate from the private owner and attempt grants.
Assignment returns the worker's attempt grant. A direct start/activity/checkpoint
uses only that grant and the exact current attempt/version/definition, with null
global revision/ownership fields and actor/origin equal to its assigned worker.
An owner relay must name the exact current owner actor and supply both grants;
old relays are fenced by takeover while current direct workers survive. Public
reads expose canonical targets and evidence, without grants.

A finished checkpoint includes the full artifact, evidence and reassessment.
It submits work for owner acceptance; provisional holds require explicit owner
classification, and required review must be the latest original passing verdict
for that exact artifact. The owner accepts with current CAS, exact attempt target
and named stopped resource IDs/reason. Empty stop IDs retain resource obligations;
identity reconciliation alone certifies no host stop. Keep the original immutable
command/key for retry: accepted grants support exact committed replay, while
new worker progress is refused.

Bind consumer availability to the actual accepted result UUID, full artifact and
final checked base before start. Invalidation preserves accepted identities and
holds each affected step until named remediation, including a post-invalidation
review where required. Scope is not renewed by discovery, readiness or holds;
`plan-only` still refuses execution. See the [worker gate](../operations.md#two-client-worker-checkpoint-gate)
for the current 29-tool source catalog (six reads and 23 mutations), with no
disabled future kinds, and complete recording requirements. The catalog has
synthetic SDK evidence; installed-client observations have the [dated coverage limits](../release-verification.md).

Historical SDK fixtures use synthetic actor labels. They do not establish
installed-client acceptance. See the [source and client coverage limits](../release-verification.md).
Verify the actual surface, installed version, tool discovery and checkpoint
flow without treating earlier evidence as a fresh installation check.

## Historical client verification

See the [dated release verification](../release-verification.md).
These source and CLI observations apply only to their recorded versions and
flows. Verify the selected installed client surface before accepting setup.
