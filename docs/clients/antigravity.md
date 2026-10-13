# Antigravity client setup

The service MCP endpoint is `http://127.0.0.1:8790/mcp`; the dashboard is
`http://127.0.0.1:8790/`. Antigravity's documented remote entry uses `serverUrl`
and a literal header in its user MCP configuration:

These setup instructions retain Task 10 preparation; Task 20 source verification
does not refresh installed versions, activate configuration or prove the actual
Antigravity call surface. The example below remains synthetic.

```json
{
  "mcpServers": {
    "adaptivePlanning": {
      "serverUrl": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer SYNTHETIC_ONLY"
      }
    }
  }
}
```

This is a synthetic documentation example. Never install `SYNTHETIC_ONLY` in real
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
- Do not spawn a fallback server or create a new canonical plan. Report the last published revision as **LAST KNOWN**, offline/stale while the service is unavailable.
- On recovery, reread service identity, events, ownership, and coordination before publishing retained evidence. Retain the original immutable command body and request key and retry that exact saved command without rebuilding metadata or payload. Reconcile epoch, generation, ownership, attempt version, and holds; fenced or stale commands need explicit owner recovery. Reread after publication or replay before dispatching work.

## Worker checkpoints and the remaining client gate

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
synthetic SDK evidence; genuine installed Antigravity discovery and acceptance
remain unverified.

The Task 20 source test uses three synthetic official SDK clients in each of auto
`2026-07-28` and legacy `2025-11-25`; `agy-worker` does not show AGY or a Google
model ran. Genuine Antigravity finished-worker and close/reopen/service-restart/
container-recreation checks remain **NOT RUN**, with **0** actual client runs
and full Task 20 acceptance **false**. Task 10 planning evidence and historical
version information do not fill these rows. Record actual surface/version,
negotiation, shared plan, checkpoint sequence and acceptance/review IDs only
from an authorized installed-client observation; preserve any allowance or call
capability gap without retrying exhausted providers.


## Global entry prepared, 2026-10-13

The private global `adaptivePlanning` entry now points to the running local service, with the five unrelated server entries preserved and the credential kept in a mode-0600 file under a mode-0700 parent. AGY CLI 1.3.4 successfully completed ordinary read-only Gemini 4 Argon High access qualification and independent code review through Orca. These were source/evidence reviews, not MCP planning calls. Fresh genuine AGY checkpoint/resume acceptance remains pending explicit approval of the persistent `mcp(adaptivePlanning/*)` rule; AGY permission settings are unchanged. See the [dated local activation evidence](../operations.md#local-activation-evidence-2026-10-13).

## Native CLI checkpoint and continuation, 2026-10-13

The later [native client acceptance record](../operations.md#native-client-acceptance-completed-2026-10-13) supersedes the pending rule/checkpoint labels above. The explicitly approved named-server rule is now installed. Two fresh AGY 1.3.4 Flash High conversations used the permanent native MCP entry: the first accepted A and exited; the second read its saved checkpoint, acquired a fresh owner grant, bound the exact accepted observation and accepted dependent B. The labelled acceptance plan is complete at revision 10. The coordinator checked the actual native calls, public events, original-plan preservation and settled processes/terminals.

This proves the recorded CLI checkpoint/continuation flow. It does not claim a fresh 29-operation AGY exercise, a negotiated protocol value that native output did not expose, or IDE/Desktop and OS sleep/reboot behavior. Prior restart/container-recreation observations retain their recorded client/image boundaries. The private installation bearer and owner/attempt grants remain outside public receipts.
