# MCP Harbour — Complete Test Plan

This document is the authoritative catalogue of everything we test (or intend
to test) for MCP Harbour, on every platform, from a single unit assertion up
to an end user installing a release, the service starting, and real MCP
traffic flowing through the daemon.

Companion document: `tests/testing-framework-design.md` (architecture and the
CI-vs-local decision). This file is the *what* and *status*; that file is the
*how* and *why*.

## Status legend

- **DONE** — implemented and running (locally and/or in CI as noted).
- **PARTIAL** — exists but not wired everywhere it should be, or covers only
  part of the surface.
- **GAP** — required for the "real end-user" goal but not built yet.

## Platforms

Every layer is intended to run on all three release targets:

| Platform | Service manager | Notes |
|----------|-----------------|-------|
| Linux (x64) | systemd `--user` | headless CI needs linger / D-Bus; keyring needs a backend |
| macOS (arm64) | launchd LaunchAgent | headless `launchctl` is fiddly |
| Windows (x64) | Service Control Manager | runner is admin; running-exe lock matters for update |

---

## Coverage at a glance

Status reflects the framework after closing the first round of gaps.
"CI-only" = built and lint-clean but only executable on real runners (cannot
be validated on this Linux host). "validated" = exercised locally.

| Layer | What it proves | Linux | macOS | Windows |
|-------|----------------|-------|-------|---------|
| L0 Static lint | workflows are valid | DONE | DONE | DONE |
| L1 Unit | logic units are correct | DONE | DONE | DONE |
| L2 Integration | gateway + auth wiring | DONE | DONE | DONE |
| L3 E2E (protocol) | real daemon + real MCP client | DONE | n/a¹ | n/a¹ |
| L4 Binary build | a shippable binary builds | DONE | CI-only | CI-only |
| L5 Binary smoke (foreground) | the frozen binary works | DONE | CI-only | CI-only |
| L6 Install scripts | install.sh / install.ps1 run | DONE² | CI-only | CI-only |
| L7 Service lifecycle | start/stop/status via OS service | DISPATCH³ | DISPATCH³ | DISPATCH³ |
| L8 End-user usage (installed, foreground) | real user path | DONE² | CI-only | CI-only |
| L8s End-user usage (installed + service) | real service path | DISPATCH³ | DISPATCH³ | DISPATCH³ |
| L9 Full CLI surface | every command behaves | DONE⁴ | CI-only | CI-only |
| L10 Self-update | `harbour update` really updates | DISPATCH³ | DISPATCH³ | DISPATCH³ |
| L11 Uninstall | clean removal | DONE² | CI-only | CI-only |

¹ L3 runs only on the ubuntu runner (needs Node/npx); the hermetic scenario
covers macOS/Windows protocol behavior.
² Validated locally on this Linux host (install script + scenario + uninstall).
³ Built and lint-clean, but `workflow_dispatch`-gated and **not yet run** — the
headless service path and real-update path can only be validated on real
runners and need first-run iteration. Not a PR gate yet.
⁴ list/inspect/identity-list/permit-show are asserted; `undock` is not yet.

The honest summary: **L0–L6, L8 (foreground), L9, L11 are real and (on Linux)
locally validated. The remaining true-service paths (L7, L8s, L10) are written,
lint-clean, and wired as manual `workflow_dispatch` jobs — they still need a
first real-runner pass before we trust them.**

---

## L0 — Static workflow lint

Purpose: catch workflow mistakes before any runner spins up.

- **actionlint** over `.github/workflows/*.yml` — YAML, `${{ }}` expressions,
  `uses:`/`needs:` graph, shellcheck of every `run:` block. Validates all
  jobs including the macOS/Windows definitions.

Status: PARTIAL — run manually (clean), not yet a CI job. *To close:* add an
`actionlint` step so the workflows lint themselves on every change.

---

## L1 — Unit tests (`tests/unit/`)

Purpose: prove each logic unit in isolation. Fast, no network, no daemon.
Runs on all three OSes in the `pytest` matrix job. Status: **DONE**.

- **test_models.py** — Pydantic models. Server stdio/http defaults and
  validation, Identity required fields, ArgumentPolicy match-type validation
  (glob/regex only; `exact`/`fuzzy` rejected), ToolPermission with/without
  arg policies, Config shape, and JSON round-trip for Server and AgentPolicy.
