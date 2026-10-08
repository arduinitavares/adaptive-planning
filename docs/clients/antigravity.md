# Antigravity client setup

The service MCP endpoint is `http://127.0.0.1:8790/mcp`; the dashboard is
`http://127.0.0.1:8790/`. Antigravity's documented remote entry uses `serverUrl`
and a literal header in its user MCP configuration:

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
