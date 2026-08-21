# Changelog

All notable changes to Steerholm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

The release workflow publishes the section for the tagged version as its GitHub
release notes, so update the entry for the version you're about to tag **before**
tagging it.

## [Unreleased]

### Changed
- **Renamed to Steerholm.** MCP Harbour is now **Steerholm**, and the CLI command
  is **`holm`** (was `harbour`). The Python package is `steerholm`, config lives
  in `~/.steerholm` (was `~/.mcp-harbour`), access keys are prefixed `steer_sk_`,
  the per-user daemon service is `steerholm`, and release assets are
  `steerholm-<platform>`. There is no migration — existing installs re-add their
  servers and agents under the new paths. (Historical entries below predate the
  rename and still refer to the old names.)
- **CLI overhaul to a verb-first, plain vocabulary.** Commands now read
  `holm <verb> <resource>`: `add server` / `add agent` (was `dock` /
  `identity create`), `remove server` / `remove agent` (was `undock` /
  `identity delete`), `list servers` / `list agents`, `show server` (was
  `inspect`) / `show agent` (was `permit show`), and `grant` (was `permit
  allow`). "Identity" is now **agent** and the credential is an **access key**.
  The daemon lifecycle commands (`start`/`stop`/`status`/`serve`) and
  `version`/`update` are unchanged.
- Config schema renamed to match: `config.json` stores agents under an `agents`
  key (was `identities`) and policy files use `agent_name` (was
  `identity_name`). Existing configs and policies load unchanged and are
  migrated to the new schema transparently on the next save.
- The error response `data` payload now carries an `error_type` field.

### Added
- `holm revoke <agent> <server> [--tool PATTERN]` removes a grant — one tool
  with `--tool`, or all access to the server without it. (Previously access
  could only be added, never removed.)
- `holm rotate agent <name>` issues a new access key while keeping the
  agent's grants; the previous key stops working immediately.
- `holm --version` flag, alongside the existing `holm version` command.
- `holm show server` now also lists which agents have been granted access to
  the server.

## [0.1.3] - 2026-08-17

### Added
- `harbour list` now shows each server's live **status**, **uptime**, and **tool
  count**; `harbour inspect` shows status, uptime, and the **tools the server
  provides** (name + description). Live data is read from the running daemon over
  the control plane; when the daemon is down the CLI says so.
- The CLI prints a hint when a **newer release is available**. The check runs at
  most once a day (cached), never blocks or fails the command, and can be turned
  off with `MCP_HARBOUR_NO_UPDATE_CHECK=1`.

### Fixed
- An argument policy no longer rejects tools that don't take the constrained
  argument. Granting `--tool "*" --args "path=..."` used to deny every tool
  without a `path` argument ("Missing required argument 'path'"); an argument
  policy now applies only when the call actually provides that argument.
- Windows install/update verified release checksums incorrectly: the downloaded
  `checksums.txt` was read as raw bytes, so every asset lookup reported "no entry"
  and aborted. It now decodes the response to text before parsing.
- Docking or undocking a server while the daemon is running now takes effect
  immediately, instead of requiring a daemon restart before the server's tools
  appear. The daemon owns server lifecycle: `harbour dock`/`undock` notify it
  over a loopback control endpoint, and it reconciles the running servers against
  the docked config (start / stop / restart). A periodic supervisor also retries
  failed starts.

## [0.1.2] - 2026-08-15

### Added
- `harbour update` and `harbour version` — in-place self-update from the latest
  GitHub release, with SHA-256 checksum verification of downloaded assets.

### Changed
- Agents now connect over **Streamable HTTP** to the daemon's `/mcp` endpoint
  with a Bearer token.
- The daemon runs as the invoking user on every platform — `systemd --user`
  (Linux), a launchd agent (macOS), and a logon Scheduled Task (Windows) —
  instead of a privileged system service. No admin rights required; it starts at
  login and restarts on failure.

### Removed
- The standalone bridge. Point MCP clients at `http://127.0.0.1:4767/mcp` with an
  `Authorization: Bearer <api-key>` header instead.

## [0.1.1] - 2026-05-10

### Fixed
- Documentation corrections.

## [0.1.0] - 2026-04-10

### Added
- Initial release: dock MCP servers behind a single endpoint, per-agent
  identities and API keys, and default-deny per-agent policies with tool and
  argument-level control.
