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
        log = EventLog(dir=tmp_path, ring_size=100)
        for i in range(3):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent()] == ["t0", "t1", "t2"]

    def test_recent_limit(self, tmp_path):
        log = EventLog(dir=tmp_path)
        for i in range(5):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent(limit=2)] == ["t3", "t4"]

    def test_recent_zero_returns_empty(self, tmp_path):
        # Guard the `events[-0:] == events[0:]` (whole ring) footgun.
        log = EventLog(dir=tmp_path)
        for i in range(3):
            log.record(_event(tool=f"t{i}"))
        assert log.recent(0) == []
        assert len(log.recent()) == 3   # None -> all

    def test_ring_is_bounded(self, tmp_path):
        log = EventLog(dir=tmp_path, ring_size=3)
        for i in range(10):
            log.record(_event(tool=f"t{i}"))
        assert [e.tool for e in log.recent()] == ["t7", "t8", "t9"]

    def test_writes_jsonl_roundtrip(self, tmp_path):
        log = EventLog(dir=tmp_path)
        log.record(_event(tool="read", decision="denied", reason="nope", result="error"))
        log.record(_event(tool="write"))
        lines = log.path.read_text().strip().splitlines()
        assert len(lines) == 2
        first = DecisionEvent(**json.loads(lines[0]))
        assert first.tool == "read"
        assert first.decision == "denied"
        assert first.reason == "nope"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows isolates AppData via ACLs")
    def test_log_file_is_owner_only(self, tmp_path):
        log = EventLog(dir=tmp_path)
        log.record(_event())
        path = log.path
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_record_survives_a_write_error(self, tmp_path):
        # Parent dir doesn't exist -> open() fails, but record must not propagate;
        # the in-memory ring still captures the event.
        log = EventLog(dir=tmp_path / "missing")
        log.record(_event(tool="x"))
        assert [e.tool for e in log.recent()] == ["x"]

    def test_default_path_tracks_config_dir(self, tmp_path, monkeypatch):
        import steerholm.config as config_mod
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        assert EventLog().path == tmp_path / "events-000001.jsonl"


class TestTornLineRepair:
    def test_leaves_a_well_formed_log_untouched(self, tmp_path):
        # Must be a real segment name, or the repair would simply ignore the file
        # and the assertion would hold for the wrong reason.
        path = tmp_path / "events-000001.jsonl"
        body = '{"ts":"t","agent":"a","tool":"t","decision":"allowed"}\n'
        path.write_text(body)
        EventLog(dir=tmp_path)                   # construction triggers the check
        assert path.read_text() == body          # already ends on a line boundary

    def test_closes_a_torn_last_line(self, tmp_path):
        # The segment the writer will resume appending to is the one that matters:
        # a legacy file is never appended to again, so it cannot fuse.
        path = tmp_path / "events-000001.jsonl"
        path.write_bytes(b'{"ts":"2026-08-3')    # killed mid-write
        EventLog(dir=tmp_path)
        assert path.read_bytes().endswith(b"\n")

    def test_repair_failure_is_swallowed(self, tmp_path):
        class _Unreadable:
            def exists(self):
                raise OSError("boom")
        EventLog(dir=_Unreadable())             # must not raise


