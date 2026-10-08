# Codex client setup

The service MCP endpoint is `http://127.0.0.1:8790/mcp`; the dashboard is
`http://127.0.0.1:8790/`. Configure the installation bearer credential separately
from the URL. These are documentation examples, not an accepted client setup.

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

**Codex Desktop is a distinct tested surface.** A terminal export and successful
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
