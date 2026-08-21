<p align="center">
  <img src="https://github.com/user-attachments/assets/bf7d955f-c79e-41c8-b517-e05c27bd2203" alt="MCP Harbour" height="256" />
</p>

<p align="center">
  Dock your servers once, control which agents can access which tools, and manage everything from a single place.<br/>
</p>

<p align="center">
  Built as an implementation of the <a href="https://gpars.io">GPARS</a> plane boundary.
</p>

<p align="center">
  <a href="https://github.com/mcpharbour/mcpharbour/releases/latest"><img src="https://img.shields.io/github/v/release/mcpharbour/mcpharbour?color=darkgreen&label=version" alt="Version" /></a>
  <a href="https://docs.mcpharbour.ai"><img src="https://img.shields.io/badge/docs-latest-indigo" alt="Docs" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

## Install

**Linux / macOS:**

```bash
curl -fsSL https://mcpharbour.ai/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://mcpharbour.ai/install.ps1 | iex
```

This downloads the binary, registers a per-user background daemon that runs as you, and starts it. No admin rights, no Python or package manager required.

Or download binaries directly from [GitHub Releases](https://github.com/mcpharbour/mcpharbour/releases).

## Quick Start

```bash
# 1. Dock an MCP server
harbour dock --name filesystem \
  --command "npx -y @modelcontextprotocol/server-filesystem /home/user/projects"

# 2. Create an identity
harbour identity create my-agent

# 3. Grant permissions
harbour permit allow my-agent filesystem --tool "*" --args "path=/home/user/projects/**"
```

Then configure your MCP client (Claude Code, VS Code, Cursor, OpenCode):

```json
{
  "mcpServers": {
    "harbour": {
      "url": "http://127.0.0.1:4767/mcp",
      "headers": {
        "Authorization": "Bearer harbour_sk_..."
      }
    }
  }
}
```

## How It Works

```
Agent → Streamable HTTP /mcp → Harbour Daemon → MCP Servers
              │                       │
          Bearer auth          identity verification
                               policy enforcement
                               AUTHORIZATION_DENIED / SERVER_UNAVAILABLE
```

- **Default deny** — no policy means no access
- **Identity from token** — agents cannot self-assert their identity
- **Per-agent policies** — whitelist of servers, tools, and argument constraints
- **Isolation by policy** — one shared daemon; each agent is confined by its policy, not by separate server processes
- **GPARS error codes** — `AUTHORIZATION_DENIED` (-31001) and `SERVER_UNAVAILABLE` (-31002)

## Documentation

Read the full docs at [docs.mcpharbour.ai](https://docs.mcpharbour.ai)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Author

[Ismael Kaissy](https://github.com/15m43lk4155y)

## License

This project is licensed under the [MIT License](./LICENSE).
