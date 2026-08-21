from unittest.mock import patch

from typer.testing import CliRunner

from mcp_harbour.main import app
from mcp_harbour.updater import ReleaseAsset, ReleaseInfo, UpdateError

runner = CliRunner()


def test_version_command():
    from mcp_harbour import __version__

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_package_version_matches_distribution_metadata():
    # __init__.py is the single source; pyproject derives from it. Guard the two
    # against drifting apart on a future bump.
    import importlib.metadata

    from mcp_harbour import __version__

    assert importlib.metadata.version("mcp-harbour") == __version__


def test_update_check_reports_available_update():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("mcp-harbour-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("mcp_harbour.main.update_binary", return_value=info) as update:
        result = runner.invoke(app, ["update", "--check"])

    assert result.exit_code == 0
    assert "Update available" in result.output
    update.assert_called_once_with(tag=None, check_only=True, force=False)


def test_update_reports_up_to_date():
    info = ReleaseInfo(
        tag="v0.1.1",
        asset=ReleaseAsset("mcp-harbour-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=False,
    )

    with patch("mcp_harbour.main.update_binary", return_value=info) as update:
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "already up to date" in result.output
    update.assert_called_once_with(tag=None, check_only=True, force=False)


def test_update_installs_after_confirmation():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("mcp-harbour-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("mcp_harbour.main.update_binary", return_value=info) as check, \
         patch("mcp_harbour.main.run_update_installer") as installer:
        result = runner.invoke(app, ["update"], input="y\n")

    assert result.exit_code == 0
    assert "Updated Harbour to v0.1.2" in result.output
    check.assert_called_once_with(tag=None, check_only=True, force=False)
    installer.assert_called_once_with("v0.1.2")


def test_update_yes_skips_confirmation():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("mcp-harbour-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("mcp_harbour.main.update_binary", return_value=info), \
         patch("mcp_harbour.main.run_update_installer") as installer:
        result = runner.invoke(app, ["update", "--yes"])

    assert result.exit_code == 0
    installer.assert_called_once_with("v0.1.2")


def test_update_reports_errors():
    with patch("mcp_harbour.main.update_binary", side_effect=UpdateError("not a release binary")):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "not a release binary" in result.output


def test_update_handles_installer_failure():
    info = ReleaseInfo(
        tag="v0.1.2",
        asset=ReleaseAsset("mcp-harbour-linux-x64.tar.gz", "https://example.com/release.tar.gz"),
        update_available=True,
    )

    with patch("mcp_harbour.main.update_binary", return_value=info), \
         patch("mcp_harbour.main.run_update_installer", side_effect=UpdateError("installer exploded")):
        result = runner.invoke(app, ["update", "--yes"])

    assert result.exit_code == 1
    assert "installer exploded" in result.output


# ─── Update-available hint ──────────────────────────────────────────


def test_update_hint_shown_when_newer_available(tmp_path, monkeypatch):
    import mcp_harbour.main as m
    from mcp_harbour import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("MCP_HARBOUR_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag", lambda timeout=2.0: "v9.9.9")
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert mock_console.print.called
    assert "9.9.9" in mock_console.print.call_args[0][0]
    assert __version__ in mock_console.print.call_args[0][0]


def test_no_hint_when_up_to_date(tmp_path, monkeypatch):
    import mcp_harbour.main as m
    from mcp_harbour import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("MCP_HARBOUR_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag", lambda timeout=2.0: f"v{__version__}")
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert not mock_console.print.called


def test_update_check_is_throttled(tmp_path, monkeypatch):
    import json
    import time
    import mcp_harbour.main as m
    from mcp_harbour import __version__, config
    from unittest.mock import MagicMock

    monkeypatch.delenv("MCP_HARBOUR_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "update-check.json").write_text(
        json.dumps({"checked_at": time.time(), "latest": __version__})
    )

    def _boom(timeout=2.0):
        raise AssertionError("network check must not run within the interval")

    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag", _boom)
    monkeypatch.setattr(m, "err_console", MagicMock())

    m._maybe_notify_update()  # reads the fresh cache; no network call


def test_update_check_disabled_by_env(tmp_path, monkeypatch):
    import mcp_harbour.main as m
    from mcp_harbour import config
    from unittest.mock import MagicMock

    monkeypatch.setenv("MCP_HARBOUR_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    def _boom(timeout=2.0):
        raise AssertionError("check must not run when disabled")

    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag", _boom)
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)

    m._maybe_notify_update()

    assert not mock_console.print.called
