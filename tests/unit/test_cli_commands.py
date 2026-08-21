"""Coverage for the CLI commands and helpers in mcp_harbour.main."""
import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import mcp_harbour.main as m
from mcp_harbour.main import app, _format_uptime, _status_markup

runner = CliRunner()


@pytest.fixture
def cli(config_manager, monkeypatch):
    """CliRunner bound to a temp-config CLI, with the daemon reported down by
    default (no network) so command output is deterministic."""
    monkeypatch.setattr(m, "config_manager", config_manager)
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    return config_manager


# ─── Pure helpers ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, "-"), (0, "0s"), (5, "5s"), (59, "59s"), (60, "1m 0s"),
     (125, "2m 5s"), (3600, "1h 0m"), (3661, "1h 1m"), (86400, "1d 0h"), (90061, "1d 1h")],
)
def test_format_uptime(seconds, expected):
    assert _format_uptime(seconds) == expected


@pytest.mark.parametrize(
    "state,needle",
    [("running", "running"), ("failed", "failed"), ("stopped", "stopped"), ("weird", "unknown")],
)
def test_status_markup(state, needle):
    assert needle in _status_markup(state)


# ─── _harbour_up ────────────────────────────────────────────────────


def _fake_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda s, *a: False
    return resp


def test_harbour_up_true_on_signature(monkeypatch):
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"service": "mcp-harbour"})):
        assert m._harbour_up("127.0.0.1", 4767) is True


def test_harbour_up_false_on_wrong_service(monkeypatch):
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"service": "something-else"})):
        assert m._harbour_up("127.0.0.1", 4767) is False


def test_harbour_up_false_on_error(monkeypatch):
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert m._harbour_up("127.0.0.1", 4767) is False


# ─── _daemon_server_status ──────────────────────────────────────────


def test_daemon_server_status_none_when_down(monkeypatch):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    assert m._daemon_server_status() is None


