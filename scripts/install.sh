#!/usr/bin/env bash
set -euo pipefail

REPO="steerholm/steerholm"
SERVICE_NAME="steerholm"
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
trap "rm -rf ${TMP_DIR}" EXIT

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

# ── 3. Install binaries ───────────────────────────────────────────

mkdir -p "$INSTALL_DIR"
cp "${TMP_DIR}/holm" "$INSTALL_DIR/"
chmod +x "${INSTALL_DIR}/holm"

# Check PATH
if ! echo "$PATH" | grep -q "$INSTALL_DIR"; then
    warn "${INSTALL_DIR} is not in your PATH. Add it:"
    echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
fi

HOLM_BIN="${INSTALL_DIR}/holm"
info "Installed holm at ${HOLM_BIN}"

# ── 4. Register service ───────────────────────────────────────────

if [ -n "${STEERHOLM_NO_SERVICE:-}" ]; then
    info "Skipping service registration (STEERHOLM_NO_SERVICE set)."
    info "Run the daemon manually with: holm serve"
    echo ""
    info "Installation complete."
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

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    info "Registered systemd user service"
    info "Daemon started on 127.0.0.1:4767"

elif [ "$OS" = "Darwin" ]; then
    PLIST_DIR="${HOME}/Library/LaunchAgents"
    PLIST_FILE="${PLIST_DIR}/dev.steerholm.daemon.plist"

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

    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    launchctl load "$PLIST_FILE"

    info "Registered launchd agent"
    info "Daemon started on 127.0.0.1:4767"
fi

echo ""
info "Manage with:"
echo "  holm status"
echo "  holm stop"
echo "  holm start"
echo ""
info "Installation complete."
