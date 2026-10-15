"""The environment to give every process this program starts.

A frozen (PyInstaller) build prepends its bundled libraries to LD_LIBRARY_PATH
before any of our code runs, and keeps the original in LD_LIBRARY_PATH_ORIG —
or leaves that unset when there was none. Every child inherits the altered
value, so `systemctl`, `curl` and docked MCP servers load OUR copy of OpenSSL
instead of the system's. Where the system's is newer they fail to load at all:
`version 'OPENSSL_3.4.0' not found` on Ubuntu 26.04.

It also passes on its own bookkeeping (`_PYI_ARCHIVE_FILE`,
`_PYI_APPLICATION_HOME_DIR`, ...). A frozen program that finds those treats
itself as a child of that same run and uses the PARENT's unpacked files instead
of its own. That is wrong whenever the child is a different build — the new
`holm migrate` the installer runs during an update loaded the old build's
compiled libraries and crashed.
"""
import os
import sys
from typing import Dict, Optional


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """os.environ as the user had it, plus `extra`."""
    env = dict(os.environ)
    # Only a frozen build altered it. From source, an LD_LIBRARY_PATH is the
    # user's own and must be passed on untouched.
    if getattr(sys, "frozen", False):
        for key in [k for k in env if k.startswith("_PYI_")]:
            del env[key]
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    env.update(extra or {})
    return env