class TestRotation:
    def test_rolls_to_a_new_segment_at_the_cap(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=300, max_files=100)  # keep high: pruning tested separately
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        segments = sorted(p.name for p in tmp_path.glob("events-*.jsonl"))
        assert len(segments) > 1
        assert segments[0] == "events-000001.jsonl"
        for seg in tmp_path.glob("events-*.jsonl"):
            assert seg.stat().st_size <= 300 or seg.read_text().count("\n") == 1

    def test_segments_are_never_renamed(self, tmp_path):
        # Segment 1 keeps its identity (and its content) across rotations.
        log = EventLog(dir=tmp_path, max_bytes=300, max_files=100)
        log.record(_event(tool="first"))
        first_seg = tmp_path / "events-000001.jsonl"
        inode = first_seg.stat().st_ino
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        assert first_seg.exists() and first_seg.stat().st_ino == inode
        assert "first" in first_seg.read_text()

    def test_prunes_to_the_keep_limit(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=3)
        for i in range(60):
            log.record(_event(tool=f"t{i}"))
        segments = sorted(tmp_path.glob("events-*.jsonl"))
        assert len(segments) == 3
        # The survivors are the newest ones, and the oldest was deleted.
        assert not (tmp_path / "events-000001.jsonl").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows isolates AppData via ACLs")
    def test_new_segments_are_owner_only(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200)
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        for seg in tmp_path.glob("events-*.jsonl"):
            assert stat.S_IMODE(os.stat(seg).st_mode) == 0o600

    def test_resumes_the_newest_segment_after_a_restart(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=300)
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        newest = log.path
        # A daemon restart must continue the newest segment, not start over at 1.
        assert EventLog(dir=tmp_path, max_bytes=300).path == newest

    def test_an_oversized_event_does_not_spin(self, tmp_path):
        # A single event larger than the cap still gets written (to its own segment).
        log = EventLog(dir=tmp_path, max_bytes=50)
        log.record(_event(tool="x" * 500))
        assert sum(p.stat().st_size for p in tmp_path.glob("events-*.jsonl")) > 500


class TestRotationEdges:
    def test_segment_number_rejects_non_segments(self, tmp_path):
        from steerholm.events import segment_number
        assert segment_number(tmp_path / "events-000007.jsonl") == 7
        assert segment_number(tmp_path / "events.jsonl") == 0     # not a segment
        assert segment_number(tmp_path / "events-old.jsonl") == 0  # a user's archive
        assert segment_number(None) == 0

    def test_resume_survives_an_unusable_directory(self, tmp_path):
        class _Unreadable:
            def __truediv__(self, other):
                raise OSError("boom")
            def glob(self, pattern):
                raise OSError("boom")
        log = EventLog(dir=_Unreadable())
        assert log.path is None
        log.record(_event(tool="x"))                  # must not raise
        assert [e.tool for e in log.recent()] == ["x"]

    def test_prune_ignores_an_undeletable_segment(self, tmp_path, monkeypatch):
        # A segment held open by a reader (Windows) can't be deleted; pruning must
        # skip it rather than break the record that triggered the rotation.
        import os as _os, pathlib as _pl, time
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)   # let segments pile up
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        before = len(list(tmp_path.glob("events-*.jsonl")))
        assert before > 1
        for seg in tmp_path.glob("events-*.jsonl"):           # all now expired
            _os.utime(seg, (time.time() - 86400 * 30,) * 2)

        monkeypatch.setattr(_pl.Path, "unlink",
                            lambda self, **kw: (_ for _ in ()).throw(OSError("locked")))
        log.configure(max_age_days=1)                          # every unlink fails
        assert len(list(tmp_path.glob("events-*.jsonl"))) == before   # swallowed

    def test_prune_keeps_a_segment_it_cannot_read(self, tmp_path):
        # If the age of a segment can't be established, retention must not guess
        # and delete it.
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        for i in range(10):
            log.record(_event(tool=f"t{i}"))
        oldest = sorted(tmp_path.glob("events-*.jsonl"))[0]
        oldest.write_bytes(b"not json at all\n")      # undated, unreadable as events
        log.configure(max_age_days=1)
        assert oldest.exists()


