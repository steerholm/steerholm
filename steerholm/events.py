"""Decision events — the audit stream emitted at the policy enforcement point.

Mission Control M1. Every tool call the gateway evaluates produces one
`DecisionEvent` (allowed/denied + outcome), appended to an owner-only JSONL log
and kept in a bounded in-memory ring so a reader can replay recent history
without touching the file. Auditing must never break a call, so the file write is
best-effort — a failure is logged and the ring still holds the event.
"""
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Optional

from pydantic import BaseModel

from . import config as _config

logger = logging.getLogger("steerholm.events")

# Long values are truncated so an event stays a bounded one-liner. This is
# summarisation, not redaction: the audit log deliberately records what an agent
# actually did — real argument values and denial reasons — because that is the
# point of an audit, and an agent's tool arguments rarely carry secrets (server
# credentials are injected by the daemon via --env, never passed by the agent).
# The log is written owner-only (0600); treat it as sensitive when exporting it.
_MAX_VALUE_LEN = 80


def now_iso() -> str:
    """A UTC ISO-8601 timestamp for an event."""
    return datetime.now(timezone.utc).isoformat()


def summarize_args(arguments: Optional[dict]) -> str:
    """A compact one-line view of a tool call's arguments (`key=value`), with long
    values truncated. Values are recorded faithfully, not scrubbed."""
    if not arguments:
        return ""
    parts: List[str] = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > _MAX_VALUE_LEN:
            text = text[:_MAX_VALUE_LEN] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


class DecisionEvent(BaseModel):
    ts: str
    agent: str                       # name (human-readable label)
    agent_id: Optional[str] = None   # immutable principal id; filter by this, not name
    tool: str
    decision: str                    # "allowed" | "denied"
    server: Optional[str] = None
    server_id: Optional[str] = None  # immutable; a server name can be reused too
    reason: Optional[str] = None
    result: str = "error"            # "ok" | "error"
    latency_ms: Optional[int] = None
    args_summary: str = ""


# Rotation: the writer appends to the highest-numbered segment and starts the next
# one when a record would take it past MAX_SEGMENT_BYTES; retention deletes the
# lowest numbers. Segments are never renamed — a rename can fail while a reader
# holds the file open (Windows) and races a reader mid-rotation — so rotating is
# just "start writing the next number", which readers see as a new file appearing.
SEGMENT_PREFIX = "events-"
SEGMENT_SUFFIX = ".jsonl"
MAX_SEGMENT_BYTES = 10 * 1024 * 1024
MAX_LOG_FILES = 5

# Only these are segments. A loose match would let an unrelated file (a user's
# `events-old.jsonl` archive) become the write target, and its unparseable number
# would send the next roll onto an existing segment.
_SEGMENT_RE = re.compile(r"^events-\d{6,}\.jsonl$")


