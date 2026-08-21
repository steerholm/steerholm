"""PyInstaller entry point for the windowless Steerholm daemon (Windows).

Built with --windowed so the per-user logon Scheduled Task runs the daemon with
no console window. It serves the same gateway as `holm serve`, but as a
background process: since no console is attached, all output goes to a log file.

This is not a Windows service — it runs as the user via the logon task, exactly
like the systemd --user unit (Linux) and the LaunchAgent (macOS).
"""
import os
import sys

# In --windowed mode there is no console, so sys.stdout/sys.stderr are None.
# Redirect to a log file before anything (logging, uvicorn) tries to write.
_log_dir = os.path.join(os.environ.get("APPDATA", ""), "steerholm")
try:
    os.makedirs(_log_dir, exist_ok=True)
    _log = open(os.path.join(_log_dir, "daemon.log"), "a", buffering=1, encoding="utf-8")
except OSError:
    # Never leave stdout/stderr as None in --windowed mode (logging would break).
    _log = open(os.devnull, "w")
sys.stdout = _log
sys.stderr = _log

import asyncio
import logging

from steerholm.gateway import SteerholmGateway
from steerholm.config import DEFAULT_HOST, DEFAULT_PORT

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)

if __name__ == "__main__":
    logging.info("Starting Steerholm daemon on http://%s:%s/mcp", DEFAULT_HOST, DEFAULT_PORT)
    asyncio.run(SteerholmGateway().serve(DEFAULT_HOST, DEFAULT_PORT))
