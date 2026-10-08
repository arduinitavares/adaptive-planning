# Copilot client setup

The service MCP endpoint is `http://127.0.0.1:8790/mcp`; the dashboard is
`http://127.0.0.1:8790/`. **Copilot CLI** uses the user file
`~/.copilot/mcp-config.json` with this schema:

```json
{
  "mcpServers": {
    "adaptivePlanning": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${ADAPTIVE_PLANNING_TOKEN}"
      },
      "tools": ["*"]
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
files or reports. `/mcp show adaptivePlanning` is a documented session diagnostic;
terminal configuration-management output alone is insufficient call evidence.
The inspected documentation does not establish a direct model-free MCP planning
mutation command. Verify an actual supported call surface before acceptance.

**VS Code is a separate client surface.** Its `mcp.json` uses `servers` and
password-prompt `inputs`; Copilot CLI does not consume this shape:

```json
{
  "servers": {
    "adaptivePlanning": {
      "type": "http",
      "url": "http://127.0.0.1:8790/mcp",
      "headers": {
        "Authorization": "Bearer ${input:adaptive-planning-token}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "adaptive-planning-token",
      "description": "Installation token",
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

Complete the [three-client planning gate](../operations.md#three-client-planning-gate)
with the actual selected surface, installed version, negotiated protocol, fresh
tools, plan ID, committed revision and dashboard observation. Record unavailable
call capability or allowance as a gap. An official SDK equivalent is separate
from installed Copilot acceptance.
