# Changelog

All notable changes to MCP Harbour are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

The release workflow publishes the section for the tagged version as its GitHub
release notes, so update the entry for the version you're about to tag **before**
tagging it.

## [Unreleased]

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
