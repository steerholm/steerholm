# Changelog

All notable changes to Steerholm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

The release workflow publishes the section for the tagged version as its GitHub
release notes, so update the entry for the version you're about to tag **before**
tagging it. At tag time, rename `[Unreleased]` to the version and date.

## [0.1.2-rc.4] - 2026-10-19

Pre-release. Changes since 0.1.2-rc.3.

### Changed

- `holm add server`, `modify server`, `remove server` and `log set` return as
  soon as the daemon has the change, and the daemon applies it in the
  background. They print which servers are starting or stopping; `holm show
  server` shows when one is up, or why it failed. They waited while a new
  server started, and a first `npx` run could outlast that wait and report
  "Could not reach the daemon to apply the change: timed out" for a change that
  had been made.
- A tool call to a tool that no answering server provides, while a server the
  agent may use is down or failed, is reported as `SERVER_UNAVAILABLE` naming
  that server and logged as `error` — that server may be the one with the tool.
  It was reported as "not found on any docked server" and logged as a denial.

### Fixed

- Documentation: a refused or failed tool call comes back as a tool result with
  `isError: true` and the reason as its text, as it has since 0.1.0 — not as a
  JSON-RPC error with code `-31001`/`-31002`, which applies only to other
  requests. `holm grant` takes several argument policies by repeating `--args`,
  not by listing them after one. A crashed daemon is restarted on Linux and
  macOS; on Windows it stays down until the next sign-in or `holm start`.

### Security

- Glob argument policies now match as documented: `*` and `?` stay within one
  path level and `**` crosses levels. `*` used to cross levels, so `/srv/*`
  allowed `/srv/a/b`. **A grant that relied on that needs `**`** — this applies
  to URLs and free text too: `url=https://example.com/*` no longer matches
  deeper paths, and `message=*` no longer matches a value containing `/`.
- A value with a `..` path segment no longer matches a glob policy unless the
  pattern contains `..` itself. `/srv/../etc/passwd` used to satisfy both
  `/srv/*` and `/srv/**`.
- A negated character class (`[!a]`) no longer matches a path separator.

## [0.1.2-rc.3] - 2026-10-18

Pre-release. Changes since 0.1.2-rc.2.

### Changed

- The daemon answers as soon as it starts and brings its MCP servers up in the
  background, instead of starting every server before it opens its port. While a
  server comes up, `holm list servers` and `holm show server` show it as
  `starting`. An agent that connects right after the daemon starts may see a
  server's tools appear a moment later.

### Fixed

- The installer no longer reports "Daemon did not answer on 127.0.0.1:4767
  within 10s" when the daemon was only waiting for a slow server to start (an
  `npx` server downloading its package on a fresh machine).
- Windows: updating from 0.1.0, 0.1.1, 0.1.2-rc.1 or 0.1.2-rc.2 no longer ends
  with a `zlib.error` traceback and exit code 1; the installer now ends the
  previous version's updater as intended, with exit code 0.
- Windows: `holm stop` stops the daemon and the MCP servers it started. It
  reported "Daemon is still listening on 127.0.0.1:4767" and left them running.
- Stopping the daemon while servers are still starting no longer leaves those
  server processes running.

## [0.1.2-rc.2] - 2026-10-17

Pre-release. Changes since 0.1.2-rc.1.

### Fixed

- Windows: installing or updating no longer stops at the migration step with
  "something is listening on 127.0.0.1:4767" when nothing is.
- Linux: `holm status`, `stop`, `start` and `update` work on systems whose
  OpenSSL is newer than the one bundled with Steerholm (Ubuntu 26.04), and MCP
  servers no longer load Steerholm's bundled libraries.
- `holm update` no longer ends with a `zlib.error` traceback and exit code 1
  after a successful update. An update started from 0.1.0, 0.1.1 or 0.1.2-rc.1
  still runs that version's updater, so the installer ends it once the new
  version is in place: on Linux and macOS the shell reports "Terminated" (exit
  143) for that one update, after "Updated Steerholm to …".
- Windows: the installer registers the logon task over SSH, where it failed with
  "No mapping between account names and security IDs". The daemon still starts
  only in a signed-in desktop session (console, Remote Desktop or VNC).
- Windows: refusing stored data from another version now names the PowerShell
  installer instead of the bash one.
- Windows: a failed install run with `irm … | iex` no longer closes the
  PowerShell window, taking the error with it.
- `holm update` and the installers say what the release server answered when
  it refuses (for example a rate limit) or cannot be reached, instead of a
  traceback or a bare `curl` error.

### Security

- If `checksums.txt` cannot be downloaded, `holm update` and the installers stop
  instead of installing without verifying the download.

## [0.1.2-rc.1] - 2026-09-30

Pre-release. Release candidates may still change how stored data is migrated;
if you test one, you may have to start fresh.

### Added

- An audit log of every tool call an agent makes through Steerholm: which agent,
  server and tool, and whether policy allowed it, denied it, or the server
  failed. `holm log` reads it, with or without the daemon running (filter with
  `--agent`, `--server` and `--status`; `-n` for how many), and `holm log
  --follow` watches calls as they happen. History is rotated and retained: see
  the limits with `holm log config`, change them with `holm log set
  --max-files N --max-age-days N`.
- `holm modify server` changes a server in place (`--command`, `--url`, `--env`,
  `--unset`) and keeps its id and every agent's grants.
- Agents and servers have permanent ids, shown by `holm show`, kept across key
  rotation. `holm log --agent` accepts an id.
- The installer migrates stored data to the new layout, keeping a copy of the
  previous layout in `.pre-migration` for inspection.
- Pre-releases: `holm update` does not install them; `holm update --tag <tag>`
  does.

### Changed

- Agent key hashes are stored in `config.json` instead of the OS keyring, and the
  daemon's control token is a file in the config directory.
- Unknown keys in `config.json` are rejected instead of ignored.
- The documentation for the newest version is served at `/latest`.

### Fixed

- A `config.json` that cannot be loaded (a mistyped key, a truncated file) is
  refused with a message naming the problem. It used to be treated as empty, and
  the next change overwrote every server and agent in it.
- Stored data written by a different version is refused with a message, instead
  of being misread.
- `holm add agent` works where no OS keyring is available, such as headless
  Linux, where it failed with "No recommended backend was available".
- The installer keeps the daemon stopped for the whole install, so it cannot
  restart on the new binary before the stored data is ready.

### Security

- Grants are tied to permanent ids instead of names. A server or agent removed and
  added again under the same name starts with no grants; removing a server used
  to leave its grants in place for whatever took the name next.

## [0.1.1] - 2026-08-29

### Added

- `--env KEY=VALUE` on `holm add server` — pass environment variables (config or
  secrets like a database connection string) to a stdio server. Repeatable;
  variable names are validated (the portable Kubernetes/Docker rule) and
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
- `list` and `show` output now displays names, commands, and argument patterns
  literally instead of mangling or crashing when they contain `[` — for example a
  regex policy like `sql=re:^[a-z]+` in `holm show agent`.

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
