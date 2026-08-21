#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="mcp-harbour"
INSTALL_DIR="${HOME}/.local/bin"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }

OS=$(uname -s)

# ── 1. Stop and remove service ─────────────────────────────────────

if [ "$OS" = "Linux" ]; then
    UNIT_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
    if [ -f "$UNIT_FILE" ]; then
        systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload
        info "Removed systemd service."
    fi

elif [ "$OS" = "Darwin" ]; then
    PLIST_FILE="${HOME}/Library/LaunchAgents/dev.mcp-harbour.daemon.plist"
    if [ -f "$PLIST_FILE" ]; then
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        rm -f "$PLIST_FILE"
        info "Removed launchd agent."
    fi
fi

# ── 2. Remove binaries ────────────────────────────────────────────

rm -f "${INSTALL_DIR}/harbour"
info "Removed binaries."

info "Uninstall complete."
info "Config files remain at ~/.mcp-harbour — delete manually if desired."
