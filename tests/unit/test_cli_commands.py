"""Coverage for the CLI commands and helpers in steerholm.main."""
import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import steerholm.main as m
from steerholm.main import app, _format_uptime, _status_markup

runner = CliRunner()


@pytest.fixture
def cli(config_manager, monkeypatch):
    """CliRunner bound to a temp-config CLI, with the daemon reported down by
    default (no network) so command output is deterministic."""
    monkeypatch.setattr(m, "config_manager", config_manager)
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
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


# ─── _daemon_up ────────────────────────────────────────────────────


def _fake_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda s, *a: False
    return resp


def test_daemon_up_true_on_signature(monkeypatch):
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"service": "steerholm"})):
        assert m._daemon_up("127.0.0.1", 4767) is True


def test_daemon_up_false_on_wrong_service(monkeypatch):
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"service": "something-else"})):
        assert m._daemon_up("127.0.0.1", 4767) is False


def test_daemon_up_false_on_error(monkeypatch):
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert m._daemon_up("127.0.0.1", 4767) is False


# ─── _daemon_server_status ──────────────────────────────────────────


def test_daemon_server_status_none_when_down(monkeypatch):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
    assert m._daemon_server_status() is None


def test_daemon_server_status_returns_dict_when_up(monkeypatch):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    monkeypatch.setattr("steerholm.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", return_value=_fake_urlopen({"srv": {"state": "running"}})):
        assert m._daemon_server_status() == {"srv": {"state": "running"}}


def test_daemon_server_status_none_on_error(monkeypatch):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    monkeypatch.setattr("steerholm.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert m._daemon_server_status() is None


# ─── _notify_daemon_reconcile ───────────────────────────────────────


def test_notify_reconcile_noop_when_down(monkeypatch, capsys):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
    m._notify_daemon_reconcile()
    assert "not running" in capsys.readouterr().out


def test_notify_reconcile_reports_started_stopped(monkeypatch, capsys):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    monkeypatch.setattr("steerholm.config.get_or_create_control_token", lambda: "tok")
    payload = {"started": ["a"], "stopped": ["b"], "failed": ["c"]}
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        m._notify_daemon_reconcile()
    out = capsys.readouterr().out
    assert "a" in out and "b" in out and "c" in out


def test_notify_reconcile_reports_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    monkeypatch.setattr("steerholm.config.get_or_create_control_token", lambda: "tok")
    with patch("urllib.request.urlopen", side_effect=OSError("nope")):
        m._notify_daemon_reconcile()
    assert "Could not reach the daemon" in capsys.readouterr().out


# ─── add / remove server ────────────────────────────────────────────


def test_add_server_and_notifies(cli, monkeypatch):
    notify = MagicMock()
    monkeypatch.setattr(m, "_notify_daemon_reconcile", notify)
    result = runner.invoke(app, ["add", "server", "fs", "--command", "echo hi"])
    assert result.exit_code == 0
    assert "Added server" in result.output
    assert cli.get_server("fs") is not None
    notify.assert_called_once()


def test_add_server_invalid_shows_error(cli):
    # Neither --command nor --url -> add_server raises -> _handle prints + Exit(1).
    result = runner.invoke(app, ["add", "server", "bad"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_add_server_with_env(cli, monkeypatch):
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, [
        "add", "server", "db", "--command", "uvx x",
        "--env", "DATABASE_URI=postgresql://u:p@h/db", "--env", "LOG=debug",
    ])
    assert result.exit_code == 0
    assert cli.get_server("db").env == {
        "DATABASE_URI": "postgresql://u:p@h/db", "LOG": "debug"
    }


def test_add_server_env_bad_format_errors(cli):
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "NOEQUALS"])
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output


def test_show_server_masks_env_values(cli):
    cli.add_server("db", command="uvx x", env={"DATABASE_URI": "secret://token"})
    result = runner.invoke(app, ["show", "server", "db"])
    assert result.exit_code == 0
    assert "DATABASE_URI" in result.output          # key is shown
    assert "secret://token" not in result.output    # value is masked
    assert "***" in result.output


def test_add_server_env_value_with_equals(cli, monkeypatch):
    # The '=' in the value must survive — parsing splits on the FIRST '=' only.
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    dsn = "postgresql://u:p@h/db?sslmode=require&opt=1"
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", f"DSN={dsn}"])
    assert result.exit_code == 0
    assert cli.get_server("db").env == {"DSN": dsn}


