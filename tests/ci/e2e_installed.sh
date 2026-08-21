#!/usr/bin/env bash
# Stage 3 (Linux/macOS): simulate the real end-user flow against the built binary.
#   build check -> configure -> run install.sh WITH service -> verify the
#   service-managed daemon is answering -> live usage test -> uninstall + verify.
# Each phase is recorded into Allure (parentSuite=OS, suite=phase). The job fails
# if install, usage, or uninstall fails.
#
# Env: ARCHIVE (path to the release tarball), OSNAME (Linux|macOS), PLATFORM.
set -uo pipefail

AR="allure-results"
mkdir -p "$AR"

emit() { # name suite status
  python tests/smoke/scenario.py emit --alluredir "$AR" \
    --allure-os "$OSNAME" --allure-suite "$2" --allure-name "$1" --status "$3" || true
}

# ── Build: the freshly built binary runs ───────────────────────────
TMP="$(mktemp -d)"
tar -xzf "$ARCHIVE" -C "$TMP"
BIN_TMP="$TMP/harbour"
chmod +x "$BIN_TMP"
if "$BIN_TMP" version >/dev/null 2>&1; then
  emit "built binary runs ($PLATFORM)" Build passed
else
  emit "built binary runs ($PLATFORM)" Build failed
fi

# ── Configure before the daemon starts (the service reads this config) ──
TOKEN="$(python tests/smoke/scenario.py configure --harbour "$BIN_TMP" | sed -n 's/^TOKEN=//p')"

export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring
# The service-managed daemon must use the SAME (file) keyring backend as the
# configure step, or it can't verify the token (401). Service managers do not
# inherit this shell's env, so inject it into the platform manager. CI-headless
# only: real users share one in-session OS keyring across configure and daemon.
if [ "$OSNAME" = "Linux" ]; then
  # No login session on CI; linger gives this user a systemd manager so the real
  # --user unit runs (simulating a logged-in user).
  XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export XDG_RUNTIME_DIR
  loginctl enable-linger "$USER" || true
  systemctl --user set-environment PYTHON_KEYRING_BACKEND="$PYTHON_KEYRING_BACKEND" || true
elif [ "$OSNAME" = "macOS" ]; then
  # launchd agents don't inherit the step env; setenv before install loads it.
  launchctl setenv PYTHON_KEYRING_BACKEND "$PYTHON_KEYRING_BACKEND" || true
fi

# ── Install via the real script, WITH service registration ──────────
MCP_HARBOUR_LOCAL_ARCHIVE="$ARCHIVE" bash scripts/install.sh || true
BIN="$HOME/.local/bin/harbour"

# ── Verify the installed, service-managed daemon is answering as Harbour ──
up=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:4767/healthz 2>/dev/null | grep -q '"service":"mcp-harbour"'; then
    up=1; break
  fi
  sleep 1
done
if [ "$up" = 1 ]; then
  emit "service-managed daemon up ($PLATFORM)" Install passed
else
  emit "service-managed daemon up ($PLATFORM)" Install failed
fi

# ── Usage: live test against the running daemon ─────────────────────
usage_ok=0
if [ "$up" = 1 ]; then
  if python tests/smoke/scenario.py check --url http://127.0.0.1:4767/mcp --token "$TOKEN" \
      --alluredir "$AR" --allure-os "$OSNAME" --allure-suite Usage \
      --allure-name "live usage ($PLATFORM)"; then
    usage_ok=1
  fi
else
  emit "live usage skipped: daemon not up ($PLATFORM)" Usage failed
fi

# ── Uninstall + removal verification ────────────────────────────────
bash scripts/uninstall.sh || true
if [ ! -e "$BIN" ]; then
  emit "binary removed ($PLATFORM)" Uninstall passed
  removed=1
else
  emit "binary still present after uninstall ($PLATFORM)" Uninstall failed
  removed=0
fi

[ "$up" = 1 ] && [ "$usage_ok" = 1 ] && [ "$removed" = 1 ]