class TestAgeRetention:
    """Age is taken from the events themselves, not the file's mtime, so a restore
    that preserves mtimes cannot wipe history and a copy cannot freeze it."""

    def _old(self, days):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _fill(self, log, days_ago, n=20):
        for i in range(n):
            log.record(_event(ts=self._old(days_ago), tool=f"t{i}"))

    def test_expires_segments_whose_events_are_old(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=60)
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 1
        log.configure(max_age_days=30)
        assert list(tmp_path.glob("events-*.jsonl")) == []      # all events predate the cutoff

    def test_keeps_segments_whose_events_are_recent(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=1)
        before = len(list(tmp_path.glob("events-*.jsonl")))
        log.configure(max_age_days=30)
        assert len(list(tmp_path.glob("events-*.jsonl"))) == before

    def test_mtime_alone_does_not_expire_a_segment(self, tmp_path):
        # A restore (tar -x / rsync -a) preserves old mtimes; that must not delete
        # an audit trail whose events are recent.
        import os, time
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=1)
        stale = time.time() - 86400 * 365
        for seg in tmp_path.glob("events-*.jsonl"):
            os.utime(seg, (stale, stale))
        log.configure(max_age_days=30)
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 0   # survives a restore

    def test_an_expired_current_segment_is_rolled_then_removed(self, tmp_path):
        # A quiet install may never reach the size threshold; the age limit must
        # still expire it, or retention would be a no-op there.
        log = EventLog(dir=tmp_path, max_bytes=10_000_000, max_files=0)
        self._fill(log, days_ago=90, n=3)
        assert len(list(tmp_path.glob("events-*.jsonl"))) == 1
        log.configure(max_age_days=30)
        assert list(tmp_path.glob("events-*.jsonl")) == []

    def test_never_deletes_the_segment_it_will_write_to(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=10_000_000, max_files=0, max_age_days=1)
        log.record(_event(tool="fresh"))
        log.configure()
        log.record(_event(tool="after"))                        # still writable
        assert log.path.exists()
        assert "after" in log.path.read_text()

    def test_age_zero_means_no_age_limit(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0, max_age_days=0)
        self._fill(log, days_ago=3650)
        log.configure()
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 1

    def test_max_files_zero_means_no_count_limit(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=1, n=40)
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 5

    def test_count_limit_applies_on_its_own(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=3)
        self._fill(log, days_ago=1, n=40)
        assert len(list(tmp_path.glob("events-*.jsonl"))) == 3

    def test_age_removes_what_the_count_limit_kept(self, tmp_path):
        # Both limits apply: the count trims to 3, then the age limit takes those.
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=3)
        self._fill(log, days_ago=90, n=40)
        assert len(list(tmp_path.glob("events-*.jsonl"))) == 3
        log.configure(max_age_days=30)
        assert list(tmp_path.glob("events-*.jsonl")) == []


    def test_retention_runs_at_startup(self, tmp_path):
        # An idle daemon writes nothing, so age must also be applied on construction.
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=90)
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 1
        EventLog(dir=tmp_path, max_bytes=200, max_files=0, max_age_days=30)
        assert list(tmp_path.glob("events-*.jsonl")) == []

    def test_configure_changes_limits_live(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        self._fill(log, days_ago=1, n=40)
        assert len(list(tmp_path.glob("events-*.jsonl"))) > 2
        log.configure(max_files=2)
        assert len(list(tmp_path.glob("events-*.jsonl"))) == 2




class TestRetentionRobustness:
    def test_repair_failure_is_logged_not_raised(self, tmp_path):
        (tmp_path / "events-000001.jsonl").mkdir()      # exists but unopenable
        log = EventLog(dir=tmp_path)
        log.record(_event(tool="x"))                    # must not raise
        assert [e.tool for e in log.recent()] == ["x"]

    def test_newest_event_time_of_an_unreadable_file(self, tmp_path):
        log = EventLog(dir=tmp_path)
        assert log._newest_event_time(tmp_path / "does-not-exist.jsonl") is None

    def test_undeletable_segment_is_logged_not_raised(self, tmp_path, monkeypatch):
        import pathlib as _pl
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=2)
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        monkeypatch.setattr(_pl.Path, "unlink",
                            lambda self, **kw: (_ for _ in ()).throw(OSError("locked")))
        log.configure(max_files=1)                      # every unlink fails
        assert len(list(tmp_path.glob("events-*.jsonl"))) >= 1