def test_add_server_env_empty_value_ok(cli, monkeypatch):
    # 'KEY=' is a deliberate set-to-empty, not a format error.
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "FLAG="])
    assert result.exit_code == 0
    assert cli.get_server("db").env == {"FLAG": ""}


def test_add_server_env_empty_key_errors(cli):
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "=value"])
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output


def test_add_server_env_duplicate_key_errors(cli):
    result = runner.invoke(app, [
        "add", "server", "db", "--command", "uvx x", "--env", "K=1", "--env", "K=2",
    ])
    assert result.exit_code == 1
    assert "more than once" in result.output


def test_add_server_env_key_whitespace_trimmed(cli, monkeypatch):
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "  K  =v"])
    assert result.exit_code == 0
    assert cli.get_server("db").env == {"K": "v"}


def test_add_server_env_rejected_with_url_via_cli(cli):
    result = runner.invoke(app, ["add", "server", "bad", "--url", "http://x", "--env", "K=v"])
    assert result.exit_code == 1
    assert "stdio" in result.output


def test_show_server_command_shown_literally_not_as_markup(cli):
    # Without escaping, Rich consumes "[core]" as a (dropped) markup tag and would
    # display just "serve"; the command must render literally. (Env keys can't hold
    # brackets after validation, so the unvalidated command is the realistic surface.)
    cli.add_server("db", command="serve [core]")
    result = runner.invoke(app, ["show", "server", "db"])
    assert result.exit_code == 0
    assert "serve [core]" in result.output           # shown intact, not mangled


def test_show_server_survives_malformed_markup_in_command(cli):
    # A malformed tag like "[/]" in a stored value raises MarkupError and would
    # abort `show server` if printed unescaped.
    cli.add_server("db", command="uvx [/]")
    result = runner.invoke(app, ["show", "server", "db"])
    assert result.exit_code == 0
    assert "uvx [/]" in result.output


def test_add_server_env_key_portable_name_allowed(cli, monkeypatch):
    # A portable name with '.' and '-' (valid under the K8s rule) goes through.
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, [
        "add", "server", "db", "--command", "uvx x", "--env", "my.env-name=1",
    ])
    assert result.exit_code == 0
    assert cli.get_server("db").env == {"my.env-name": "1"}


def test_add_server_env_key_internal_space_errors(cli):
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "FOO BAR=v"])
    assert result.exit_code == 1
    assert "Invalid environment variable name" in result.output


def test_add_server_env_key_leading_digit_errors(cli):
    # Notable K8s-rule consequence users may hit: a name can't start with a digit.
    result = runner.invoke(app, ["add", "server", "db", "--command", "uvx x", "--env", "2FA=x"])
    assert result.exit_code == 1
    assert "digit" in result.output


def test_remove_server(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_notify_daemon_reconcile", MagicMock())
    result = runner.invoke(app, ["remove", "server", "fs"])
    assert result.exit_code == 0
    assert "Removed server" in result.output
    assert cli.get_server("fs") is None


# ─── add / list / remove / rotate agent ─────────────────────────────


def test_add_agent_shows_key(cli):
    result = runner.invoke(app, ["add", "agent", "agent"])
    assert result.exit_code == 0
    assert "steer_sk_" in result.output
    assert "agent" in cli.config.agents


def test_add_agent_duplicate_errors(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["add", "agent", "agent"])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_list_agents_empty(cli):
    result = runner.invoke(app, ["list", "agents"])
    assert "No agents" in result.output


def test_list_agents_populated(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["list", "agents"])
    assert "agent" in result.output


def test_remove_agent(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["remove", "agent", "agent"])
    assert result.exit_code == 0
    assert "Removed agent" in result.output
    assert "agent" not in cli.config.agents


def test_rotate_agent_shows_new_key(cli):
    import re
    first = cli.add_agent("agent")
    result = runner.invoke(app, ["rotate", "agent", "agent"])
    assert result.exit_code == 0
    new_key = re.search(r"steer_sk_\w+", result.output).group(0)
    # compare full keys, not the 15-char display prefix (only 4 random chars)
    assert new_key != first


def test_rotate_agent_not_found_errors(cli):
    result = runner.invoke(app, ["rotate", "agent", "ghost"])
    assert result.exit_code == 1
    assert "Error" in result.output


# ─── grant / revoke / show agent ────────────────────────────────────


def test_grant_warns_when_server_not_added(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["grant", "agent", "ghost"])
    assert "not currently added" in result.output
    assert "Granted" in result.output


def test_grant_agent_not_found_errors_before_server_warning(cli):
    # error-ordering fix: an unknown agent errors out, and the server-not-added
    # warning is never printed.
    result = runner.invoke(app, ["grant", "nobody", "ghost"])
    assert result.exit_code == 1
    assert "Agent 'nobody' not found" in result.output
    assert "not currently added" not in result.output


def test_grant_added_server(cli):
    cli.add_agent("agent")
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["grant", "agent", "fs", "--tool", "read_*",
                                 "--args", "path=/home/**"])
    assert result.exit_code == 0
    assert "Granted" in result.output


