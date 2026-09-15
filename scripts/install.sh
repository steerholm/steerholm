#!/usr/bin/env bash
set -euo pipefail

REPO="steerholm/steerholm"
SERVICE_NAME="steerholm"
PLIST_FILE="${HOME}/Library/LaunchAgents/dev.steerholm.daemon.plist"
INSTALL_DIR="${HOME}/.local/bin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; exit 1; }

# ── 1. Detect platform ────────────────────────────────────────────

OS=$(uname -s)
ARCH=$(uname -m)

case "${OS}-${ARCH}" in
    Linux-x86_64)  PLATFORM="linux-x64" ;;
    Darwin-arm64)  PLATFORM="darwin-arm64" ;;
    Darwin-x86_64) PLATFORM="darwin-arm64" ;; # x86_64 Python on Apple Silicon; native Intel Macs unsupported
    *) error "Unsupported platform: ${OS}-${ARCH}" ;;
esac

info "Detected platform: ${PLATFORM}"

# ── 2. Obtain release archive (download, or use a local one) ───────

ASSET="steerholm-${PLATFORM}.tar.gz"
TMP_DIR=$(mktemp -d)
# Cleanup (including this temp dir) is installed in step 3, once there is
# also daemon state to restore on failure.

if [ -n "${STEERHOLM_LOCAL_ARCHIVE:-}" ]; then
    # Local-file mode (used for testing): install from a provided archive,
    # no download and no checksum lookup.
    [ -f "$STEERHOLM_LOCAL_ARCHIVE" ] || error "Local archive not found: ${STEERHOLM_LOCAL_ARCHIVE}"
    info "Installing from local archive: ${STEERHOLM_LOCAL_ARCHIVE}"
    cp "$STEERHOLM_LOCAL_ARCHIVE" "${TMP_DIR}/release.tar.gz"
else
    if [ -n "${STEERHOLM_VERSION:-}" ]; then
        LATEST="${STEERHOLM_VERSION}"
    else
        LATEST=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    fi
    if [ -z "$LATEST" ]; then
        error "Could not determine latest release."
    fi

    info "Downloading ${LATEST}..."

    DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${LATEST}/${ASSET}"
    CHECKSUM_URL="https://github.com/${REPO}/releases/download/${LATEST}/checksums.txt"

    curl -fsSL "$DOWNLOAD_URL" -o "${TMP_DIR}/release.tar.gz" || error "Download failed. Check https://github.com/${REPO}/releases"

    # ── 2b. Verify checksum ────────────────────────────────────────
    if curl -fsSL "$CHECKSUM_URL" -o "${TMP_DIR}/checksums.txt"; then
        EXPECTED=$(grep -F "$ASSET" "${TMP_DIR}/checksums.txt" | awk '{print $1}')
        [ -n "$EXPECTED" ] || error "checksums.txt has no entry for ${ASSET}"

        if command -v sha256sum >/dev/null 2>&1; then
            ACTUAL=$(sha256sum "${TMP_DIR}/release.tar.gz" | awk '{print $1}')
        else
            ACTUAL=$(shasum -a 256 "${TMP_DIR}/release.tar.gz" | awk '{print $1}')
        fi

        [ "$EXPECTED" = "$ACTUAL" ] || error "Checksum verification failed for ${ASSET}"
        info "Checksum verified"
    else
        warn "checksums.txt not available for ${LATEST}; skipping verification"
    fi
fi

tar -xzf "${TMP_DIR}/release.tar.gz" -C "$TMP_DIR"

# ── 3. Stop the daemon ────────────────────────────────────────────

# Stop before swapping the binary, not after. The unit is Restart=on-failure
# with RestartSec=5, so a daemon that exits any time between the swap and the
# restart below comes back up running the NEW binary against state the installer
# has not finished preparing. Windows already had to do this (it cannot replace
# a running executable); Linux and macOS get the same ordering here.

# Is a Steerholm daemon answering? Checks the service signature, not just "some
# 2xx on 4767" — /healthz reports `"service":"steerholm"` precisely so a
# different process holding the port is not a false positive. Bounded: a daemon
# wedged mid-shutdown can accept the connection and never reply, and an un-timed
# probe would hang the installer here with the daemon stopped and the binary
# not yet swapped.
steerholm_up() {
    curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:4767/healthz" 2>/dev/null | grep -q '"service":"steerholm"'
}

# Wait for it to go away. `launchctl unload` returns before the job's processes
# exit, so this is what makes the stop actually synchronous on macOS.
drain_daemon() {
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not available; cannot confirm the daemon stopped."
        return 0
    fi
    i=0
    while [ "$i" -lt 20 ]; do
        steerholm_up || return 0
        sleep 0.5 2>/dev/null || sleep 1   # fractional sleep is not universal
        i=$((i + 1))
    done
    return 1
}

# `systemctl restart` and `launchctl load` both return before the daemon has
# bound the port, and can return 0 on a unit that immediately fails. Step 3 took
# the daemon down deliberately, so claiming it is back without looking would end
# the install with no daemon and a message saying otherwise.
confirm_daemon_started() {
    i=0
    while [ "$i" -lt 20 ]; do
        if steerholm_up; then
            info "Daemon started on 127.0.0.1:4767"
            return 0
        fi
        sleep 0.5 2>/dev/null || sleep 1
        i=$((i + 1))
    done
    warn "Daemon did not answer on 127.0.0.1:4767 within 10s."
    warn "Check it with: holm status"
}

start_daemon() {
    if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
        systemctl --user start "$SERVICE_NAME" 2>/dev/null || true
    elif [ "$OS" = "Darwin" ] && [ -f "$PLIST_FILE" ]; then
        launchctl load "$PLIST_FILE" 2>/dev/null || true
    fi
}