def test_daemon_server_status_returns_dict_when_up(monkeypatch):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    monkeypatch.setattr("mcp_harbour.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"srv": {"state": "running"}})):
        assert m._daemon_server_status() == {"srv": {"state": "running"}}


def test_daemon_server_status_none_on_error(monkeypatch):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    monkeypatch.setattr("mcp_harbour.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert m._daemon_server_status() is None


# ─── _notify_daemon_reconcile ───────────────────────────────────────


def test_notify_reconcile_noop_when_down(monkeypatch, capsys):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    m._notify_daemon_reconcile()
    assert "not running" in capsys.readouterr().out


def test_notify_reconcile_reports_started_stopped(monkeypatch, capsys):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    monkeypatch.setattr("mcp_harbour.config.get_or_create_control_token", lambda: "tok")
    payload = {"started": ["a"], "stopped": ["b"], "failed": ["c"]}
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        m._notify_daemon_reconcile()
    out = capsys.readouterr().out
    assert "a" in out and "b" in out and "c" in out


def test_notify_reconcile_reports_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    monkeypatch.setattr("mcp_harbour.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", side_effect=OSError("nope")):
        m._notify_daemon_reconcile()
    assert "Could not reach the daemon" in capsys.readouterr().out


# ─── dock / undock ──────────────────────────────────────────────────


def test_dock_adds_server_and_notifies(cli, monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr(m, "_notify_daemon_reconcile", notify)
    result = runner.invoke(app, ["dock", "--name", "fs", "--command", "echo hi"])
    assert result.exit_code == 0
    assert "docked" in result.output
    assert cli.get_server("fs") is not None
    notify.assert_called_once()


def test_dock_invalid_shows_error(cli):
    # Neither --command nor --url -> add_server raises -> _handle prints + Exit(1).
    result = runner.invoke(app, ["dock", "--name", "bad"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_undock_removes_server(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, ["undock", "fs"])
    assert result.exit_code == 0
    assert cli.get_server("fs") is None


# ─── identity ───────────────────────────────────────────────────────


def test_identity_create_shows_key(cli):
    result = runner.invoke(app, ["identity", "create", "agent"])
    assert result.exit_code == 0
    assert "harbour_sk_" in result.output
    assert "agent" in cli.config.identities


def test_identity_create_duplicate_errors(cli):
    cli.add_identity("agent")
    result = runner.invoke(app, ["identity", "create", "agent"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_identity_list_empty(cli):
    result = runner.invoke(app, ["identity", "list"])
    assert "No identities" in result.output


def test_identity_list_populated(cli):
    cli.add_identity("agent")
    result = runner.invoke(app, ["identity", "list"])
    assert "agent" in result.output


def test_identity_delete(cli):
    cli.add_identity("agent")
    result = runner.invoke(app, ["identity", "delete", "agent"])
    assert result.exit_code == 0
    assert "agent" not in cli.config.identities


# ─── permit ─────────────────────────────────────────────────────────


def test_permit_allow_warns_when_server_not_docked(cli):
    cli.add_identity("agent")
    result = runner.invoke(app, ["permit", "allow", "agent", "ghost"])
    assert "not currently docked" in result.output
    assert "Permission granted" in result.output


def test_permit_allow_docked_server(cli):
    cli.add_identity("agent")
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["permit", "allow", "agent", "fs", "--tool", "read_*",
                                 "--args", "path=/home/**"])
    assert result.exit_code == 0
    assert "Permission granted" in result.output


def test_permit_show_no_policy(cli):
    result = runner.invoke(app, ["permit", "show", "nobody"])
    assert "No policy found" in result.output


def test_permit_show_with_regex_and_glob(cli):
    cli.add_identity("agent")
    cli.add_server("db", command="echo")
    cli.grant_permission("agent", "db", tool="query", arg_policies=["sql=re:^SELECT", "env=prod"])
    result = runner.invoke(app, ["permit", "show", "agent"])
    assert "db" in result.output
    assert "query" in result.output
    assert "re:" in result.output  # regex prefix rendered


# ─── list / inspect ─────────────────────────────────────────────────


def test_list_no_servers(cli):
    result = runner.invoke(app, ["list"])
    assert "No servers docked" in result.output


def test_list_daemon_down(cli):
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["list"])
    assert "fs" in result.output
    assert "status unavailable" in result.output


def test_list_daemon_up_running(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 5,
                                        "tools": [{"name": "t", "description": "d"}]}})
    result = runner.invoke(app, ["list"])
    assert "running" in result.output


def test_inspect_not_found(cli):
    result = runner.invoke(app, ["inspect", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_inspect_daemon_down(cli):
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["inspect", "fs"])
    assert "fs" in result.output
    assert "daemon not running" in result.output


def test_inspect_running_with_tools(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 12,
                                        "error": None,
                                        "tools": [{"name": "read", "description": "Read a file"}]}})
    result = runner.invoke(app, ["inspect", "fs"])
    assert "running" in result.output
    assert "read" in result.output


def test_inspect_running_no_tools(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 1, "error": None, "tools": []}})
    result = runner.invoke(app, ["inspect", "fs"])
    assert "no tools" in result.output.lower()


def test_inspect_unknown_status(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status", lambda: {})  # daemon up but server absent
    result = runner.invoke(app, ["inspect", "fs"])
    assert "unknown" in result.output.lower()


# ─── start / stop / status (platform branches) ──────────────────────


def test_start_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    result = runner.invoke(app, ["start"])
    assert "started" in result.output.lower()
    assert run.called


def test_start_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("subprocess.run", MagicMock())
    assert "started" in runner.invoke(app, ["start"]).output.lower()


def test_start_win32_up(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    assert "started" in runner.invoke(app, ["start"]).output.lower()


def test_start_win32_never_comes_up(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1


def test_start_win32_schtasks_fails(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run",
                        MagicMock(return_value=MagicMock(returncode=1, stderr="boom")))
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1


def test_start_unsupported(monkeypatch):
    monkeypatch.setattr("sys.platform", "sunos")
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 1
    assert "Unsupported" in result.output


def test_stop_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("subprocess.run", MagicMock())
    assert "stopped" in runner.invoke(app, ["stop"]).output.lower()


def test_stop_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("subprocess.run", MagicMock())
    assert "stopped" in runner.invoke(app, ["stop"]).output.lower()


def test_stop_win32_goes_down(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    assert "stopped" in runner.invoke(app, ["stop"]).output.lower()


def test_stop_win32_still_listening(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    assert runner.invoke(app, ["stop"]).exit_code == 1


def test_stop_unsupported(monkeypatch):
    monkeypatch.setattr("sys.platform", "sunos")
    assert runner.invoke(app, ["stop"]).exit_code == 1


def test_status_linux_active(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(stdout="active\n")))
    assert "running" in runner.invoke(app, ["status"]).output.lower()


def test_status_linux_inactive(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(stdout="inactive\n")))
    assert "inactive" in runner.invoke(app, ["status"]).output.lower()


def test_status_darwin_running(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))
    assert "running" in runner.invoke(app, ["status"]).output.lower()


def test_status_darwin_not_running(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=1)))
    assert "not running" in runner.invoke(app, ["status"]).output.lower()


