import os
import sys
from unittest.mock import patch

import pytest

from typer.testing import CliRunner

from steerholm.main import app
from steerholm.updater import ReleaseAsset, ReleaseInfo, UpdateError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _exit_without_ending_the_test_run(monkeypatch):
    # `holm update` leaves with os._exit once the installer has run.
    def exit_(code):
        raise SystemExit(code)
    monkeypatch.setattr(os, "_exit", exit_)


def test_version_command():
    from steerholm import __version__

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_package_version_matches_distribution_metadata():
    # __init__.py is the single source; pyproject derives from it. Guard the two
    # against drifting apart on a future bump.
    # Compared as versions, not strings: the metadata is PEP 440-normalised, so
    # a pre-release written 0.2.0-rc.1 is recorded as 0.2.0rc1.
    import importlib.metadata

    from packaging.version import Version

    from steerholm import __version__

    assert Version(importlib.metadata.version("steerholm")) == Version(__version__)


def test_update_check_reports_available_update():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("steerholm.main.update_binary", return_value=info) as update:
        result = runner.invoke(app, ["update", "--check"])

    assert result.exit_code == 0
    assert "Update available" in result.output
    update.assert_called_once_with(tag=None, check_only=True, force=False)


def test_update_reports_up_to_date():
    info = ReleaseInfo(
        tag="v0.1.1",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=False,
    )

    with patch("steerholm.main.update_binary", return_value=info) as update:
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "already up to date" in result.output
    update.assert_called_once_with(tag=None, check_only=True, force=False)


def test_update_installs_after_confirmation():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("steerholm.main.update_binary", return_value=info) as check, \
         patch("steerholm.main.run_update_installer") as installer:
        result = runner.invoke(app, ["update"], input="y\n")

    assert result.exit_code == 0
    assert "Updated Steerholm to v0.1.2" in result.output
    check.assert_called_once_with(tag=None, check_only=True, force=False)
    installer.assert_called_once_with("v0.1.2")


def test_update_yes_skips_confirmation():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("steerholm.main.update_binary", return_value=info), \
         patch("steerholm.main.run_update_installer") as installer:
        result = runner.invoke(app, ["update", "--yes"])

    assert result.exit_code == 0
    installer.assert_called_once_with("v0.1.2")


def test_update_reports_errors():
    with patch("steerholm.main.update_binary", side_effect=UpdateError("not a release binary")):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "not a release binary" in result.output


def test_update_handles_installer_failure():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("steerholm.main.update_binary", return_value=info), \
         patch("steerholm.main.run_update_installer", side_effect=UpdateError("installer exploded")):
        result = runner.invoke(app, ["update", "--yes"])

    assert result.exit_code == 1
    assert "installer exploded" in result.output


class _RefuseImports:
    """Fails any import of a module not loaded yet — what a frozen build does
    once its file has been replaced underneath it."""

    def find_spec(self, name, path=None, target=None):
        raise AssertionError(f"imported {name} after the installer replaced the binary")


@pytest.mark.parametrize("outcome", ["installed", "failed"])
def test_update_loads_nothing_once_the_installer_has_run(monkeypatch, outcome):
    # A successful update ended in a zlib.error traceback and exit code 1: the
    # final message's markup imported a module from the replaced binary.
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("steerholm-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )
    refuse = _RefuseImports()

    def installer(tag):
        sys.meta_path.insert(0, refuse)
        if outcome == "failed":
            raise UpdateError("installer exploded")

    def exit_(code):
        sys.meta_path.remove(refuse)     # reached without importing anything
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", exit_)
    try:
        with patch("steerholm.main.update_binary", return_value=info), \
             patch("steerholm.main.run_update_installer", side_effect=installer):
            result = runner.invoke(app, ["update", "--yes"])
    finally:
        if refuse in sys.meta_path:
            sys.meta_path.remove(refuse)

    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    if outcome == "installed":
        assert result.exit_code == 0
        assert "Updated Steerholm to v0.1.2." in result.output
    else:
        assert result.exit_code == 1
        assert "installer exploded" in result.output


# ─── Update-available hint ──────────────────────────────────────────


def test_update_hint_shown_when_newer_available(tmp_path, monkeypatch):
    import steerholm.main as m
    from steerholm import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("STEERHOLM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("steerholm.updater.fetch_latest_tag", lambda timeout=2.0: "v9.9.9")
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert mock_console.print.called
    assert "9.9.9" in mock_console.print.call_args[0][0]
    assert __version__ in mock_console.print.call_args[0][0]


def test_no_hint_when_up_to_date(tmp_path, monkeypatch):
    import steerholm.main as m
    from steerholm import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("STEERHOLM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("steerholm.updater.fetch_latest_tag", lambda timeout=2.0: f"v{__version__}")
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert not mock_console.print.called


def test_update_check_is_throttled(tmp_path, monkeypatch):
    import json
    import time
    import steerholm.main as m
    from steerholm import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("STEERHOLM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "update-check.json").write_text(
        json.dumps({"checked_at": time.time(), "latest": __version__})
    )

    def _boom(timeout=2.0):
        raise AssertionError("network check must not run within the interval")

    monkeypatch.setattr("steerholm.updater.fetch_latest_tag", _boom)
    monkeypatch.setattr(m, "err_console", MagicMock())

    m._maybe_notify_update()  # reads the fresh cache; no network call


def test_update_check_disabled_by_env(tmp_path, monkeypatch):
    import steerholm.main as m
    from steerholm import config
    from unittest.mock import MagicMock

    monkeypatch.setenv("STEERHOLM_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    def _boom(timeout=2.0):
        raise AssertionError("check must not run when disabled")

    monkeypatch.setattr("steerholm.updater.fetch_latest_tag", _boom)
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert not mock_console.print.called
