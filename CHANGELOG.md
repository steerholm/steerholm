# Changelog

All notable changes to Steerholm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

The release workflow publishes the section for the tagged version as its GitHub
release notes, so update the entry for the version you're about to tag **before**
tagging it. At tag time, rename `[Unreleased]` to the version and date.

## [Unreleased]

### Added

- `--env KEY=VALUE` on `holm add server` — pass environment variables (config or
  secrets like a database connection string) to a stdio server. Repeatable;
  `holm show server` masks the values.

### Fixed

- `holm update` now replaces the running binary atomically (write + rename) and
  restarts the daemon so it picks up the new binary. It previously failed with
  "text file busy" on Linux, and on macOS the daemon could fail to come back.
- The daemon shuts down cleanly: each MCP server's client contexts are opened
  and closed on a single task, fixing an anyio "cancel scope in a different task"
  error that corrupted shutdown and could orphan a server subprocess.
- The daemon rebinds its port with `SO_REUSEADDR` (Unix) so a restart isn't
  blocked by the previous instance's `TIME_WAIT` sockets ("port already in use").
- A server that never completes its MCP handshake no longer hangs startup
  (120s connect timeout), and shutdown stops servers concurrently.

### Security

- Config files are now owner-only: `~/.steerholm` is `0700`, and `config.json`
  and the per-agent policy files are `0600`. `--env` secrets, policies, and
  grants are no longer readable by other users on the machine; existing installs
  are hardened on the next run. (No-op on Windows, where per-user `AppData`
  already isolates them.)

## [0.1.0] - 2026-08-24

### Added

- Initial release. Steerholm is a control plane for the MCP servers your agents
  use: register servers behind one governed endpoint
  (`http://127.0.0.1:4767/mcp`), register agents with their own access keys, and
  grant each agent least-privilege access with per-tool and per-argument policies
  (default-deny).
- Verb-first CLI: `holm add server|agent`, `holm remove`, `holm list`,
  `holm show`, `holm grant`, `holm revoke`, and `holm rotate agent`.
- Per-user daemon on every platform — `systemd --user` (Linux), a launchd agent
  (macOS), and a logon Scheduled Task (Windows). No admin rights; it starts at
  login and restarts on failure. Lifecycle: `holm start|stop|status|serve`.
- Agents connect over Streamable HTTP with a Bearer access key. Changes to
  servers and grants take effect immediately over a loopback control plane.
- In-place self-update from GitHub releases with SHA-256 checksum verification:
  `holm update`, `holm version`, and the `holm --version` flag.
- One-line install for Linux, macOS, and Windows.

Coming from MCP Harbour? See the migration guide (Guides → Migrating from MCP
Harbour) to move your servers and agents across.
