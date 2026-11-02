"""Writing state files: owner-only, and never half-written.

A leaf module on purpose. `config`, `events` and `migrations` all write state,
and `config` imports `migrations`, so the helpers they share cannot live in any
of them without closing an import cycle.
"""
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None

ENCODING = "utf-8"

# Windows refuses a rename over a file that another process has open, until it
# is closed. The daemon reads config.json on every request and a policy on every
# tool call, so a save can land on a read; each read is over in milliseconds.
_REPLACE_ATTEMPTS = 10
_REPLACE_WAIT = 0.05


def _flush_to_disk(fd: int) -> None:
    """`fsync`, except on macOS: there `fsync` leaves the data in the drive's
    own cache, where a power loss still loses it, and only `F_FULLFSYNC` writes
    it through. Falls back to `fsync` where the filesystem refuses that (some
    network and external volumes)."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def restrict(path: Path, mode: int) -> None:
    """Keep a state file or directory owner-only. No-op on Windows, where the
    per-user AppData ACLs provide the isolation and POSIX modes don't map."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def fsync_dir(path: Path) -> None:
    """Flush a directory's entries, so the renames into it survive a crash.
    Not possible on Windows, where a directory cannot be opened as a file."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _flush_to_disk(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace `path` with `text`, so that a reader — or a crash — finds either
    the old file or the new one, never part of one.

    Written to a temp file beside it, flushed to the device, restricted, then
    renamed over it. A plain truncate-and-write fails DURING the write on a full
    disk or a failing device, after the old contents are already gone. The temp
    name does not end in `.json`, so nothing that lists state files mistakes a
    write in flight for one.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding=ENCODING) as f:
            f.write(text)
            f.flush()
            _flush_to_disk(f.fileno())
        restrict(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_WAIT)


def read_text(path: Path) -> str:
    """Read a state file as UTF-8, or in the system's encoding if it is not.

    Up to 0.1.2 the CLI wrote config.json and policies in the system's encoding,
    which on Windows is not UTF-8, so a non-ASCII value there is in that
    encoding. The next save writes the file as UTF-8. Elsewhere the system's
    encoding is UTF-8 and the second read fails the same way as the first.
    """
    try:
        return path.read_text(encoding=ENCODING)
    except UnicodeDecodeError:
        return path.read_text(encoding="locale")