- **test_config.py** — `ConfigManager`. Add/list/remove/get servers (stdio +
  http, reject both/neither, reject duplicates), identity add/get/remove with
  policy cascade-delete, policy grant (glob + `re:` regex, additive across
  tools and servers, invalid `arg=pattern` format rejected, grant to unknown
  identity rejected), on-disk persistence across manager instances, and
  platform config-dir resolution (`.mcp-harbour` on unix, `%APPDATA%` on
  win32).
- **test_permissions.py** — `PermissionEngine`, the heart of enforcement.
  Server-level allow/deny, tool-level exact/glob/wildcard, argument policies
  (glob path allow/deny, missing/!absent required arg denied, regex
  allow/deny, literal-glob deny), default-deny on empty policy, first-match
  semantics, regex anchored-at-start behavior, and that denials raise an
  `McpError` carrying the GPARS `AUTHORIZATION_DENIED` code, data, and
  message.
- **test_identity.py** — token → identity resolution. Correct identity for a
  valid token, `None` for unknown/partial tokens, `None` when no identities
  exist, and graceful `None` when the keyring backend throws.
- **test_errors.py** — GPARS error constructors produce the correct MCP error
  codes/messages/data for `AUTHORIZATION_DENIED` and `SERVER_UNAVAILABLE`.
- **test_process_manager.py** — command parsing (`shlex.split` of stdio
  commands incl. quoted paths, uvx/npx forms), `HarbourDaemon` shared-process
  bookkeeping, and `ServerHealth` tracking (healthy on successful start,
  failed + error recorded when a docked server fails to start).
- **test_updater.py** — self-update logic. Tag normalization, version
  comparison, platform asset selection (incl. Darwin x86_64 → arm64,
  unsupported platform error), installer-asset selection, checksum parsing
  (two formats) and verify success/failure, release-info from cached JSON,
  installer delegation (runs installer when an update exists, not on
  `--check`), version threaded as `MCP_HARBOUR_VERSION`, and subprocess
  failure surfaced as `UpdateError`.
- **test_cli.py** — `harbour version` prints the version; `harbour update`
  `--check` reports available/up-to-date, prompts then installs (with `--yes`
  to skip), and surfaces updater/installer errors as exit code 1.

---

## L2 — Integration tests (`tests/integration/`)

Purpose: prove the gateway, policy enforcement, and HTTP auth wiring work
together, using in-process gateways and mock/in-process downstreams. Runs on
all three OSes in the `pytest` job. Status: **DONE**.

- **test_session.py** — the shared gateway. Stdio and HTTP servers start as
  shared processes; tool discovery is policy-filtered (exact + glob); default
  deny when no policy; correct routing to the owning server; argument
  policies allow/deny on real calls; denied/unknown tools and unavailable
  downstreams return the right errors; plus reusable HTTP-downstream fixtures
  (an in-process Streamable HTTP MCP server) asserting visible-tool filtering,
  allowed call routing, denied-tool non-forwarding, and argument-policy
  rejection.
- **test_handshake.py** — Streamable HTTP auth/session. Missing/malformed/
  invalid `Authorization` → 401 (+ `WWW-Authenticate: Bearer`); a valid token
  initializes a session; a session id cannot switch identity (→ 401); an
  unknown session id → 404; `DELETE` of a session does not stop shared docked
  processes; one shared `Server("mcp-harbour")` backs all sessions; and
  `serve` exits(1) on a port already in use.

---

## L3 — End-to-end protocol tests (`tests/e2e/`)

Purpose: a *real* daemon over real Streamable HTTP with a real downstream MCP
server and the real MCP client library.

- **test_e2e.py** — starts `HarbourGateway.serve` in-process on an ephemeral
  port, connects with the MCP `streamable_http_client`, and drives the full
  protocol against `@modelcontextprotocol/server-everything` (requires Node /
  `npx`): valid/invalid token, initialize capabilities, list-tools for
  full/restricted/no-policy identities, tool calls, argument-policy denial,
  and multi-session identity isolation through one endpoint.

Status: **PARTIAL** — the test exists and is strong, but (a) it is **not run
in the CI matrix**, and (b) it depends on `npx`/Node. The hermetic
`tests/smoke/scenario.py` covers the same protocol surface without Node and
*is* run in CI, but the npx-backed e2e is currently unwired. *To close:*
either run `tests/e2e` in the matrix (Node is present on GitHub runners) or
formally designate the smoke scenario as the CI e2e and keep test_e2e.py as a
local/manual check.

---

## L4 — Binary build (`.github/workflows/build.yml`)

Purpose: prove a shippable artifact actually builds on each OS.

