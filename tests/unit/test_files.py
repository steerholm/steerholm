import os
from unittest import mock

import pytest

from steerholm import files


def test_write_atomic_replaces_the_file(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    files.write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_write_atomic_writes_utf8(tmp_path):
    target = tmp_path / "policy.json"
    files.write_atomic(target, '{"path": "/srv/café"}')
    assert target.read_bytes().decode("utf-8") == '{"path": "/srv/café"}'


def test_a_failed_write_keeps_the_old_file_and_no_temp(tmp_path):
    # A full disk fails DURING the write: the old contents must survive it.
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    with mock.patch.object(files, "_flush_to_disk", side_effect=OSError(28, "No space left on device")):
        with pytest.raises(OSError):
            files.write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows uses ACLs")
def test_write_atomic_is_owner_only(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("old")
    target.chmod(0o644)
    files.write_atomic(target, "new")
    assert target.stat().st_mode & 0o777 == 0o600


def test_restrict_ignores_a_chmod_failure(tmp_path):
    with mock.patch.object(files.os, "chmod", side_effect=OSError("read-only")):
        files.restrict(tmp_path / "missing", 0o600)


def test_fsync_dir_ignores_a_directory_it_cannot_open(tmp_path):
    files.fsync_dir(tmp_path / "missing")


@pytest.mark.skipif(os.name == "nt", reason="directories cannot be opened on Windows")
def test_fsync_dir_ignores_an_fsync_failure(tmp_path):
    with mock.patch.object(files, "_flush_to_disk", side_effect=OSError("EINVAL")):
        files.fsync_dir(tmp_path)


def test_full_fsync_is_used_where_the_os_has_it():
    fake = mock.Mock(F_FULLFSYNC=51)
    with mock.patch.object(files, "fcntl", fake), mock.patch.object(files.os, "fsync") as fsync:
        files._flush_to_disk(7)
    fake.fcntl.assert_called_once_with(7, 51)
    fsync.assert_not_called()


def test_full_fsync_falls_back_to_fsync_when_refused():
    fake = mock.Mock(F_FULLFSYNC=51)
    fake.fcntl.side_effect = OSError("ENOTSUP")
    with mock.patch.object(files, "fcntl", fake), mock.patch.object(files.os, "fsync") as fsync:
        files._flush_to_disk(7)
    fsync.assert_called_once_with(7)


def test_fsync_is_used_without_full_fsync():
    with mock.patch.object(files, "fcntl", None), mock.patch.object(files.os, "fsync") as fsync:
        files._flush_to_disk(7)
    fsync.assert_called_once_with(7)


def test_read_text_reads_utf8(tmp_path):
    target = tmp_path / "policy.json"
    target.write_bytes("café".encode("utf-8"))
    assert files.read_text(target) == "café"


def test_read_text_falls_back_to_the_system_encoding(tmp_path):
    # 0.1.2 on Windows wrote state in the system's encoding (cp1252 there).
    target = tmp_path / "policy.json"
    target.write_bytes("café".encode("cp1252"))
    real = files.Path.read_text

    def read(self, encoding=None):
        return real(self, encoding="cp1252" if encoding == "locale" else encoding)

    with mock.patch.object(files.Path, "read_text", read):
        assert files.read_text(target) == "café"


@pytest.mark.skipif(os.name == "nt", reason="the system encoding is UTF-8 only off Windows")
def test_read_text_still_refuses_undecodable_bytes(tmp_path):
    target = tmp_path / "policy.json"
    target.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(UnicodeDecodeError):
        files.read_text(target)


def test_replace_retries_while_windows_holds_the_file_open(tmp_path):
    with mock.patch.object(files.os, "name", "nt"), \
         mock.patch.object(files.os, "replace", side_effect=[PermissionError, PermissionError, None]) as replace, \
         mock.patch.object(files.time, "sleep") as sleep:
        files._replace(tmp_path / "a", tmp_path / "b")
    assert replace.call_count == 3
    assert sleep.call_count == 2


def test_replace_gives_up_after_its_attempts(tmp_path):
    with mock.patch.object(files.os, "name", "nt"), \
         mock.patch.object(files.os, "replace", side_effect=PermissionError) as replace, \
         mock.patch.object(files.time, "sleep"):
        with pytest.raises(PermissionError):
            files._replace(tmp_path / "a", tmp_path / "b")
    assert replace.call_count == files._REPLACE_ATTEMPTS


def test_replace_does_not_retry_off_windows(tmp_path):
    with mock.patch.object(files.os, "name", "posix"), \
         mock.patch.object(files.os, "replace", side_effect=PermissionError) as replace:
        with pytest.raises(PermissionError):
            files._replace(tmp_path / "a", tmp_path / "b")
    assert replace.call_count == 1