def test_revoke_removes_grant(cli):
    cli.add_agent("agent")
    cli.add_server("fs", command="echo")
    cli.grant_permission("agent", "fs", tool="read_*")
    result = runner.invoke(app, ["revoke", "agent", "fs", "--tool", "read_*"])
    assert result.exit_code == 0
    assert "Revoked" in result.output


def test_revoke_nothing_to_revoke(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["revoke", "agent", "fs"])
    assert result.exit_code == 0
    assert "Nothing to revoke" in result.output


def test_revoke_agent_not_found_errors(cli):
    result = runner.invoke(app, ["revoke", "ghost", "fs"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_agent_no_grants(cli):
    cli.add_agent("agent")
    result = runner.invoke(app, ["show", "agent", "agent"])
    assert result.exit_code == 0
    assert "none granted" in result.output


def test_show_agent_not_found_errors(cli):
    result = runner.invoke(app, ["show", "agent", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_agent_with_regex_and_glob(cli):
    cli.add_agent("agent")
    cli.add_server("db", command="echo")
    cli.grant_permission("agent", "db", tool="query", arg_policies=["sql=re:^SELECT", "env=prod"])
    result = runner.invoke(app, ["show", "agent", "agent"])
    assert "db" in result.output
    assert "query" in result.output
    assert "re:" in result.output  # regex prefix rendered


def test_show_agent_arg_pattern_with_brackets_shown_literally(cli):
    # A regex arg policy like ^[a-z]+ contains '[...]'; without escaping Rich
    # consumes it as a (dropped) markup tag and renders "sql=re:^+". Common input.
    cli.add_agent("agent")
    cli.add_server("db", command="echo")
    cli.grant_permission("agent", "db", tool="query", arg_policies=["sql=re:^[a-z]+"])
    result = runner.invoke(app, ["show", "agent", "agent"])
    assert result.exit_code == 0
    assert "^[a-z]+" in result.output      # pattern shown intact, not mangled


# ─── list servers / show server ─────────────────────────────────────


def test_list_servers_empty(cli):
    result = runner.invoke(app, ["list", "servers"])
    assert "No servers added" in result.output


def test_list_servers_daemon_down(cli):
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["list", "servers"])
    assert "fs" in result.output
    assert "status unavailable" in result.output


def test_list_servers_command_with_brackets_shown_literally(cli):
    # The command cell renders as Rich markup; a '[' must show literally, not be
    # parsed as a (dropped) markup tag.
    cli.add_server("db", command="serve [core]")
    result = runner.invoke(app, ["list", "servers"])
    assert result.exit_code == 0
    assert "[core]" in result.output


def test_show_server_not_found_bracket_name_does_not_crash(cli):
    # The not-found error interpolates the name from argv; a malformed tag like
    # "[/]" must not abort the command with a Rich markup error.
    result = runner.invoke(app, ["show", "server", "[/]"])
    assert result.exit_code == 1          # not found -> clean exit, not a crash
    assert "not found" in result.output   # message printed (a crash would skip it)


def test_show_server_grantee_tool_pattern_brackets_shown_literally(cli):
    # The grantees table shows granted tool patterns; a glob class like [abc] must
    # render literally rather than be consumed as (dropped) Rich markup.
    cli.add_agent("agent")
    cli.add_server("db", command="echo")
    cli.grant_permission("agent", "db", tool="log_[abc]")
    result = runner.invoke(app, ["show", "server", "db"])
    assert result.exit_code == 0
    assert "log_[abc]" in result.output


def test_list_servers_daemon_up_running(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 5,
                                        "tools": [{"name": "t", "description": "d"}]}})
    result = runner.invoke(app, ["list", "servers"])
    assert "running" in result.output


def test_show_server_not_found(cli):
    result = runner.invoke(app, ["show", "server", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_server_daemon_down(cli):
    cli.add_server("fs", command="echo")
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "fs" in result.output
    assert "daemon not running" in result.output


def test_show_server_lists_grantees(cli):
    cli.add_agent("agent")
    cli.add_server("fs", command="echo")
    cli.grant_permission("agent", "fs", tool="read_*")
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "Agents with access" in result.output
    assert "agent" in result.output
    assert "read_*" in result.output


def test_show_server_running_with_tools(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 12,
                                        "error": None,
                                        "tools": [{"name": "read", "description": "Read a file"}]}})
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "running" in result.output
    assert "read" in result.output


def test_show_server_running_no_tools(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "running", "uptime_seconds": 1, "error": None, "tools": []}})
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "no tools" in result.output.lower()