- Reusable workflow: PyInstaller builds `harbour` (and `harbour-service` on
  Windows), packages with the install/uninstall scripts into the
  platform archive, uploads it as an artifact. Shared with `release.yml` so
  packaging is defined once.

Status: **DONE** (called by both `release.yml` and `test-matrix.yml`).
*Validated locally:* the Linux PyInstaller binary builds and `harbour version`
runs, confirming no missing hidden imports.

---

## L5 — Frozen-binary smoke, foreground (`binary-smoke` job)

Purpose: prove the *frozen binary* (not the source) works as an end-user MCP
endpoint — without yet involving the installer or service manager.

- **scenario.py** (`tests/smoke/`) drives the external binary:
  `dock` a hermetic downstream → `identity create` → `permit allow` (a tool
  and an argument policy) → `serve` on an ephemeral port → connect with a real
  MCP client and assert: unauthenticated → 401, initialize → `mcp-harbour`,
  policy-filtered discovery, allowed call succeeds, denied call rejected,
  argument policy allows valid / rejects invalid.
- **downstream_server.py** — a self-contained stdio MCP server (`echo`,
  `secret`, `add`) so the scenario needs no Node and no network.

Status: **DONE** — validated locally against both the source CLI and the
frozen Linux binary. In CI it runs on all three OSes, but note: it uses
`harbour serve` (a foreground daemon the scenario starts itself), **not** the
installed/service-managed daemon. That distinction is exactly what L6–L8 add.

---

## L6 — Install-script execution (GAP)

Purpose: prove a real install works — the same path a user runs.

Planned scenarios, per OS:

- **Linux / macOS (`install.sh`)** — run the script in local-file mode against
  the freshly built artifact; assert the binary lands on PATH, checksum
  verification runs (against a staged `checksums.txt`), and — unless
  skip-service is set — the service unit/agent is registered.
- **Windows (`install.ps1`)** — same, installing `harbour.exe` and
  `harbour-service.exe` to `%LOCALAPPDATA%`, registering the Windows service.

Status: **GAP**. Requires two approved-but-unbuilt enabling changes:
`install.sh` **local-file mode** (`MCP_HARBOUR_LOCAL_ARCHIVE`) and
**skip-service mode** (`MCP_HARBOUR_NO_SERVICE`). `install.ps1` already has a
local mode (`$HarbourBinaryPath`).

---

## L7 — Service lifecycle (GAP)

Purpose: prove the daemon runs as a managed OS service, the way it does for a
real user after install.

Planned scenario, per OS, after L6 install:

- `harbour status` → running
- `harbour stop` → `status` shows stopped
- `harbour start` → `status` shows running
- exercised through the real service manager: systemd `--user` (Linux),
  launchd LaunchAgent (macOS), SCM via `harbour-service.exe` (Windows).

Status: **GAP**. Headless-CI caveats (documented in the design doc): Linux
needs linger/`XDG_RUNTIME_DIR`/`dbus-run-session`; macOS user agents may not
fully start headless; Windows is the most reliable. Where a runner genuinely
cannot start a user service, the test must degrade to "unit/agent registered +
daemon reachable via serve" and say so — never a silent skip.

---

## L8 — End-user usage against the installed + service-managed daemon (GAP)

Purpose: the real acceptance test — behave exactly like an end user. After
installing via the script and starting the service, connect to the
service-managed daemon (default `127.0.0.1:4767`) and run the full usability
scenario.

Planned scenario, per OS:

- Do **not** start `harbour serve`; the service is already running.
- Configure servers/identities/policies via the installed `harbour` CLI.
- Connect as an MCP client to the running service and assert the same policy
  surface as L5 (auth, discovery filtering, allowed/denied calls, argument
  policies), plus that config changes are picked up per the daemon's reload
  behavior.

Status: **GAP**. Requires a `scenario.py` enhancement: an **attach mode**
(`--attach host:port`) that connects to an already-running daemon instead of
starting its own `serve`. This is the single most important missing piece for
"validates all functionality like an end user would."

---

## L9 — Full CLI surface (PARTIAL)

Purpose: prove every command behaves, not just the policy-critical ones.

Covered today (via L1/L2/L5): `dock` (stdio+http), `identity create`,
`permit allow`, `serve`, `version`, `update --check`. Their underlying logic
is unit/integration tested.

