"""The environment handed to every process Steerholm starts."""
import sys

from steerholm.childenv import child_env

BUNDLED = "/tmp/_MEI12345"


def _frozen(monkeypatch, ld_library_path, original=None):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", ld_library_path)
    if original is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)


def test_a_frozen_build_hands_on_the_users_own_value(monkeypatch):
    _frozen(monkeypatch, f"{BUNDLED}:/opt/lib", original="/opt/lib")
    env = child_env()
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_a_frozen_build_drops_it_when_the_user_had_none(monkeypatch):
    # PyInstaller leaves LD_LIBRARY_PATH_ORIG unset when there was nothing to save.
    _frozen(monkeypatch, BUNDLED)
    assert "LD_LIBRARY_PATH" not in child_env()


def test_a_frozen_build_drops_pyinstallers_bookkeeping(monkeypatch):
    # With it, a different frozen build started as a child reuses OUR unpacked
    # libraries instead of its own.
    _frozen(monkeypatch, BUNDLED)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", BUNDLED)
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/home/u/.local/bin/holm")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    assert not [k for k in child_env() if k.startswith("_PYI_")]


def test_from_source_the_users_value_is_left_alone(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert child_env()["LD_LIBRARY_PATH"] == "/opt/lib"


def test_extra_is_added_and_wins(monkeypatch):
    monkeypatch.setenv("STEERVAL_X", "inherited")
    env = child_env({"STEERVAL_X": "given", "STEERVAL_Y": "1"})
    assert env["STEERVAL_X"] == "given" and env["STEERVAL_Y"] == "1"