def test_show_server_unknown_status(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status", lambda: {})  # daemon up but server absent
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "unknown" in result.output.lower()


def test_version_flag(cli):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert m.__version__ in result.output


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
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    assert "started" in runner.invoke(app, ["start"]).output.lower()


def test_start_win32_never_comes_up(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock(returncode=0)))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
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
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
    assert "stopped" in runner.invoke(app, ["stop"]).output.lower()


def test_stop_win32_still_listening(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("subprocess.run", MagicMock())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
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
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: True)
    assert "running" in runner.invoke(app, ["status"]).output.lower()


def test_status_win32_down(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(m, "_daemon_up", lambda *a, **k: False)
    assert "not running" in runner.invoke(app, ["status"]).output.lower()


def test_status_unsupported(monkeypatch):
    monkeypatch.setattr("sys.platform", "sunos")
    assert runner.invoke(app, ["status"]).exit_code == 1


# ─── serve ──────────────────────────────────────────────────────────


def test_serve_constructs_and_runs(monkeypatch):
    gw = MagicMock()
    monkeypatch.setattr("steerholm.gateway.SteerholmGateway", lambda: gw)
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
    from steerholm import config
    monkeypatch.delenv("STEERHOLM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "update-check.json").write_text(json.dumps({"checked_at": 0, "latest": "9.9.9"}))
    monkeypatch.setattr("steerholm.updater.fetch_latest_tag",
                        MagicMock(side_effect=OSError("offline")))
    mock_console = MagicMock()
    monkeypatch.setattr(m, "err_console", mock_console)
    m._maybe_notify_update()
    assert mock_console.print.called  # hints from the cached latest despite the fetch error


def test_notify_update_cache_write_failure_is_silent(tmp_path, monkeypatch):
    from steerholm import config
    monkeypatch.delenv("STEERHOLM_NO_UPDATE_CHECK", raising=False)
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")  # CONFIG_DIR is a file -> cache read + write both fail, swallowed
    monkeypatch.setattr(config, "CONFIG_DIR", not_a_dir)
    monkeypatch.setattr("steerholm.updater.fetch_latest_tag", lambda timeout=2.0: "v9.9.9")
    monkeypatch.setattr(m, "err_console", MagicMock())
    m._maybe_notify_update()  # must not raise


def test_update_check_up_to_date(monkeypatch):
    from steerholm.updater import ReleaseAsset, ReleaseInfo
    info = ReleaseInfo(tag="v0.0.1", asset=ReleaseAsset("a", "b"), update_available=False)
    monkeypatch.setattr("steerholm.main.update_binary", MagicMock(return_value=info))
    result = runner.invoke(app, ["update", "--check"])
    assert "up to date" in result.output.lower()


def test_show_server_url_server(cli):
    cli.add_server("api", url="http://localhost:9000/mcp")
    result = runner.invoke(app, ["show", "server", "api"])
    assert "http://localhost:9000/mcp" in result.output


def test_show_server_failed_shows_error(cli, monkeypatch):
    cli.add_server("fs", command="echo")
    monkeypatch.setattr(m, "_daemon_server_status",
                        lambda: {"fs": {"state": "failed", "uptime_seconds": None,
                                        "error": "connection refused", "tools": []}})
    result = runner.invoke(app, ["show", "server", "fs"])
    assert "connection refused" in result.output


# ─── audit log (holm log) ───────────────────────────────────────────

_TS = "2026-08-31T12:04:35.000+00:00"


def _write_events(config_dir, events):
    (config_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


def test_log_shows_events_from_the_file(cli, tmp_config_dir):
    # Works with the daemon down (cli fixture reports it down) — reads the file.
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "cursor", "server": "git", "tool": "git_log",
         "decision": "allowed", "reason": None},
        {"ts": _TS, "agent": "cursor", "server": "fs", "tool": "write_file",
         "decision": "denied", "reason": "not allowed"},
    ])
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    for needle in ("cursor", "git_log", "write_file", "allowed", "denied", "12:04:35"):
        assert needle in result.output