Not yet asserted end-to-end against the installed binary: `list`, `inspect`,
`undock`, `identity list`, `identity delete`, `permit show`. *To close:*
extend the scenario (or add a CLI-surface scenario) to run each command
against the installed binary and assert on output/exit codes.

Status: **PARTIAL**.

---

## L10 — Self-update (GAP)

Purpose: prove `harbour update` really replaces a running install, on each OS,
including the Windows running-exe lock that unit tests cannot reach.

Planned scenarios:

- `harbour update --check` reports correctly on each OS.
- **High-fidelity (recommended):** install the previous real release, run
  `harbour update` to the latest real release, assert version bump and that
  the binary still works. Zero mocking; exercises real checksum verification
  and binary replacement. Runs on tag / on demand (needs real releases).
- **Pre-merge (optional):** stage a local "release" and point the updater at
  it via a base-override hook; gateable per-PR.

Status: **GAP** for the integration/e2e level (updater *logic* is unit tested
in L1).

---

## L11 — Uninstall (GAP)

Purpose: prove clean removal — also part of the end-user lifecycle.

Planned scenario, per OS: run `uninstall.sh` / `uninstall.ps1`; assert the
binary, the service registration, and (as designed) the relevant state are
removed and the service no longer responds.

Status: **GAP**.

---

## Reporting — Allure 3

Results are visualized with Allure 3. `allure-pytest` (a dev dependency) makes
the unit/integration/e2e suites emit Allure results via `--alluredir`, and the
smoke scenario emits its own Allure result file (each check becomes a step) when
run with `--alluredir`. In CI, every result-producing job uploads its
`allure-results-*` artifact; the `report` job in `test-matrix.yml` aggregates
them and runs `allure generate` (config in `allurerc.mjs`).

The matrix runs on `pull_request` (validation only — no publish) and on
`workflow_dispatch`. Each **dispatched** run's report is published to
**`runs/<run-number>/` on the `gh-pages` branch** (prior runs are kept via
`keep_files`). A **landing page**
at the site root lists every run in a table — Run, Date (UTC), Ref, Commit,
Trigger, Passed, Failed, Skipped, Report — built by
`tests/ci/build_report_index.py` from a `runs.json` manifest carried on the
branch. Per-run trend charts come from an `actions/cache` of `history.jsonl`.

Setup note: **Pages must serve the `gh-pages` branch** (Settings → Pages →
Source: Deploy from a branch → `gh-pages` / root). The `report` job pushes with
the built-in `GITHUB_TOKEN` (`contents: write`).

Locally: `make allure` (suite + scenario → report) or `make allure-serve` (live
server). Needs the Allure 3 CLI: `npm install -g allure`.

## Closed in this round

- **`scenario.py` attach mode** (`configure` / `check` subcommands) — *done,
  validated*. Enables L8 against any running daemon.
- **`install.sh` local-file (`MCP_HARBOUR_LOCAL_ARCHIVE`) + skip-service
  (`MCP_HARBOUR_NO_SERVICE`) modes** — *done, validated*. Mirrored in
  `install.ps1`.
- **Keyring bundled into the frozen binary** (`--collect-all keyrings.alt`) so
  headless `identity create` works with the file backend — *done, validated*
  on Linux. Removes the gnome-keyring/dbus fragility.
- **L0 actionlint**, **L3 npx e2e (ubuntu)**, **L5 binary smoke**, **L6/L8
  install-use**, **L11 uninstall** — wired into `test-matrix.yml` /
  `install-matrix.yml` and lint-clean.
- **L9 CLI assertions** — `list` / `inspect` / `identity list` / `permit show`
  asserted in the scenario `configure` step.

## Still open (needs a first real-runner pass)

In priority order:

1. **L7 service lifecycle / L8s service usage** — `service-lifecycle` job exists
   (`workflow_dispatch`), but the headless service start (systemd `--user`
   env+linger on Linux, launchd headless on macOS) is unproven. Run it, watch,
   iterate; only then move it onto the PR gate.
2. **L10 self-update** — `self-update` job exists (`workflow_dispatch`); needs a
   published release to update *from* and *to*, so it runs post-release.
3. **L9 `undock`** — add an assertion for the one remaining uncovered command.
4. **release.yml reusable-build refactor** — verify via a `workflow_dispatch`
   build before the next real tagged release.
5. Promote the dispatch-gated jobs to PR gates once they pass reliably.

Until 1–2 have had a green real-runner run, CI proves the code, the binary, the
install scripts (foreground), and uninstall — but the **service-managed daemon
and real self-update across platforms remain asserted-but-unproven**.