def segment_number(path) -> int:
    """The ordinal encoded in a segment's name, or 0 if it isn't a segment."""
    try:
        if not _SEGMENT_RE.match(path.name):
            return 0
        return int(path.stem.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def list_segments(directory) -> List:
    """Every audit log segment in `directory`, oldest first.

    Sorted by ordinal rather than by name, so ordering survives the width change
    when the counter passes 999999. Raises if the directory cannot be listed —
    callers that must not confuse "unreadable" with "empty" use this directly.
    """
    segments = [p for p in directory.glob(f"{SEGMENT_PREFIX}*{SEGMENT_SUFFIX}")
                if _SEGMENT_RE.match(p.name)]
    return sorted(segments, key=segment_number)


def log_files(directory) -> List:
    """`list_segments`, treating an unreadable directory as "no log yet"."""
    try:
        return list_segments(directory)
    except Exception:
        return []


class EventLog:
    """Append-only JSONL audit log plus a bounded in-memory ring of recent events.

    The files are the on-disk audit trail (best-effort — buffered writes are not
    fsync'd, so a crash may lose the most recent lines); the ring lets a reader
    replay the last N events instantly. The directory is resolved lazily off
    `config.CONFIG_DIR` so a `STEERHOLM_CONFIG_DIR` override (or a test's
    monkeypatched dir) is honoured.
    """

    def __init__(self, dir=None, ring_size: int = 500,
                 max_bytes: int = MAX_SEGMENT_BYTES, max_files: int = MAX_LOG_FILES,
                 max_age_days: int = 0):
        self._dir = dir
        self._ring: Deque[DecisionEvent] = deque(maxlen=ring_size)
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._max_age_days = max_age_days
        self._current = None
        self._size = 0
        self._resume_current_segment()
        self._repair_torn_last_line()
        # Apply retention at startup too, so an age limit still expires segments
        # on a daemon that has been idle (rotation alone only runs on a write).
        self._prune()

    def configure(self, max_files: int = None, max_age_days: int = None) -> None:
        """Apply changed retention settings without restarting the daemon.

        Segment size is intentionally absent: changing it at runtime would leave
        existing segments at the old size, so it is fixed at construction.
        """
        if max_files is not None:
            self._max_files = max_files
        if max_age_days is not None:
            self._max_age_days = max_age_days
        self._prune()

    @property
    def dir(self):
        return self._dir if self._dir is not None else _config.CONFIG_DIR

    @property
    def path(self):
        """The segment currently being appended to."""
        if self._current is None:
            self._resume_current_segment()
        return self._current

    def _segment(self, number: int):
        return self.dir / f"{SEGMENT_PREFIX}{number:06d}{SEGMENT_SUFFIX}"

    def _segments(self) -> List:
        return log_files(self.dir)

    def _next_segment(self, segments=None):
        """The path to roll to: one past the highest ordinal that exists.

        Taken from the whole directory rather than from `_current + 1`, so a roll
        can never land on a segment that already holds records.
        """
        segments = self._segments() if segments is None else segments
        highest = max((segment_number(s) for s in segments), default=0)
        return self._segment(max(highest, segment_number(self._current or self._segment(0))) + 1)

    def _resume_current_segment(self) -> None:
        """Continue appending to the newest existing segment, or start the first.

        A directory we cannot list is NOT an empty one: restarting the numbering
        while segments exist would write new records into an old file and invert
        the order readers see. Leave `_current` unset and retry later instead.
        """
        try:
            segments = list_segments(self.dir)  # propagates: unreadable != empty
        except Exception:
            self._current, self._size = None, 0
            return
        try:
            self._current = segments[-1] if segments else self._segment(1)
            self._size = self._current.stat().st_size if self._current.exists() else 0
        except Exception:
            self._current, self._size = None, 0

    def _repair_torn_last_line(self) -> None:
        """Close a line left half-written by a previous run that died mid-append.

        Without this the next append fuses onto the torn line, so a reader loses
        both it and the first real event after it. Run at startup and again after
        a failed write, which can also leave a partial line (a short flush on
        ENOSPC) inside a running daemon.
        """
        try:
            path = self.path
            if path is None or not path.exists() or path.stat().st_size == 0:
                return
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) == b"\n":
                    return
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write("\n")
            self._size += 1
        except Exception as e:  # best-effort, like the rest of the audit path
            logger.warning("Could not repair the event log's last line: %s", e)

    @staticmethod
    def _event_time(line: str) -> Optional[float]:
        """Epoch seconds for one JSONL line, or None if it carries no usable ts."""
        try:
            stamp = json.loads(line).get("ts")
            return datetime.fromisoformat(stamp).timestamp() if stamp else None
        except Exception:
            # Includes a ts that parses as JSON but not as a date (a number, or
            # free text): skip that line rather than abandoning the whole file.
            return None

    def _newest_event_time(self, path) -> Optional[float]:
        """Epoch seconds of the newest event in a segment, from the events' own
        `ts` fields.

        Age comes from the records, not the file's mtime: mtime is metadata that a
        restore (`tar -x`, `rsync -a`) preserves and a copy (`cp -r`) resets, so
        keying retention on it would either delete a restored audit trail or never
        expire a copied one.

        Reads the tail in growing windows until a dated line is found, because a
        single record can exceed any fixed window (a long failure `reason` is not
        truncated). Takes the MAXIMUM over the window rather than the last line:
        `ts` is wall-clock, so a backward clock step can leave an older stamp last,
        and trusting it would expire a segment still holding recent records.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return None
        for window in (8192, 65536, 524288, size):
            window = min(window, size)
            try:
                with open(path, "rb") as f:
                    f.seek(size - window)
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                return None
            # A partial first line is dropped unless we read the whole file.
            lines = tail.splitlines()[(1 if window < size else 0):]
            stamps = [t for t in (self._event_time(line) for line in lines) if t]
            if stamps:
                return max(stamps)
            if window >= size:
                break
        return None

    def _is_expired(self, path, cutoff: float) -> bool:
        """True if every event in the segment predates `cutoff`."""
        newest = self._newest_event_time(path)
        if newest is None:
            return False  # unreadable or undated — never delete on a guess
        return newest < cutoff

    def _prune(self) -> None:
        """Apply retention: drop segments beyond the max-files count and any whose
        events all predate the age limit. Either limit set to 0 means "no limit",
        and the segment currently being written is never removed."""
        if self._current is None:
            self._resume_current_segment()  # never treat "unknown" as "prunable"
        cutoff = (time.time() - self._max_age_days * 86400
                  if self._max_age_days and self._max_age_days > 0 else None)

        # An age limit has to be able to expire the segment being written, or a
        # quiet install that never reaches the size threshold keeps history for
        # ever. Roll it first so it becomes an ordinary, prunable segment.
        if cutoff is not None and self._current is not None and self._size:
            if self._is_expired(self._current, cutoff):
                self._current = self._next_segment(segments=self._segments())
                self._size = 0

        segments = self._segments()  # log_files() already swallows listing errors
        older = [s for s in segments if s != self._current]  # oldest first
        doomed = []
        if self._max_files and self._max_files > 0:
            excess = len(segments) - self._max_files
            if excess > 0:
                doomed.extend(older[:excess])
        if cutoff is not None:
            doomed.extend(s for s in older if self._is_expired(s, cutoff))
        for old in dict.fromkeys(doomed):  # de-duplicate, preserve order
            try:
                old.unlink()
            except OSError as e:
                logger.warning("Could not apply retention to %s: %s", old, e)

    def record(self, event: DecisionEvent) -> None:
        self._ring.append(event)
        line = event.model_dump_json() + "\n"
        size = len(line.encode("utf-8"))
        rolled = False
        try:
            if self._current is None:
                self._resume_current_segment()
            # Roll before writing so a segment never exceeds the cap. Never roll on
            # an empty segment, or an over-sized single event would spin forever.
            if self._size and self._size + size > self._max_bytes:
                self._current = self._next_segment(segments=self._segments())
                self._size = 0
                rolled = True
            with open(self._current, "a", encoding="utf-8", newline="") as f:
                f.write(line)
            self._size += size
            _config._restrict(self._current, 0o600)
            if rolled:
                self._prune()
        except Exception as e:  # audit is best-effort; never break the call path
            logger.warning("Could not append to event log %s: %s", self._current, e)
            # A failed write may have flushed part of a line and left `_size`
            # behind the real file. Re-sync from disk and close the torn line so
            # the next record cannot fuse onto it.
            self._resume_current_segment()
            self._repair_torn_last_line()

    def recent(self, limit: Optional[int] = None) -> List[DecisionEvent]:
        events = list(self._ring)
        if limit is None:
            return events
        return events[-limit:] if limit > 0 else []  # limit 0 -> none (not `[-0:]` = all)
