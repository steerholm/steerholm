"""Unit tests for the decision-event audit log (Mission Control M1)."""
import json
import os
import stat

import pytest

from steerholm.events import DecisionEvent, EventLog, now_iso, summarize_args


def _event(**kw):
    base = dict(ts=now_iso(), agent="a", tool="t", decision="allowed", result="ok")
    base.update(kw)
    return DecisionEvent(**base)


class TestSummarizeArgs:
    def test_empty(self):
        assert summarize_args(None) == ""
        assert summarize_args({}) == ""

    def test_plain_values(self):
        assert summarize_args({"path": "/x", "n": 3}) == "path=/x, n=3"

    def test_records_values_faithfully_without_scrubbing(self):
        # By design the audit records what the agent actually did; values are not
        # masked (agents rarely pass secrets, and the log is owner-only).
        s = summarize_args({"host": "db", "password": "hunter2"})
        assert s == "host=db, password=hunter2"

    def test_truncates_long_values_at_the_boundary(self):
        s = summarize_args({"q": "x" * 200})
        assert s.endswith("…")
        assert "x" * 80 in s          # cut at _MAX_VALUE_LEN (80)...
        assert "x" * 81 not in s      # ...and no further

    def test_non_scalar_values_are_stringified(self):
        s = summarize_args({"flag": True, "opts": {"a": 1}, "xs": [1, 2], "z": None})
        assert s == "flag=True, opts={'a': 1}, xs=[1, 2], z=None"


class TestEventLog:
    def test_record_and_recent_preserve_order(self, tmp_path):
        log = EventLog(path=tmp_path / "events.jsonl", ring_size=100)
        for i in range(3):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent()] == ["t0", "t1", "t2"]

    def test_recent_limit(self, tmp_path):
        log = EventLog(path=tmp_path / "events.jsonl")
        for i in range(5):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent(limit=2)] == ["t3", "t4"]

    def test_recent_zero_returns_empty(self, tmp_path):
        # Guard the `events[-0:] == events[0:]` (whole ring) footgun.
        log = EventLog(path=tmp_path / "events.jsonl")
        for i in range(3):
            log.record(_event(tool=f"t{i}"))
        assert log.recent(0) == []
        assert len(log.recent()) == 3   # None -> all

    def test_ring_is_bounded(self, tmp_path):
        log = EventLog(path=tmp_path / "events.jsonl", ring_size=3)
        for i in range(10):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent()] == ["t7", "t8", "t9"]

    def test_writes_jsonl_roundtrip(self, tmp_path):
        path = tmp_path / "events.jsonl"
        log = EventLog(path=path)
        log.record(_event(tool="read", decision="denied", reason="nope", result="error"))
        log.record(_event(tool="write"))
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        first = DecisionEvent(**json.loads(lines[0]))
        assert first.tool == "read"
        assert first.decision == "denied"
        assert first.reason == "nope"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows isolates AppData via ACLs")
    def test_log_file_is_owner_only(self, tmp_path):
        path = tmp_path / "events.jsonl"
        EventLog(path=path).record(_event())
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_record_survives_a_write_error(self, tmp_path):
        # Parent dir doesn't exist -> open() fails, but record must not propagate;
        # the in-memory ring still captures the event.
        log = EventLog(path=tmp_path / "missing" / "events.jsonl")
        log.record(_event(tool="x"))
        assert [e.tool for e in log.recent()] == ["x"]

    def test_default_path_tracks_config_dir(self, tmp_path, monkeypatch):
        import steerholm.config as config_mod
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        assert EventLog().path == tmp_path / "events.jsonl"


class TestTornLineRepair:
    def test_leaves_a_well_formed_log_untouched(self, tmp_path):
        path = tmp_path / "events.jsonl"
        body = '{"ts":"t","agent":"a","tool":"t","decision":"allowed"}\n'
        path.write_text(body)
        EventLog(path=path)                      # construction triggers the check
        assert path.read_text() == body          # already ends on a line boundary

    def test_closes_a_torn_last_line(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_bytes(b'{"ts":"2026-08-3')    # killed mid-write
        EventLog(path=path)
        assert path.read_bytes().endswith(b"\n")

    def test_repair_failure_is_swallowed(self, tmp_path):
        class _Unreadable:
            def exists(self):
                raise OSError("boom")
        EventLog(path=_Unreadable())             # must not raise
