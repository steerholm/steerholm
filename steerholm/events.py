"""Decision events — the audit stream emitted at the policy enforcement point.

Mission Control M1. Every tool call the gateway evaluates produces one
`DecisionEvent` (allowed/denied + outcome), appended to an owner-only JSONL log
and kept in a bounded in-memory ring so a reader can replay recent history
without touching the file. Auditing must never break a call, so the file write is
best-effort — a failure is logged and the ring still holds the event.
"""
import logging
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
    reason: Optional[str] = None
    result: str = "error"            # "ok" | "error"
    latency_ms: Optional[int] = None
    args_summary: str = ""


class EventLog:
    """Append-only JSONL audit log plus a bounded in-memory ring of recent events.

    The JSONL file is the on-disk audit trail (best-effort — buffered writes are
    not fsync'd, so a crash may lose the most recent lines); the ring lets a reader
    (the control plane, later) replay the last N events instantly. The file path is
    resolved lazily off `config.CONFIG_DIR` so a `STEERHOLM_CONFIG_DIR` override (or
    a test's monkeypatched dir) is honoured.
    """

    def __init__(self, path=None, ring_size: int = 500):
        self._path = path
        self._ring: Deque[DecisionEvent] = deque(maxlen=ring_size)

    @property
    def path(self):
        return self._path or (_config.CONFIG_DIR / "events.jsonl")

    def record(self, event: DecisionEvent) -> None:
        self._ring.append(event)
        try:
            path = self.path
            with open(path, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
            _config._restrict(path, 0o600)
        except Exception as e:  # audit is best-effort; never break the call path
            logger.warning("Could not append to event log %s: %s", self.path, e)

    def recent(self, limit: Optional[int] = None) -> List[DecisionEvent]:
        events = list(self._ring)
        if limit is None:
            return events
        return events[-limit:] if limit > 0 else []  # limit 0 -> none (not `[-0:]` = all)
