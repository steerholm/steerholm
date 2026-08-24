<p align="center">
  <img src="docs/logo/badge.svg" alt="Steerholm" width="480" />
</p>

<p align="center">
  An action plane for your agents, a control plane for you — every action an agent takes is checked against your policy before it reaches a server.<br/>
</p>

<p align="center">
  <a href="https://github.com/steerholm/steerholm/releases/latest"><img src="https://img.shields.io/github/v/release/steerholm/steerholm?color=darkgreen&label=version" alt="Version" /></a>
  <a href="https://docs.steerholm.ai"><img src="https://img.shields.io/badge/docs-latest-indigo" alt="Docs" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

## Install

**Linux / macOS:**

```bash
curl -fsSL https://steerholm.ai/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://steerholm.ai/install.ps1 | iex
```

This downloads the binary, registers a per-user background daemon that runs as you, and starts it. No admin rights, no Python or package manager required.

Or download binaries directly from [GitHub Releases](https://github.com/steerholm/steerholm/releases).

## Quick Start

```bash
# 1. Add an MCP server
holm add server git --command "uvx mcp-server-git"

# 2. Add an agent (prints its access key once)
holm add agent my-agent

# 3. Grant it scoped access
holm grant my-agent git --tool "git_log" --args "repo_path=/home/user/projects/**"
```

Then configure your MCP client (Claude Code, VS Code, Cursor, OpenCode) with the
agent's access key as the Bearer token:

```json
{
  "mcpServers": {
    "steerholm": {
      "type": "http",
      "url": "http://127.0.0.1:4767/mcp",
      "headers": {
        "Authorization": "Bearer steer_sk_..."
      }
    }
  }
}
```

## How It Works

```
Agent → HTTP /mcp → Steerholm daemon → MCP Servers
              │                       │
          Bearer auth          agent verification
                               policy enforcement
                               AUTHORIZATION_DENIED / SERVER_UNAVAILABLE
```

- **Default deny** — no grant means no access
- **Agent from token** — agents cannot self-assert who they are; the access key determines the agent
- **Per-agent policies** — allowlist of servers, tools, and argument constraints
- **Isolation by policy** — one shared daemon; each agent is confined by its policy, not by separate server processes
- **Structured error codes** — `AUTHORIZATION_DENIED` (-31001) and `SERVER_UNAVAILABLE` (-31002)

## Documentation

Read the full docs at [docs.steerholm.ai](https://docs.steerholm.ai)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Author

[Ismael Kaissy](https://github.com/15m43lk4155y)

## License

This project is licensed under the [MIT License](./LICENSE).
