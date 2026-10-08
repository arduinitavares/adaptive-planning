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

Complete the [three-client planning gate](../operations.md#three-client-planning-gate)
through the actual installed Antigravity surface. It must freshly discover the
planning tools, read installation info and the canonical shared plan, commit a
planning-only revision with a distinct fact, and expose that event through the
same dashboard. Record the negotiated MCP protocol rather than assuming a
version. An SDK session does not establish Antigravity acceptance.

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
`plan-only` still refuses execution. See the [worker gate](../operations.md#three-client-worker-checkpoint-gate)
for the 24 tested tools, disabled future kinds and complete recording requirements.

The Task 20 source test uses three synthetic official SDK clients in each of auto
`2026-07-28` and legacy `2025-11-25`; `agy-worker` does not show AGY or a Google
model ran. Genuine Antigravity finished-worker and close/reopen/service-restart/
container-recreation checks remain **NOT RUN**, with **0** actual client runs
and full Task 20 acceptance **false**. Task 10 planning evidence and historical
version information do not fill these rows. Record actual surface/version,
negotiation, shared plan, checkpoint sequence and acceptance/review IDs only
from an authorized installed-client observation; preserve any allowance or call
capability gap without retrying exhausted providers.