class TestAgeEdges:
    """The tail-window and timestamp cases that make a segment un-ageable."""

    def _old_iso(self, days):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_a_ts_that_is_not_a_date_falls_back_to_an_earlier_line(self, tmp_path):
        # A ts that parses as JSON but not as a date must skip that line, not
        # abandon the file — otherwise the segment could never expire.
        seg = tmp_path / "events-000001.jsonl"
        seg.write_text(
            json.dumps({"ts": self._old_iso(400), "tool": "dated"}) + "\n"
            + json.dumps({"ts": 1755000000, "tool": "numeric"}) + "\n"
            + json.dumps({"ts": "yesterday", "tool": "prose"}) + "\n"
        )
        log = EventLog(dir=tmp_path, max_files=0)
        assert log._newest_event_time(seg) is not None
        log.configure(max_age_days=30)
        assert not seg.exists()

    def test_a_record_larger_than_the_tail_window_is_still_dated(self, tmp_path):
        # `reason` is not truncated, so one record can exceed any fixed window.
        seg = tmp_path / "events-000001.jsonl"
        seg.write_text(json.dumps(
            {"ts": self._old_iso(400), "tool": "t", "reason": "x" * 20000}) + "\n")
        log = EventLog(dir=tmp_path, max_files=0)
        assert log._newest_event_time(seg) is not None
        log.configure(max_age_days=30)
        assert not seg.exists()

    def test_a_backdated_last_line_does_not_expire_recent_records(self, tmp_path):
        # ts is wall-clock: a backward clock step can leave an older stamp last.
        # Age must come from the newest stamp in the file, not the final line.
        seg = tmp_path / "events-000001.jsonl"
        seg.write_text(
            json.dumps({"ts": self._old_iso(0), "tool": "recent_denial"}) + "\n"
            + json.dumps({"ts": self._old_iso(400), "tool": "clock_stepped_back"}) + "\n"
        )
        (tmp_path / "events-000002.jsonl").write_text(
            json.dumps({"ts": self._old_iso(0), "tool": "current"}) + "\n")
        EventLog(dir=tmp_path, max_files=0, max_age_days=30)
        assert seg.exists()                     # recent_denial survives

    def test_an_unlistable_directory_does_not_restart_numbering(self, tmp_path, monkeypatch):
        # Confusing "cannot list" with "empty" would write new records into an old
        # segment and invert the order readers see.
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        newest = log.path
        import pathlib as _pl
        monkeypatch.setattr(_pl.Path, "glob",
                            lambda self, pat: (_ for _ in ()).throw(OSError("denied")))
        log._resume_current_segment()
        assert log._current is None             # unset, not reset to segment 1
        monkeypatch.undo()
        log._resume_current_segment()
        assert log._current == newest           # recovers once listing works

    def test_a_roll_never_lands_on_an_existing_segment(self, tmp_path):
        log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
        for i in range(20):
            log.record(_event(tool=f"t{i}"))
        existing = sorted(tmp_path.glob("events-*.jsonl"))
        log._current = existing[0]              # as a listing hiccup could leave it
        log._size = existing[0].stat().st_size
        assert log._next_segment() not in existing

    def test_newest_event_time_of_an_unreadable_segment(self, tmp_path, monkeypatch):
        seg = tmp_path / "events-000001.jsonl"
        seg.write_text(json.dumps({"ts": self._old_iso(1), "tool": "t"}) + "\n")
        log = EventLog(dir=tmp_path)
        real_open = open
        monkeypatch.setattr("builtins.open",
                            lambda p, *a, **k: (_ for _ in ()).throw(OSError("busy"))
                            if str(p).endswith("000001.jsonl") else real_open(p, *a, **k))
        assert log._newest_event_time(seg) is None


def test_resume_survives_a_stat_failure_on_the_newest_segment(tmp_path, monkeypatch):
    # Listing works but stat does not: leave _current unset rather than guessing.
    log = EventLog(dir=tmp_path, max_bytes=200, max_files=0)
    log.record(_event(tool="t"))
    import pathlib as _pl
    monkeypatch.setattr(_pl.Path, "exists",
                        lambda self: (_ for _ in ()).throw(OSError("denied")))
    log._resume_current_segment()
    assert log._current is None