def test_log_no_file_reports_empty(cli):
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "No matching activity" in result.output


def test_log_filters_by_agent(cli, tmp_config_dir):
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": "t_a", "decision": "allowed", "server": "s"},
        {"ts": _TS, "agent": "b", "tool": "t_b", "decision": "allowed", "server": "s"},
    ])
    result = runner.invoke(app, ["log", "--agent", "a"])
    assert "t_a" in result.output
    assert "t_b" not in result.output


def test_log_filters_by_decision(cli, tmp_config_dir):
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": "t_ok", "decision": "allowed", "server": "s"},
        {"ts": _TS, "agent": "a", "tool": "t_no", "decision": "denied", "server": "s"},
    ])
    result = runner.invoke(app, ["log", "--decision", "denied"])
    assert "t_no" in result.output
    assert "t_ok" not in result.output


def test_log_limit_shows_most_recent(cli, tmp_config_dir):
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": f"t{i}", "decision": "allowed", "server": "s"}
        for i in range(5)
    ])
    result = runner.invoke(app, ["log", "-n", "2"])
    assert "t3" in result.output and "t4" in result.output
    assert "t0" not in result.output


def test_log_bracketed_reason_not_markup(cli, tmp_config_dir):
    # A regex reason like ^[a-z]+ must render literally, not mangle as Rich markup.
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "server": "db", "tool": "query",
         "decision": "denied", "reason": "sql=re:^[a-z]+"},
    ])
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "^[a-z]+" in result.output


def test_log_skips_blank_and_torn_lines(cli, tmp_config_dir):
    # A blank line and a torn final line (crash mid-write) are skipped, not fatal.
    (tmp_config_dir / "events.jsonl").write_text(
        json.dumps({"ts": _TS, "agent": "a", "tool": "good",
                    "decision": "allowed", "server": "s"}) + "\n"
        "\n"                        # blank line
        '{"ts": "2026-08-31T12:0'   # torn JSON, no newline/close
    )
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "good" in result.output


def test_log_skips_non_dict_lines(cli, tmp_config_dir):
    # A valid-JSON non-object line (tamper/corruption) is skipped, not fatal.
    (tmp_config_dir / "events.jsonl").write_text(
        "null\n42\n[1,2]\n"
        + json.dumps({"ts": _TS, "agent": "a", "tool": "good",
                      "decision": "allowed", "server": "s"}) + "\n"
    )
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "good" in result.output


def test_log_tolerates_invalid_utf8(cli, tmp_config_dir):
    # A torn multibyte char (crash mid-write) must not abort the whole read.
    path = tmp_config_dir / "events.jsonl"
    good = json.dumps({"ts": _TS, "agent": "a", "tool": "good",
                       "decision": "allowed", "server": "s"})
    path.write_bytes(good.encode() + b"\n" + b"\xff\xfe not utf-8\n")
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "good" in result.output


def test_log_tolerates_non_string_fields(cli, tmp_config_dir):
    # A corrupted dict with numeric ts/decision must render without crashing.
    (tmp_config_dir / "events.jsonl").write_text(
        json.dumps({"ts": 123, "agent": "a", "tool": "t", "decision": 7, "server": "s"}) + "\n"
    )
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0


def test_log_negative_limit_rejected(cli):
    result = runner.invoke(app, ["log", "-n", "-5"])
    assert result.exit_code != 0          # typer min=0 rejects it


def test_log_zero_limit_shows_all(cli, tmp_config_dir):
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": f"t{i}", "decision": "allowed", "server": "s"}
        for i in range(3)
    ])
    result = runner.invoke(app, ["log", "-n", "0"])
    assert result.exit_code == 0
    for i in range(3):
        assert f"t{i}" in result.output


def test_log_invalid_decision_rejected(cli):
    result = runner.invoke(app, ["log", "--decision", "denyed"])
    assert result.exit_code == 1
    assert "must be one of" in result.output


# ─── holm log --follow (tails the same file) ────────────────────────


def _stop_after(n):
    """A stop() predicate for _tail_event_log: allow n loop passes, then stop."""
    calls = {"n": 0}
    def stop():
        calls["n"] += 1
        return calls["n"] > n
    return stop


