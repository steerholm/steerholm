"""Tests for the ConfigManager."""

import sys
import pytest
from steerholm.models import Server, Agent, AgentPolicy, ToolPermission, ServerType


class TestConfigManagerServers:
    def test_add_server(self, config_manager):
        config_manager.add_server("filesystem", command="echo hello")
        server = config_manager.get_server("filesystem")
        assert server is not None
        assert server.command == "echo hello"

    def test_add_http_server(self, config_manager):
        config_manager.add_server("remote", url="http://localhost:8000/mcp")
        server = config_manager.get_server("remote")
        assert server.url == "http://localhost:8000/mcp"
        assert server.server_type.value == "http"

    def test_add_server_rejects_both(self, config_manager):
        with pytest.raises(ValueError):
            config_manager.add_server("bad", command="echo", url="http://x")

    def test_add_server_rejects_neither(self, config_manager):
        with pytest.raises(ValueError):
            config_manager.add_server("bad")

    def test_add_duplicate_server_raises(self, config_manager):
        config_manager.add_server("filesystem", command="echo")
        with pytest.raises(ValueError, match="already exists"):
            config_manager.add_server("filesystem", command="echo2")

    def test_list_servers(self, config_manager):
        config_manager.add_server("filesystem", command="echo")
        config_manager.add_server("remote", url="http://localhost/mcp")
        servers = config_manager.list_servers()
        assert len(servers) == 2
        assert {s.name for s in servers} == {"filesystem", "remote"}

    def test_remove_server(self, config_manager):
        config_manager.add_server("filesystem", command="echo")
        config_manager.remove_server("filesystem")
        assert config_manager.get_server("filesystem") is None

    def test_remove_nonexistent_raises(self, config_manager):
        with pytest.raises(ValueError):
            config_manager.remove_server("doesnt-exist")

    def test_get_nonexistent_returns_none(self, config_manager):
        assert config_manager.get_server("nope") is None

    def test_persistence(self, config_manager, tmp_config_dir, monkeypatch):
        config_manager.add_server("filesystem", command="echo hello")

        import steerholm.config as config_mod

        cm2 = config_mod.ConfigManager()
        assert cm2.get_server("filesystem") is not None
        assert cm2.get_server("filesystem").command == "echo hello"


class TestConfigManagerAgents:
    def test_add_agent(self, config_manager):
        config_manager.add_agent("test-agent")
        assert config_manager.get_agent("test-agent") is not None
        assert config_manager.get_agent("test-agent").name == "test-agent"

    def test_add_duplicate_agent_raises(self, config_manager):
        config_manager.add_agent("test-agent")
        with pytest.raises(ValueError, match="already exists"):
            config_manager.add_agent("test-agent")

    def test_get_nonexistent_agent(self, config_manager):
        assert config_manager.get_agent("ghost") is None

    def test_remove_agent_cascades_to_policy(self, config_manager):
        config_manager.add_agent("test-agent")
        config_manager.grant_permission("test-agent", "filesystem", tool="read_file",
                                        arg_policies=["path=/home/user/public/**"])

        assert config_manager.get_agent("test-agent") is not None
        assert config_manager.load_policy("test-agent") is not None

        config_manager.remove_agent("test-agent")

        assert config_manager.get_agent("test-agent") is None
        assert config_manager.load_policy("test-agent") is None

    def test_remove_nonexistent_agent_raises(self, config_manager):
        with pytest.raises(ValueError):
            config_manager.remove_agent("doesnt-exist")

    def test_rotate_agent_key_changes_key_keeps_grants(self, config_manager):
        first = config_manager.add_agent("test-agent")
        config_manager.grant_permission("test-agent", "filesystem", tool="read_file")

        rotated = config_manager.rotate_agent_key("test-agent")

        assert rotated != first
        assert config_manager.get_agent("test-agent").key_prefix == rotated[:15] + "..."
        # grants survive a rotation
        policy = config_manager.load_policy("test-agent")
        assert policy.permissions["filesystem"][0].name == "read_file"

    def test_rotate_nonexistent_agent_raises(self, config_manager):
        with pytest.raises(ValueError, match="not found"):
            config_manager.rotate_agent_key("ghost")