def test_status_win32_up(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: True)
    assert "running" in runner.invoke(app, ["status"]).output.lower()


def test_status_win32_down(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(m, "_harbour_up", lambda *a, **k: False)
    assert "not running" in runner.invoke(app, ["status"]).output.lower()


def test_status_unsupported(monkeypatch):
    monkeypatch.setattr("sys.platform", "sunos")
    assert runner.invoke(app, ["status"]).exit_code == 1


# ─── serve ──────────────────────────────────────────────────────────


def test_serve_constructs_and_runs(monkeypatch):
    gw = MagicMock()
    monkeypatch.setattr("mcp_harbour.gateway.HarbourGateway", lambda: gw)
    ran = {}
    monkeypatch.setattr("asyncio.run", lambda coro: ran.setdefault("called", True))
    result = runner.invoke(app, ["serve", "--port", "5001"])
    assert result.exit_code == 0
    assert ran.get("called")


# ─── remaining branches ─────────────────────────────────────────────


def test_root_callback_branches():
    # completion (resilient parsing) and skip-list commands register nothing.
    for resilient, sub in [(True, "list"), (False, "version"), (False, "serve")]:
        ctx = MagicMock(resilient_parsing=resilient, invoked_subcommand=sub)
        m._root(ctx)
        ctx.call_on_close.assert_not_called()
    # an interactive command registers the update hint.
    ctx = MagicMock(resilient_parsing=False, invoked_subcommand="list")
    m._root(ctx)
    ctx.call_on_close.assert_called_once_with(m._maybe_notify_update)


def test_notify_update_falls_back_to_cache_on_fetch_error(tmp_path, monkeypatch):
    from mcp_harbour import config
    monkeypatch.delenv("MCP_HARBOUR_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "update-check.json").write_text(json.dumps({"checked_at": 0, "latest": "9.9.9"}))
    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag",
                        MagicMock(side_effect=OSError("offline")))
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)
    m._maybe_notify_update()
    assert mock_console.print.called  # hints from the cached latest despite the fetch error


def test_notify_update_cache_write_failure_is_silent(tmp_path, monkeypatch):
    from mcp_harbour import config
    monkeypatch.delenv("MCP_HARBOUR_NO_UPDATE_CHECK", raising=False)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")  # CONFIG_DIR is a file -> cache read + write both fail, swallowed
    monkeypatch.setattr(config, "CONFIG_DIR", not_a_dir)
    monkeypatch.setattr("mcp_harbour.updater.fetch_latest_tag", lambda timeout=2.0: "v9.9.9")
    monkeypatch.setattr(m, "err_console", MagicMock())
    m._maybe_notify_update()  # must not raise


def test_update_check_up_to_date(monkeypatch):
    from mcp_harbour.updater import ReleaseAsset, ReleaseInfo
    info = ReleaseInfo(tag="v0.0.1", asset=ReleaseAsset("a", "b"), update_available=False)
    monkeypatch.setattr("mcp_harbour.main.update_binary", MagicMock(return_value=info))
    result = runner.invoke(app, ["update", "--check"])
    assert "up to date" in result.output.lower()


def test_inspect_url_server(cli):
    cli.add_server("api", url="http://localhost:9000/mcp")
    result = runner.invoke(app, ["inspect", "api"])
    assert "http://localhost:9000/mcp" in result.output


def test_inspect_failed_server_shows_error(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "failed", "uptime_seconds": None,
                                        "error": "connection refused", "tools": []}})
    result = runner.invoke(app, ["inspect", "fs"])
    assert "connection refused" in result.output
