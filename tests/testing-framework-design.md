# Cross-Platform Testing Framework — Design

Status: approved direction — GitHub Actions (CI). Implementation starting at Phase 1.
Scope: Linux, macOS, Windows — including service lifecycle and self-update.

Decision log:
- Runner = GitHub Actions matrix. Local execution (Vagrant / `act` /
  self-hosted) is deferred; macOS cannot run on the current Linux host, so
  CI is the only path that covers all three platforms now.

## Why this shape

The whole point is to verify install, service management, and update on
every platform we ship. macOS and Windows cannot run in Docker, so the
backbone must be **GitHub Actions** (the only practical source of real
macOS and Windows machines). Docker is kept only as an optional Linux
**distro-breadth** supplement, not the core.

## Platforms

Runners (match what `release.yml` builds):

| Runner | Target | Notes |
|--------|--------|-------|
| `ubuntu-latest` | linux-x64 | systemd present, but `--user` services need CI setup (below) |
| `macos-latest` | darwin-arm64 | Apple Silicon; launchd; headless quirks (below) |
| `macos-13` (optional) | x64 via Rosetta | only if Intel-mac / Rosetta path matters |
| `windows-latest` | windows-x64 | SCM present; runner is admin, so service install works |

## Layers (run on every runner)

1. **Suite** — `pytest` on each OS, so platform branches (`_win_sc`,
   `systemctl`, `launchctl`) are at least imported and exercised.
2. **Frozen-binary smoke** — build the PyInstaller binary per OS, run the
   shared scenario against the *binary* (what users actually run).
3. **Install + service lifecycle** — run `install.sh` / `install.ps1`,
   then `harbour start` → `status` → scenario → `stop`, exercising the
   real platform service manager.
4. **Self-update** — `harbour update` end-to-end, including the Windows
   running-exe replacement that unit tests cannot reach.

## Shared scenario runner

One script, `tests/smoke/scenario.py`, is the single definition of "is
Harbour usable," runnable against either the source CLI or an
installed/frozen binary:

- dock a downstream MCP server, create an identity, grant a scoped policy,
- reach the daemon (started either via the service or `harbour serve`),
- connect with a **real MCP Streamable HTTP client** (`/mcp` needs session
  negotiation; raw curl is misleading),
- assert: allowed tool listed + callable, denied tool hidden + rejected,
  argument policy enforced, unauthenticated request → 401,
- always tear down.

Backed by an in-repo downstream MCP server (no external network). Written
to run identically on all three OSes (no bash-isms; pure Python + the MCP
SDK client).

## The two hard parts

### Service lifecycle in headless CI

Each OS has a catch; the framework must handle them explicitly rather than
pretend they "just work":

- **Linux** — `systemctl --user` needs a user systemd + D-Bus session that
  a non-login CI shell lacks. Mitigation: `loginctl enable-linger`, set
  `XDG_RUNTIME_DIR`, and/or wrap in `dbus-run-session`. If that proves too
  flaky, fall back to a system unit for the test, or verify unit-file
  install + `harbour serve` and mark the `--user` path lower-fidelity.
- **macOS** — user LaunchAgents in a headless runner may not fully start
  without an Aqua/login session; `launchctl bootstrap gui/$(id -u)` +
  `kickstart` is the modern path. If start is unreliable, assert the plist
  is installed and the daemon runs via `harbour serve`, and flag the gap.
- **Windows** — runner is admin, so `sc.exe` install/start/stop and
  `harbour-service.exe` should work directly. Highest-fidelity of the
  three.

Principle: prefer real service start/stop; where a runner genuinely can't,
degrade to "registration installed + daemon serves" and **say so in the
output** rather than silently skipping.

### Self-update without a chicken-and-egg

`harbour update` resolves a release from GitHub, downloads the install
script + `checksums.txt`, runs the installer, which downloads the archive.
Pre-release there is nothing to update from. Two complementary strategies:

- **High-fidelity, post-release (recommended):** on each OS, install the
  previous real release, then `harbour update` to the latest real release.
  Zero mocking — exercises the true network path, real checksum
  verification, and the running-binary replacement (this is where the
  Windows lock surfaces). Requires only that releases exist and ship
  `checksums.txt` (they now do). Runs on tag / on demand, not as a
  pre-merge gate.
- **Pre-merge, staged:** add a base-override hook so CI can point the
  updater + install scripts at a locally staged "release" (tarball +
  `checksums.txt` + install script). Needs a small code change
  (`MCP_HARBOUR_UPDATE_REPO` or a base-URL env in `updater.py`, mirrored
  in the install scripts). Lower fidelity but gateable on every PR.

Start with the post-release real-update test (no code change, highest
signal); add the staged hook only if we want update coverage on every PR.

## Required code changes (small, enabling)

1. **`install.sh` local-file mode** (approved) — `MCP_HARBOUR_LOCAL_ARCHIVE`
   to install a provided artifact without downloading; mirrors
   `install.ps1`'s `$HarbourBinaryPath`. Lets Layer 3 install freshly
   built binaries pre-release.
2. **Skip-service mode** — `MCP_HARBOUR_NO_SERVICE=1` for environments
   where service registration isn't wanted (e.g. the binary-smoke layer).
3. **(Optional) update base-override** — only if we want pre-merge staged
   update tests (see above).

All default off; production `curl | bash` behavior unchanged.

## Reuse the release build

`release.yml` already builds the PyInstaller binaries on all three OSes.
Factor the build into a reusable workflow / composite action so the test
workflow and the release workflow share one build definition instead of
drifting.

## Docker (optional supplement, not core)

Keep a small Docker tier for things Actions covers poorly: Linux **distro
breadth** (Debian/Fedora/Alpine), slim/declared-deps-only runs, and the
keyring + Node-missing edge cases. It supplements the Linux runner; it is
not the framework.

## Directory layout (proposed)

```
.github/workflows/
  test-matrix.yml          # the cross-platform framework
  build.yml                # reusable binary build (shared with release)
tests/
  smoke/
    scenario.py            # shared usability scenario (source or binary)
    downstream_server.py   # in-repo MCP server backing the scenario
  docker/                  # optional Linux distro-breadth supplement
    ...
```

## Phased plan

1. `scenario.py` + `downstream_server.py`; green against the source CLI
   locally.
2. `build.yml` reusable build; `test-matrix.yml` Layer 1 (pytest) +
   Layer 2 (binary smoke) on all three OSes.
3. `install.sh` local-file + skip-service modes.
4. Layer 3 install + service lifecycle per OS, with the headless-CI
   mitigations above.
5. Layer 4 self-update: post-release real-update test on each OS.
6. Optional: staged pre-merge update hook; Docker distro tier.

## Honest risks

- macOS/Linux **user**-service start in headless CI is the most likely
  source of flakiness; the plan has a documented degrade path rather than
  a silent skip.
- The post-release update test depends on real releases; it cannot gate a
  PR, only a tag. Pre-merge update coverage requires the optional override
  hook.
- macOS runner is arm64 only by default; Intel/Rosetta needs `macos-13`.

## Open questions

- Downstream MCP server: standalone script (lean) vs reuse
  `tests/conftest.py` fixtures? Standalone keeps the frozen-binary path
  free of test-only imports.
- Do we want the staged pre-merge update hook now, or rely on the
  post-release real-update test first?
- Is an Intel-mac (`macos-13`) target worth the extra runner, given
  releases only ship `darwin-arm64`?