class TestConfigManagerPolicies:
    def test_grant_permission_creates_policy(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")

        policy = config_manager.load_policy("agent")
        assert policy is not None
        assert policy.permissions["filesystem"][0].name == "read_file"

    def test_grant_permission_with_arg_policies(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file",
                                        arg_policies=["path=/home/user/**"])

        policy = config_manager.load_policy("agent")
        arg = policy.permissions["filesystem"][0].policies[0]
        assert arg.arg_name == "path"
        assert arg.match_type == "glob"
        assert arg.pattern == "/home/user/**"

    def test_grant_permission_with_regex(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "db", tool="query",
                                        arg_policies=["sql=re:^SELECT.*"])

        policy = config_manager.load_policy("agent")
        arg = policy.permissions["db"][0].policies[0]
        assert arg.match_type == "regex"
        assert arg.pattern == "^SELECT.*"

    def test_grant_permission_invalid_format_raises(self, config_manager):
        config_manager.add_agent("agent")
        with pytest.raises(ValueError, match="Invalid argument policy"):
            config_manager.grant_permission("agent", "fs", arg_policies=["no_equals_sign"])

    def test_grant_permission_agent_not_found_raises(self, config_manager):
        with pytest.raises(ValueError, match="not found"):
            config_manager.grant_permission("ghost", "filesystem")

    def test_revoke_permission_removes_one_tool(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")
        config_manager.grant_permission("agent", "filesystem", tool="write_file")

        assert config_manager.revoke_permission("agent", "filesystem", tool="read_file") is True

        policy = config_manager.load_policy("agent")
        tool_names = [t.name for t in policy.permissions["filesystem"]]
        assert tool_names == ["write_file"]

    def test_revoke_permission_removes_whole_server(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")
        config_manager.grant_permission("agent", "git", tool="git_status")

        assert config_manager.revoke_permission("agent", "filesystem") is True

        policy = config_manager.load_policy("agent")
        assert "filesystem" not in policy.permissions
        assert "git" in policy.permissions

    def test_revoke_last_tool_drops_the_server(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")

        assert config_manager.revoke_permission("agent", "filesystem", tool="read_file") is True

        policy = config_manager.load_policy("agent")
        assert "filesystem" not in policy.permissions

    def test_revoke_permission_returns_false_when_nothing_matches(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")

        # no such tool grant, and no grant at all for another server
        assert config_manager.revoke_permission("agent", "filesystem", tool="write_file") is False
        assert config_manager.revoke_permission("agent", "git") is False

    def test_revoke_permission_no_policy_returns_false(self, config_manager):
        config_manager.add_agent("agent")  # never granted anything → no policy file
        assert config_manager.revoke_permission("agent", "filesystem") is False

    def test_revoke_permission_agent_not_found_raises(self, config_manager):
        with pytest.raises(ValueError, match="not found"):
            config_manager.revoke_permission("ghost", "filesystem")

    def test_load_nonexistent_policy(self, config_manager):
        assert config_manager.load_policy("nonexistent") is None

    def test_grant_permission_is_additive(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")
        config_manager.grant_permission("agent", "filesystem", tool="write_file")

        policy = config_manager.load_policy("agent")
        tool_names = [t.name for t in policy.permissions["filesystem"]]
        assert "read_file" in tool_names
        assert "write_file" in tool_names

    def test_grant_permission_additive_across_servers(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="*")
        config_manager.grant_permission("agent", "git", tool="git_status")

        policy = config_manager.load_policy("agent")
        assert "filesystem" in policy.permissions
        assert "git" in policy.permissions


# ─── Platform Config Dir ───────────────────────────────────────────


class TestConfigPlatformDir:
    # _get_config_dir() reads sys.platform at call time, not import time,
    # so monkeypatching sys.platform works here.

    def test_unix_config_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        from steerholm.config import _get_config_dir
        assert ".steerholm" in str(_get_config_dir())

    def test_windows_config_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", "/fake/appdata")
        from steerholm.config import _get_config_dir
        path = _get_config_dir()
        assert "steerholm" in str(path)
        assert "appdata" in str(path).lower()


# ─── coverage: config-dir override, control token, error paths ──────

import keyring.errors as _kerr
from unittest.mock import MagicMock as _MM

import pytest as _pytest

from steerholm import config as _cfg


def test_get_config_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("STEERHOLM_CONFIG_DIR", str(tmp_path))
    assert _cfg._get_config_dir() == tmp_path


def test_get_or_create_control_token_creates_then_reuses(monkeypatch):
    store = {}
    monkeypatch.setattr(_cfg.keyring, "get_password", lambda svc, acc: store.get((svc, acc)))
    monkeypatch.setattr(_cfg.keyring, "set_password",
                        lambda svc, acc, v: store.__setitem__((svc, acc), v))
    t1 = _cfg.get_or_create_control_token()
    assert t1.startswith("steer_ctl_")
    assert _cfg.get_or_create_control_token() == t1  # reused, not regenerated


def test_load_config_corrupt_returns_empty(config_manager):
    _cfg.CONFIG_FILE.write_text("{ not valid json")
    config_manager.reload()
    assert config_manager.config.servers == {}


def test_remove_agent_swallows_missing_keyring_entry(config_manager, monkeypatch):
    config_manager.add_agent("agent")
    monkeypatch.setattr(_cfg.keyring, "delete_password",
                        _MM(side_effect=_kerr.PasswordDeleteError("gone")))
    config_manager.remove_agent("agent")
    assert "agent" not in config_manager.config.agents


def test_remove_agent_logs_keyring_error_but_removes(config_manager, monkeypatch):
    config_manager.add_agent("agent")
    monkeypatch.setattr(_cfg.keyring, "delete_password", _MM(side_effect=RuntimeError("boom")))
    config_manager.remove_agent("agent")
    assert "agent" not in config_manager.config.agents


def test_remove_agent_swallows_policy_unlink_error(config_manager, monkeypatch):
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")  # writes a policy file
    monkeypatch.setattr("pathlib.Path.unlink", _MM(side_effect=OSError("locked")))
    config_manager.remove_agent("agent")
    assert "agent" not in config_manager.config.agents


def test_remove_agent_not_found_raises(config_manager):
    with _pytest.raises(ValueError):
        config_manager.remove_agent("ghost")


def test_list_agents(config_manager):
    config_manager.add_agent("a")
    config_manager.add_agent("b")
    names = {i.name for i in config_manager.list_agents()}
    assert names == {"a", "b"}


def test_load_policy_corrupt_returns_none(config_manager):
    (_cfg.POLICIES_DIR / "agent.json").write_text("{ bad json")
    assert config_manager.load_policy("agent") is None


# ─── legacy-schema migration (identities -> agents) ─────────────────


def test_legacy_config_identities_key_migrates_to_agents(config_manager):
    _cfg.CONFIG_FILE.write_text(
        '{"servers": {}, "identities": '
        '{"bob": {"name": "bob", "key_prefix": "steer_sk_x..."}}}'
    )
    config_manager.reload()
    assert "bob" in config_manager.config.agents
    # re-saving rewrites the config in the new schema
    config_manager.save_config()
    import json
    saved = json.loads(_cfg.CONFIG_FILE.read_text())
    assert "agents" in saved and "identities" not in saved


def test_legacy_policy_identity_name_migrates_to_agent_name(config_manager):
    (_cfg.POLICIES_DIR / "bob.json").write_text(
        '{"identity_name": "bob", "permissions": {"fs": [{"name": "*", "policies": []}]}}'
    )
    policy = config_manager.load_policy("bob")
    assert policy is not None
    assert policy.agent_name == "bob"
    assert "fs" in policy.permissions
