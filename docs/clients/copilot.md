# Copilot client setup

Copilot CLI uses the private user file `~/.copilot/mcp-config.json`.
VS Code uses a separate schema and needs separate runtime verification.

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
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${ADAPTIVE_PLANNING_TOKEN}"
      },
      "tools": [
        "*"
      ]
    },
    "adaptivePlanningRemote": {
      "type": "http",
      "url": "http://192.168.1.10:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${ADAPTIVE_PLANNING_REMOTE_TOKEN}"
      },
      "tools": [
        "*"
      ]
    }
  }
}
```

The [official add-server guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
documents this user-file shape; the
[CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
documents environment expansion in remote headers. The installed CLI must still
prove it: privately provide an environment variable containing a synthetic
installation token to that exact process, then observe authenticated initialize,
fresh tools/list and a successful call. A literal `${ADAPTIVE_PLANNING_TOKEN}`
header or an absent value must fail authentication. Record the results without
logging either the expanded header, literal candidate or credential value. A
schema check or configured `tools: ["*"]` filter does not prove expansion or
remote discovery.

With execution authorization, preserve existing user entries and provision the
environment through a private local channel. Keep private configuration at mode
`0600` under a private parent directory. Never install synthetic example tokens
into a real user configuration or put token values in CLI arguments, repository
files or reports. `/mcp show adaptivePlanningLocal` is a documented session diagnostic;
terminal configuration-management output alone is insufficient call evidence.
The inspected documentation does not establish a direct model-free MCP planning
mutation command. Verify an actual supported call surface before acceptance.

**VS Code is a separate client surface.** Its `mcp.json` uses `servers` and
password-prompt `inputs`; Copilot CLI does not consume this shape:

```json
{
  "servers": {
    "adaptivePlanningLocal": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${input:adaptive-planning-token}"
      }
    },
    "adaptivePlanningRemote": {
      "type": "http",
      "url": "http://192.168.1.10:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${input:adaptive-planning-remote-token}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "adaptive-planning-token",
      "description": "Installation token",
      "password": true
    },
    {
      "type": "promptString",
      "id": "adaptive-planning-remote-token",
      "description": "Remote installation token",
      "password": true
    }
  ]
}
```

The [VS Code MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
documents this prompt mechanism. Name the actual extension-host surface tested;
servers needing interactive inputs are not forwarded to Agent Host by the
documented current behavior. A VS Code result does not prove Copilot CLI's
environment expansion.

For a future, separately requested Copilot validation, use the scenario in the
[two-client planning gate](../operations.md#two-client-planning-gate) with the selected surface, installed version, negotiated protocol, fresh
tools, plan ID, committed revision and dashboard observation. Record unavailable
call capability or allowance as a gap. An official SDK equivalent is separate
from installed Copilot acceptance.

## Instruction contract, domain grants, and session lifecycle

Copilot operates under the generic framework-neutral instruction contract:
`publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work`.
Workflow document references and phase metadata are optional; no specific Superpowers sequence is required.

### Domain grants and session persistence

Persist domain grants (orchestrator tokens, attempt tokens) only in explicitly
private client/session working records outside service public views. Do not assume
automatic client-session persistence across Copilot sessions or restarts. If a
new session starts without a retained valid orchestrator grant, perform an explicit
`takeover` with current generation and expected ownership version. Takeover re-establishes
coordination authority but is not scope renewal; execution boundaries remain strictly
governed by user instructions.

### Resume and stale recovery sequence

Ordinary resume reads events, unfinished ownership, holds, and reviews, and
obtains an authorized owner `sweep_stale` before dispatch or recovery. A direct
worker asks the owner to sweep. If its attempt is overdue, reread the latest version
after the sweep, then the owner confirms ownership or an unfenced current worker
publishes a recovery checkpoint.

### Worktree isolation and resource declarations

Isolate each assigned worker within its own worktree or directory and declare
physical resource claims (repository, workspace, external) to avoid concurrent
write conflicts. Workers must report observable activity at least every five
minutes; attempts exceeding fifteen minutes of inactivity reach the default stale
threshold. Owner sweep and explicit recovery are required. The service coordinates
declared resources and capability fences; workers and operators enforce actual
filesystem isolation and stops.

### Missing-MCP worker relay and safe pause

When a dispatched worker lacks direct MCP tools, an authorized relay submits
`start_step`, `report_activity`, and `record_checkpoint` using both orchestrator
and attempt grants. The relay faithfully preserves the worker's identity in
`origin_worker` and the relay's identity in `relay`, and never invents checks or
heartbeat signals. For review, `record_review` records the independent reviewer
in `Review.reviewer` and the submitting actor as orchestrator; the `Review` DTO
has no relay field. Safe pause relays record state truthfully without fabricating
progress.

### Outage policy

If the MCP service or dashboard becomes unavailable during execution:
- An assigned bounded worker finishes verification locally and retains unpublished evidence in its private working record.
- New assignments and dependents wait.
- Do not spawn a fallback server or create a competing authority for this plan.
  Deliberately configured independent instances can serve other plans. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

## Worker checkpoints and client verification

The installation bearer authorizes MCP transport access, while the private owner
and attempt grants authorize domain commands. Direct worker start/activity/
checkpoint calls use only the assignment's attempt token and exact current own
target/version/definition, null global revision/ownership fields, and assigned
actor/origin. Relays name the exact current owner actor and need both grants.
Takeover fences old owner relays without fencing current direct workers. Read
projections carry targets and proof; they never convey either private grant.

Publish a finished checkpoint with the complete artifact, evidence and
reassessment. It is submitted work until the current owner accepts with current
CAS, exact target and full artifact, after explicit reassessment resolution and
any required latest original passing review. Supply only supported stopped
resource IDs/reason; empty IDs retain physical obligations. Reconciliation of
resource identity is not stop confirmation. Preserve the original immutable
envelope/key for uncertain retries. Accepted grants allow exact authenticated
replay of committed worker commands, and refuse new terminal progress.

Consumer availability must bind the actual acceptance-returned result UUID,
exact artifact and final checked base. Invalidation holds the producer and each
affected consumer for explicit named remediation, with a post-invalidation
review when required. Tools, reads and readiness do not renew scope or grant
execution; `plan-only` still refuses assignment. The [worker gate](../operations.md#two-client-worker-checkpoint-gate)
records the current 29-tool source catalog (six reads and 23 mutations), with no
disabled future kinds, and proof fields. The catalog has synthetic SDK evidence;
genuine installed Copilot discovery and acceptance remain unverified.

Historical SDK fixtures use synthetic actor labels. They do not establish
installed-client acceptance. See the [source and client coverage limits](../release-verification.md).
Verify the actual surface, installed version, tool discovery and checkpoint
flow without treating earlier evidence as a fresh installation check.
