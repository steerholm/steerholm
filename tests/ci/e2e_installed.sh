#!/usr/bin/env bash
# Stage 3 (Linux/macOS): simulate the real end-user flow against the built binary.
#   build check -> configure -> run install.sh WITH service -> verify the
#   service-managed daemon is answering -> live usage test -> re-run install over
#   the running daemon (self-update regression) -> uninstall + verify.
# Each phase is recorded into Allure (parentSuite=OS, suite=phase). The job fails
# if install, usage, update, or uninstall fails.
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
BIN_TMP="$TMP/holm"
chmod +x "$BIN_TMP"
if "$BIN_TMP" version >/dev/null 2>&1; then
  emit "built binary runs ($PLATFORM)" Build passed
else
  emit "built binary runs ($PLATFORM)" Build failed
fi

# ── Configure before the daemon starts (the service reads this config) ──
TOKEN="$(python tests/smoke/scenario.py configure --holm "$BIN_TMP" | sed -n 's/^TOKEN=//p')"

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
STEERHOLM_LOCAL_ARCHIVE="$ARCHIVE" bash scripts/install.sh || true
BIN="$HOME/.local/bin/holm"

# ── Verify the installed, service-managed daemon is answering as Steerholm ──
up=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:4767/healthz 2>/dev/null | grep -q '"service":"steerholm"'; then
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

# ── Update: re-run install.sh over the RUNNING daemon. Regression guard for
#    the self-update path — the binary must be replaced atomically (an in-place
#    cp is "text file busy" on Linux / crashes the daemon on macOS), and the
#    service must restart onto the new binary. ────────────────────────
update_ok=0
if [ "$up" = 1 ]; then
  ino_before="$(stat -c %i "$BIN" 2>/dev/null || stat -f %i "$BIN" 2>/dev/null)"
  STEERHOLM_LOCAL_ARCHIVE="$ARCHIVE" bash scripts/install.sh
  install_rc=$?
  ino_after="$(stat -c %i "$BIN" 2>/dev/null || stat -f %i "$BIN" 2>/dev/null)"
  reup=0
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:4767/healthz 2>/dev/null | grep -q '"service":"steerholm"'; then
      reup=1; break
    fi
    sleep 1
  done
  if [ "$reup" != 1 ]; then
    echo "DIAG: daemon did not rebind 4767 within 60s after the update"
    if [ "$OSNAME" = "Linux" ]; then
      systemctl --user status steerholm --no-pager -l 2>&1 | tail -20 || true
      journalctl --user -u steerholm --no-pager -n 25 2>&1 | tail -25 || true
    else
      launchctl list 2>&1 | grep -i steerholm || true
      tail -n 25 "$HOME/.steerholm/daemon.log" 2>&1 || true
    fi
  fi
  # inode must change: proves an atomic rename, not an in-place cp (same inode).
  if [ "$install_rc" = 0 ] && [ -n "$ino_before" ] && [ "$ino_before" != "$ino_after" ] && [ "$reup" = 1 ]; then
    update_ok=1
    emit "update over running daemon ($PLATFORM)" Update passed
  else
    emit "update over running daemon failed (rc=$install_rc replaced=$([ "$ino_before" != "$ino_after" ] && echo y || echo n) reup=$reup) ($PLATFORM)" Update failed
  fi
else
  emit "update skipped: daemon not up ($PLATFORM)" Update failed
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

[ "$up" = 1 ] && [ "$usage_ok" = 1 ] && [ "$update_ok" = 1 ] && [ "$removed" = 1 ]