def test_tail_emits_events_appended_after_the_offset(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    path.write_text(json.dumps({"ts": _TS, "agent": "a", "tool": "old", "decision": "allowed"}) + "\n")
    offset = path.stat().st_size
    path.write_text(path.read_text()
                    + json.dumps({"ts": _TS, "agent": "a", "tool": "new", "decision": "denied"}) + "\n")

    seen = []
    m._tail_event_log(seen.append, offset=offset, poll=0, stop=_stop_after(3))
    assert [e["tool"] for e in seen] == ["new"]      # only what came after the offset



def test_tail_skips_blank_torn_and_non_dict_lines(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    path.write_text(
        "\n" "null\n" "{not json}\n"
        + json.dumps({"ts": _TS, "agent": "a", "tool": "good", "decision": "allowed"}) + "\n"
    )
    seen = []
    m._tail_event_log(seen.append, offset=0, poll=0, stop=_stop_after(3))
    assert [e["tool"] for e in seen] == ["good"]



def test_tail_reopens_after_truncation(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    path.write_text(json.dumps({"ts": _TS, "agent": "a", "tool": "first", "decision": "allowed"}) + "\n")
    # Start past the end, then truncate + rewrite (as rotation would).
    offset = path.stat().st_size + 500
    path.write_text(json.dumps({"ts": _TS, "agent": "a", "tool": "rotated", "decision": "allowed"}) + "\n")

    seen = []
    m._tail_event_log(seen.append, offset=offset, poll=0, stop=_stop_after(4))
    assert [e["tool"] for e in seen] == ["rotated"]



def test_read_event_history_returns_events_and_exact_offset(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": "h1", "decision": "allowed"},
        {"ts": _TS, "agent": "b", "tool": "h2", "decision": "allowed"},
    ])
    events, offset = m._read_event_history(lambda e: True, 10)
    assert [e["tool"] for e in events] == ["h1", "h2"]
    assert offset == path.stat().st_size          # exactly at EOF -> no gap, no repeat


def test_read_event_history_offset_excludes_a_partial_line(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    complete = json.dumps({"ts": _TS, "agent": "a", "tool": "h1", "decision": "allowed"}) + "\n"
    path.write_text(complete + '{"partial": ')
    events, offset = m._read_event_history(lambda e: True, 10)
    assert [e["tool"] for e in events] == ["h1"]
    assert offset == len(complete)                # partial line left for the tail


def test_log_follow_prints_history_then_tails(cli, tmp_config_dir, monkeypatch):
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": "old_call", "decision": "allowed", "server": "s"},
    ])
    def fake_tail(on_event, offset=0, poll=0.25, stop=None):
        on_event({"ts": _TS, "agent": "a", "tool": "live_call",
                  "decision": "denied", "server": "s", "reason": "nope"})
    monkeypatch.setattr(m, "_tail_event_log", fake_tail)

    result = runner.invoke(app, ["log", "-f"])
    assert result.exit_code == 0
    assert "old_call" in result.output                      # scrollback
    assert "Watching for new decisions" in result.output
    assert "live_call" in result.output and "nope" in result.output


def test_log_follow_applies_filters_to_new_events(cli, tmp_config_dir, monkeypatch):
    def fake_tail(on_event, offset=0, poll=0.25, stop=None):
        on_event({"ts": _TS, "agent": "a", "tool": "kept", "decision": "denied", "server": "s"})
        on_event({"ts": _TS, "agent": "b", "tool": "filtered", "decision": "denied", "server": "s"})
    monkeypatch.setattr(m, "_tail_event_log", fake_tail)

    result = runner.invoke(app, ["log", "-f", "--agent", "a"])
    assert "kept" in result.output
    assert "filtered" not in result.output


def test_log_follow_warns_when_daemon_is_down(cli, monkeypatch):
    # Following still works offline (the file is the source) — just say so.
    monkeypatch.setattr(m, "_tail_event_log", lambda *a, **k: None)
    result = runner.invoke(app, ["log", "-f"])
    assert result.exit_code == 0
    assert "no new decisions will be recorded" in result.output


def test_log_follow_truncates_a_long_reason(cli, monkeypatch):
    def fake_tail(on_event, offset=0, poll=0.25, stop=None):
        on_event({"ts": _TS, "agent": "a", "tool": "t", "decision": "denied",
                  "server": "s", "reason": "x" * 200})
    monkeypatch.setattr(m, "_tail_event_log", fake_tail)
    result = runner.invoke(app, ["log", "-f"])
    assert "…" in result.output
    assert "x" * 61 not in result.output



# ─── follow: regression tests for the audit-tail review findings ────


class _StatRaises:
    """A path stand-in whose stat() fails, to test _log_was_replaced portably
    (unlinking an open file is not permitted on Windows)."""
    def __init__(self, exc):
        self.exc = exc

    def stat(self):
        raise self.exc


def test_read_event_history_offset_is_exact_with_invalid_utf8_partial(cli, tmp_config_dir):
    # errors="replace" turns each bad byte into U+FFFD (3 bytes re-encoded), so
    # byte arithmetic done on the decoded text skews the resume offset.
    path = tmp_config_dir / "events.jsonl"
    complete = json.dumps({"ts": _TS, "agent": "a", "tool": "h1", "decision": "allowed"}).encode() + b"\n"
    path.write_bytes(complete + b'{"ts": "' + b"\xff" * 40)

    events, offset = m._read_event_history(lambda e: True, 10)
    assert [e["tool"] for e in events] == ["h1"]
    assert offset == len(complete)          # exact byte boundary, not skewed


def test_follow_does_not_crash_on_a_badly_torn_partial(cli, tmp_config_dir):
    # Enough invalid bytes drove the old arithmetic negative -> ValueError on seek.
    path = tmp_config_dir / "events.jsonl"
    complete = json.dumps({"ts": _TS, "agent": "a", "tool": "h1", "decision": "allowed"}).encode() + b"\n"
    path.write_bytes(complete + b'{"ts": "' + b"\xff" * 80)

    _, offset = m._read_event_history(lambda e: True, 10)
    assert offset >= 0
    seen = []
    m._tail_event_log(seen.append, offset=offset, poll=0, stop=_stop_after(2))


def test_tail_reassembles_a_multibyte_char_split_across_polls(cli, tmp_config_dir):
    # A poll boundary must not finalize the decoder mid-character.
    path = tmp_config_dir / "events.jsonl"
    line = json.dumps({"ts": _TS, "agent": "café", "tool": "t", "decision": "allowed"},
                      ensure_ascii=False).encode("utf-8")
    cut = line.index(b"\xc3") + 1           # split inside the 'é'
    path.write_bytes(line[:cut])
    rest = line[cut:] + b"\n"

    state = {"n": 0}
    def stop():
        state["n"] += 1
        if state["n"] == 2:                 # writer completes the line mid-follow
            with open(path, "ab") as f:
                f.write(rest)
        return state["n"] > 5

    seen = []
    m._tail_event_log(seen.append, offset=0, poll=0, stop=stop)
    assert [e["agent"] for e in seen] == ["café"]


def test_tail_withholds_a_complete_looking_line_until_its_newline(cli, tmp_config_dir):
    # Load-bearing version: the payload is VALID json, so only the newline gate
    # can keep it from being emitted early.
    path = tmp_config_dir / "events.jsonl"
    path.write_text(json.dumps({"ts": _TS, "agent": "a", "tool": "pending", "decision": "allowed"}))

    state = {"n": 0}
    def stop():
        state["n"] += 1
        if state["n"] == 2:
            with open(path, "a") as f:
                f.write("\n")
        return state["n"] > 5

    seen = []
    m._tail_event_log(seen.append, offset=0, poll=0, stop=stop)
    assert [e["tool"] for e in seen] == ["pending"]      # exactly once, after the newline


def test_event_after_a_torn_line_is_not_swallowed(cli, tmp_config_dir):
    # A crash leaves a line with no newline; the next recorded event must survive.
    from steerholm.events import DecisionEvent, EventLog, now_iso
    path = tmp_config_dir / "events.jsonl"
    path.write_bytes(b'{"ts":"2026-08-3')                # torn by a killed daemon

    EventLog(path=path).record(DecisionEvent(
        ts=now_iso(), agent="a", tool="next_real_event", decision="allowed"))

    assert [e["tool"] for e in m._iter_event_log()] == ["next_real_event"]


def test_log_was_replaced_detects_rename_rotation(cli, tmp_config_dir):
    # Rotation by rename leaves a same-or-larger file: size alone can't see it.
    path = tmp_config_dir / "events.jsonl"
    path.write_bytes(b'{"a":1}\n')
    with open(path, "rb") as handle:
        handle.read()
        path.rename(tmp_config_dir / "events.jsonl.1")
        path.write_bytes(b'{"b":2}\n{"c":3}\n')
        assert m._log_was_replaced(handle, path) is True


def test_log_was_replaced_on_missing_file(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    path.write_bytes(b'{"a":1}\n')
    with open(path, "rb") as handle:
        assert m._log_was_replaced(handle, _StatRaises(FileNotFoundError())) is True


def test_log_was_replaced_ignores_a_transient_stat_error(cli, tmp_config_dir):
    # A momentary EACCES/EBUSY must NOT be read as "replaced" (that replays the log).
    path = tmp_config_dir / "events.jsonl"
    path.write_bytes(b'{"a":1}\n')
    with open(path, "rb") as handle:
        assert m._log_was_replaced(handle, _StatRaises(PermissionError())) is False


def test_tail_keyboard_interrupt_stops_the_loop(cli, tmp_config_dir):
    path = tmp_config_dir / "events.jsonl"
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": f"t{i}", "decision": "allowed"} for i in range(3)
    ])
    calls = []
    def boom(e):
        calls.append(e)
        raise KeyboardInterrupt()
    m._tail_event_log(boom, offset=0, poll=0, stop=_stop_after(10))
    assert len(calls) == 1          # Ctrl-C ends the follow, not just one event


