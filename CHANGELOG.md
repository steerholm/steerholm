# Changelog

All notable changes to Steerholm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

The release workflow publishes the section for the tagged version as its GitHub
release notes, so update the entry for the version you're about to tag **before**
tagging it. At tag time, rename `[Unreleased]` to the version and date.

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