# Deliberately NOT gated on STEERHOLM_NO_SERVICE: that flag means "do not
# register a service", not "nothing is running". A daemon from an earlier
# install would otherwise have its binary swapped underneath it and never be
# restarted. install.ps1 orders it the same way, for the same reason.
stop_daemon() {
    # Reuse the CLI where it exists: `holm stop` already knows the per-platform
    # stop and the plist path. On a fresh install there is no binary yet.
    if [ -x "${INSTALL_DIR}/holm" ]; then
        "${INSTALL_DIR}/holm" stop >/dev/null 2>&1 || true
    elif [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    elif [ "$OS" = "Darwin" ] && [ -f "$PLIST_FILE" ]; then
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
    fi
    DAEMON_STOPPED=1
    if ! drain_daemon; then
        warn "A Steerholm daemon is still answering on 127.0.0.1:4767."
        warn "Stop it (\`holm stop\`, or kill the process) and re-run this installer."
        exit 1
    fi
}

DAEMON_STOPPED=0
INSTALL_DONE=0

# The daemon is deliberately down from here until step 5 starts it again. Any
# abort in between — disk full, read-only HOME, Ctrl-C — would otherwise leave
# the machine with no daemon and no message saying so.
cleanup() {
    rc=$?
    rm -rf "${TMP_DIR}"
    if [ "$rc" -ne 0 ] && [ "$DAEMON_STOPPED" = 1 ] && [ "$INSTALL_DONE" = 0 ]; then
        warn "Install did not complete; restarting the daemon it stopped."
        start_daemon
    fi
}
trap cleanup EXIT

stop_daemon


# ── 4. Install binaries ───────────────────────────────────────────

mkdir -p "$INSTALL_DIR"
# Atomic replace: write to a temp name in the same dir, then rename over the
# target. An in-place `cp` fails with "text file busy" on Linux (and can crash
# the running process on macOS) when the binary is already executing — which it
# is during `holm update`, where both the daemon and the updater run this file.
# A rename swaps the directory entry; running processes keep the old inode.
cp "${TMP_DIR}/holm" "${INSTALL_DIR}/holm.new"
chmod +x "${INSTALL_DIR}/holm.new"
mv -f "${INSTALL_DIR}/holm.new" "${INSTALL_DIR}/holm"

# Check PATH
if ! echo "$PATH" | grep -q "$INSTALL_DIR"; then
    warn "${INSTALL_DIR} is not in your PATH. Add it:"
    echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
fi

HOLM_BIN="${INSTALL_DIR}/holm"
info "Installed holm at ${HOLM_BIN}"

# ── Migrate the on-disk state ─────────────────────────────────────

# Run by the NEWLY installed binary, not the one that started this script: the
# process running `holm update` is the old version and cannot contain the new
# schema steps. The daemon is stopped (step 3) and not started until step 5, so
# nothing is reading or writing the state while this runs.
if ! "$HOLM_BIN" migrate; then
    # Set BEFORE reporting: `error` exits, so anything after it never runs. The
    # EXIT trap would otherwise restart the daemon, and against an un-migrated
    # state that daemon refuses to serve — a restart loop instead of one clear
    # message.
    DAEMON_STOPPED=0
    warn "The new binary is installed but the state was not migrated, so the"
    warn "daemon will refuse to start against it. It has been left stopped."
    error "Could not migrate the Steerholm state. Fix the cause above, then re-run this installer."
fi

# ── 5. Register service ───────────────────────────────────────────

if [ -n "${STEERHOLM_NO_SERVICE:-}" ]; then
    info "Skipping service registration (STEERHOLM_NO_SERVICE set)."
    info "Run the daemon manually with: holm serve"
    echo ""
    info "Installation complete."
    INSTALL_DONE=1
    exit 0
fi

if [ "$OS" = "Linux" ]; then
    UNIT_DIR="${HOME}/.config/systemd/user"
    UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"

    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Steerholm Daemon
After=network.target

[Service]
Type=simple
ExecStart=${HOLM_BIN} serve
Restart=on-failure
RestartSec=5
# 78 is EX_CONFIG: the daemon refusing a state whose schema it cannot read.
# Restarting cannot fix that, and without this it would retry every RestartSec
# forever. See entry_holmd.py.
RestartPreventExitStatus=78

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    # The daemon was stopped in step 3, so this is a plain start. `restart` is
    # kept rather than `start` only so an interrupted stop cannot leave the old
    # binary running — restarting an already-stopped unit just starts it.
    systemctl --user restart "$SERVICE_NAME"

    info "Registered systemd user service"
    confirm_daemon_started

elif [ "$OS" = "Darwin" ]; then
    PLIST_DIR=$(dirname "$PLIST_FILE")

    mkdir -p "$PLIST_DIR"

    LOG_DIR="${HOME}/.steerholm"
    mkdir -p "$LOG_DIR"

    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.steerholm.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>${HOLM_BIN}</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/daemon.log</string>
</dict>
</plist>
EOF

    # Unload again: the plist was just rewritten and launchd caches the old
    # definition. This is a second teardown, so drain again — `launchctl unload`
    # returns before the job's processes exit, and loading while the old one
    # still holds 4767 makes the new agent fail to bind and launchd throttle it.
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    drain_daemon || warn "Old daemon still answering; the reload may fail to bind."
    launchctl load "$PLIST_FILE"

    info "Registered launchd agent"
    confirm_daemon_started
fi

echo ""
info "Manage with:"
echo "  holm status"
echo "  holm stop"
echo "  holm start"
echo ""
info "Installation complete."

INSTALL_DONE=1