def test_tail_picks_up_a_log_created_after_it_starts(cli, tmp_config_dir):
    # "waits for a missing file" must mean it actually reads it once it appears.
    path = tmp_config_dir / "events.jsonl"
    state = {"n": 0}
    def stop():
        state["n"] += 1
        if state["n"] == 2:
            path.write_text(json.dumps(
                {"ts": _TS, "agent": "a", "tool": "first_ever", "decision": "allowed"}) + "\n")
        return state["n"] > 5

    seen = []
    m._tail_event_log(seen.append, offset=0, poll=0, stop=stop)
    assert [e["tool"] for e in seen] == ["first_ever"]


def test_follow_handoff_shows_each_event_exactly_once(cli, tmp_config_dir):
    # The headline property: an event landing between history and tail appears once.
    path = tmp_config_dir / "events.jsonl"
    ev = lambda t: json.dumps({"ts": _TS, "agent": "a", "tool": t, "decision": "allowed"}) + "\n"
    path.write_text(ev("h1") + ev("h2"))

    history, offset = m._read_event_history(lambda e: True, 10)
    with open(path, "a") as f:
        f.write(ev("raced"))                # lands in the handoff window
    seen = []
    m._tail_event_log(seen.append, offset=offset, poll=0, stop=_stop_after(3))

    assert [e["tool"] for e in history] + [e["tool"] for e in seen] == ["h1", "h2", "raced"]


def test_log_follow_wires_the_history_offset_into_the_tail(cli, tmp_config_dir, monkeypatch):
    # Pins the seam: passing offset=0 would re-print history as live events.
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": "h1", "decision": "allowed", "server": "s"},
    ])
    captured = {}
    def fake_tail(on_event, offset=0, poll=0.25, stop=None):
        captured["offset"] = offset
    monkeypatch.setattr(m, "_tail_event_log", fake_tail)

    runner.invoke(app, ["log", "-f"])
    _, expected = m._read_event_history(lambda e: True, 10)
    assert expected > 0 and captured["offset"] == expected


def test_log_follow_zero_limit_shows_all_history(cli, tmp_config_dir, monkeypatch):
    # -n 0 is documented as "all"; it must mean that with -f too.
    _write_events(tmp_config_dir, [
        {"ts": _TS, "agent": "a", "tool": f"t{i}", "decision": "allowed", "server": "s"}
        for i in range(12)
    ])
    monkeypatch.setattr(m, "_tail_event_log", lambda *a, **k: None)
    result = runner.invoke(app, ["log", "-n", "0", "-f"])
    assert result.exit_code == 0
    assert "t0" in result.output        # oldest shown -> not silently capped at 10


def test_log_was_replaced_false_when_fstat_fails(cli, tmp_config_dir):
    # An unusable fd is not evidence of replacement; replaying the log would be worse.
    class _FilenoRaises:
        def tell(self):
            return 0
        def fileno(self):
            raise OSError("no fd")
    path = tmp_config_dir / "events.jsonl"
    path.write_bytes(b'{"a":1}\n')
    assert m._log_was_replaced(_FilenoRaises(), path) is False
