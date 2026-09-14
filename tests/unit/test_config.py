"""Tests for the ConfigManager."""

import json
import os
import stat
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

    def test_add_server_with_env(self, config_manager):
        config_manager.add_server("db", command="uvx x",
                                  env={"DATABASE_URI": "postgresql://u:p@h/db"})
        assert config_manager.get_server("db").env == {"DATABASE_URI": "postgresql://u:p@h/db"}

    def test_add_server_env_persists_across_reload(self, config_manager):
        config_manager.add_server("db", command="uvx x", env={"K": "v"})
        config_manager.reload()
        assert config_manager.get_server("db").env == {"K": "v"}

    def test_add_server_rejects_env_with_url(self, config_manager):
        with pytest.raises(ValueError, match="stdio"):
            config_manager.add_server("bad", url="http://x", env={"K": "v"})

    def test_add_server_env_key_allows_portable_names(self, config_manager):
        # The Kubernetes / Docker-build rule: letters, digits, '_', '-', '.', not
        # starting with a digit. Every one of these is valid on every OS and must
        # be accepted (guards against regressing into a too-strict POSIX rule).
        env = {
            "DATABASE_URI": "x",
            "FOO-BAR": "1",
            "my.env.name": "2",
            "lower_case": "3",
            "MixedCase1": "4",
            "_INTERNAL": "5",
        }
        config_manager.add_server("db", command="uvx x", env=env)
        assert config_manager.get_server("db").env == env

    def test_add_server_env_key_rejects_leading_digit(self, config_manager):
        with pytest.raises(ValueError, match="digit"):
            config_manager.add_server("db", command="uvx x", env={"2FA_TOKEN": "v"})

    def test_add_server_env_key_rejects_parentheses(self, config_manager):
        # A Windows-only name (parentheses) is rejected for portability, even
        # though the Win32 API would accept it — it's inherited, never declared.
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.add_server("db", command="uvx x", env={"ProgramFiles(x86)": "v"})

    def test_add_server_env_key_rejects_internal_whitespace(self, config_manager):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.add_server("db", command="uvx x", env={"FOO BAR": "v"})

    def test_add_server_env_key_rejects_leading_whitespace(self, config_manager):
        # The model layer rejects a padded key (rather than silently trimming) —
        # only the CLI convenience-trims before it reaches here.
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.add_server("db", command="uvx x", env={" K ": "v"})

    def test_add_server_env_key_rejects_control_char(self, config_manager):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.add_server("db", command="uvx x", env={"A\x01B": "v"})

    def test_add_server_env_key_rejects_equals(self, config_manager):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.add_server("db", command="uvx x", env={"A=B": "v"})

    def test_add_server_env_key_rejects_empty(self, config_manager):
        with pytest.raises(ValueError, match="empty"):
            config_manager.add_server("db", command="uvx x", env={"": "v"})

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows isolates AppData via ACLs")
    def test_config_and_policy_files_are_owner_only(self, config_manager):
        import steerholm.config as c
        config_manager.add_server("db", command="uvx x", env={"DB": "secret"})
        config_manager.add_agent("a")
        config_manager.grant_permission("a", "db", tool="*")
        assert stat.S_IMODE(os.stat(c.CONFIG_DIR).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(c.POLICIES_DIR).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(c.CONFIG_FILE).st_mode) == 0o600
        policy = c.POLICIES_DIR / f"{config_manager._agent_id('a')}.json"
        assert policy.exists()
        assert stat.S_IMODE(os.stat(policy).st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows isolates AppData via ACLs")
    def test_update_hardens_preexisting_loose_config(self, config_manager):
        # Simulate a v0.1.0 install (dir + files world-readable), then start the
        # updated binary — a fresh ConfigManager must re-harden everything.
        import steerholm.config as c
        config_manager.add_server("db", command="uvx x", env={"DB": "secret"})
        config_manager.add_agent("a")
        config_manager.grant_permission("a", "db", tool="*")
        policy = c.POLICIES_DIR / f"{config_manager._agent_id('a')}.json"
        os.chmod(c.CONFIG_DIR, 0o755)
        os.chmod(c.CONFIG_FILE, 0o644)
        os.chmod(policy, 0o644)

        c.ConfigManager()  # like the updated daemon/CLI starting up

        assert stat.S_IMODE(os.stat(c.CONFIG_DIR).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(c.CONFIG_FILE).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(policy).st_mode) == 0o600

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
        config_manager.add_server("filesystem", command="echo")
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
        config_manager.add_server("filesystem", command="echo")
        config_manager.grant_permission("test-agent", "filesystem", tool="read_file")

        rotated = config_manager.rotate_agent_key("test-agent")

        assert rotated != first
        assert config_manager.get_agent("test-agent").key_prefix == rotated[:15] + "..."
        # grants survive a rotation
        policy = config_manager.load_policy("test-agent")
        assert policy.permissions[config_manager._server_id("filesystem")][0].name == "read_file"

    def test_rotate_nonexistent_agent_raises(self, config_manager):
        with pytest.raises(ValueError, match="not found"):
            config_manager.rotate_agent_key("ghost")

    def test_add_agent_mints_immutable_id(self, config_manager):
        config_manager.add_agent("a")
        assert config_manager.get_agent("a").id.startswith("agt_")

    def test_agent_id_persists_across_reload(self, config_manager):
        config_manager.add_agent("a")
        agent_id = config_manager.get_agent("a").id
        config_manager.reload()
        assert config_manager.get_agent("a").id == agent_id

    def test_rotate_preserves_agent_id(self, config_manager):
        # A new key for the same principal must NOT change its id.
        config_manager.add_agent("a")
        original = config_manager.get_agent("a").id
        config_manager.rotate_agent_key("a")
        assert config_manager.get_agent("a").id == original

    def test_recreate_same_name_gets_a_new_id(self, config_manager):
        # The audit-integrity case: delete + re-add the same name is a NEW principal.
        config_manager.add_agent("cursor")
        first = config_manager.get_agent("cursor").id
        config_manager.remove_agent("cursor")
        config_manager.add_agent("cursor")
        assert config_manager.get_agent("cursor").id != first

    def test_legacy_agent_without_id_loads_as_none(self, config_manager):
        _cfg.CONFIG_FILE.write_text(
            '{"servers": {}, "agents": '
            '{"old": {"name": "old", "key_prefix": "steer_sk_x..."}}}'
        )
        config_manager.reload()
        assert config_manager.get_agent("old").id is None

    def test_legacy_agent_id_stays_none_across_rotation(self, config_manager):
        # Forward-only: rotation must not mint an id for a legacy agent.
        _cfg.CONFIG_FILE.write_text(
            '{"servers": {}, "agents": '
            '{"old": {"name": "old", "key_prefix": "steer_sk_x..."}}}'
        )
        config_manager.reload()
        config_manager.rotate_agent_key("old")
        assert config_manager.get_agent("old").id is None


class TestConfigManagerPolicies:
    @pytest.fixture(autouse=True)
    def _servers(self, config_manager):
        # A grant records the server's id, so the server has to exist first.
        for name in ("filesystem", "git", "db", "srv", "fs"):
            if not config_manager.get_server(name):
                config_manager.add_server(name, command="echo")

    def test_grant_permission_creates_policy(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")

        policy = config_manager.load_policy("agent")
        assert policy is not None
        assert policy.permissions[config_manager._server_id("filesystem")][0].name == "read_file"

    def test_grant_permission_with_arg_policies(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file",
                                        arg_policies=["path=/home/user/**"])

        policy = config_manager.load_policy("agent")
        arg = policy.permissions[config_manager._server_id("filesystem")][0].policies[0]
        assert arg.arg_name == "path"
        assert arg.match_type == "glob"
        assert arg.pattern == "/home/user/**"

    def test_grant_permission_with_regex(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "db", tool="query",
                                        arg_policies=["sql=re:^SELECT.*"])

        policy = config_manager.load_policy("agent")
        arg = policy.permissions[config_manager._server_id("db")][0].policies[0]
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
        tool_names = [t.name for t in policy.permissions[config_manager._server_id("filesystem")]]
        assert tool_names == ["write_file"]

    def test_revoke_permission_removes_whole_server(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")
        config_manager.grant_permission("agent", "git", tool="git_status")

        assert config_manager.revoke_permission("agent", "filesystem") is True

        policy = config_manager.load_policy("agent")
        assert config_manager._server_id("filesystem") not in policy.permissions
        assert config_manager._server_id("git") in policy.permissions

    def test_revoke_last_tool_drops_the_server(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="read_file")

        assert config_manager.revoke_permission("agent", "filesystem", tool="read_file") is True

        policy = config_manager.load_policy("agent")
        assert config_manager._server_id("filesystem") not in policy.permissions

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
        tool_names = [t.name for t in policy.permissions[config_manager._server_id("filesystem")]]
        assert "read_file" in tool_names
        assert "write_file" in tool_names

    def test_grant_permission_additive_across_servers(self, config_manager):
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "filesystem", tool="*")
        config_manager.grant_permission("agent", "git", tool="git_status")

        policy = config_manager.load_policy("agent")
        assert config_manager._server_id("filesystem") in policy.permissions
        assert config_manager._server_id("git") in policy.permissions


# ─── Platform Config Dir ───────────────────────────────────────────


class TestConfigPlatformDir:
    # _get_config_dir() reads sys.platform at call time, not import time,
    # so monkeypatching sys.platform works here.

    @pytest.fixture(autouse=True)
    def _no_override(self, monkeypatch):
        # The suite sets STEERHOLM_CONFIG_DIR so no test can touch the real
        # config; these tests are about the fallback it overrides.
        monkeypatch.delenv("STEERHOLM_CONFIG_DIR", raising=False)

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
    config_manager.add_server("srv", command="echo")
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


class TestAuditSettings:
    def test_defaults(self, config_manager):
        audit = config_manager.config.audit
        assert (audit.max_files, audit.max_age_days) == (5, 0)

    def test_audit_kwargs_excludes_the_fixed_segment_size(self, config_manager):
        # Segment size is not user-settable, so it is not passed through config.
        assert config_manager.audit_kwargs() == {"max_files": 5, "max_age_days": 0}

    def test_set_and_persist(self, config_manager):
        config_manager.set_audit_settings(max_files=3, max_age_days=30)
        config_manager.reload()
        audit = config_manager.config.audit
        assert (audit.max_files, audit.max_age_days) == (3, 30)

    def test_partial_update_leaves_others(self, config_manager):
        config_manager.set_audit_settings(max_age_days=14)
        audit = config_manager.config.audit
        assert (audit.max_age_days, audit.max_files) == (14, 5)

    def test_rejects_invalid_values(self, config_manager):
        with pytest.raises(ValueError, match="negative"):
            config_manager.set_audit_settings(max_files=-1)
        with pytest.raises(ValueError, match="negative"):
            config_manager.set_audit_settings(max_age_days=-1)

    def test_legacy_config_without_audit_section_loads(self, config_manager):
        _cfg.CONFIG_FILE.write_text('{"servers": {}, "agents": {}}')
        config_manager.reload()
        assert config_manager.config.audit.max_files == 5     # defaults applied


class TestServerIds:
    def test_add_server_mints_an_immutable_id(self, config_manager):
        config_manager.add_server("git", command="uvx mcp-server-git")
        assert config_manager.get_server("git").id.startswith("srv_")

    def test_http_server_gets_an_id_too(self, config_manager):
        config_manager.add_server("api", url="http://localhost:8000/mcp")
        assert config_manager.get_server("api").id.startswith("srv_")

    def test_server_id_persists_across_reload(self, config_manager):
        config_manager.add_server("git", command="echo")
        sid = config_manager.get_server("git").id
        config_manager.reload()
        assert config_manager.get_server("git").id == sid

    def test_re_added_name_gets_a_new_id(self, config_manager):
        # The audit-integrity case: the same name can point at a different server.
        config_manager.add_server("git", command="uvx mcp-server-git")
        first = config_manager.get_server("git").id
        config_manager.remove_server("git")
        config_manager.add_server("git", command="something-else")
        assert config_manager.get_server("git").id != first

    def test_legacy_server_without_an_id_loads_as_none(self, config_manager):
        _cfg.CONFIG_FILE.write_text(
            '{"servers": {"old": {"name": "old", "command": "echo",'
            ' "server_type": "stdio"}}, "agents": {}}'
        )
        config_manager.reload()
        assert config_manager.get_server("old").id is None


class TestModifyServer:
    def test_changes_the_command_and_keeps_the_id(self, config_manager):
        config_manager.add_server("git", command="old")
        sid = config_manager.get_server("git").id
        config_manager.modify_server("git", command="new")
        after = config_manager.get_server("git")
        assert after.command == "new"
        assert after.id == sid            # same server, new config

    def test_env_merges_and_unset_removes(self, config_manager):
        config_manager.add_server("db", command="x", env={"A": "1", "B": "2"})
        config_manager.modify_server("db", env={"B": "9", "C": "3"}, unset=["A"])
        assert config_manager.get_server("db").env == {"B": "9", "C": "3"}

    def test_env_only_change_keeps_the_command(self, config_manager):
        config_manager.add_server("db", command="uvx x", env={"A": "1"})
        config_manager.modify_server("db", env={"B": "2"})
        after = config_manager.get_server("db")
        assert after.command == "uvx x" and after.env == {"A": "1", "B": "2"}

    def test_unsetting_a_missing_key_is_not_an_error(self, config_manager):
        config_manager.add_server("db", command="x", env={"A": "1"})
        config_manager.modify_server("db", unset=["NOPE"])
        assert config_manager.get_server("db").env == {"A": "1"}

    def test_switch_to_url_clears_command_and_env(self, config_manager):
        config_manager.add_server("git", command="uvx x", env={"A": "1"})
        config_manager.modify_server("git", url="http://localhost:9000/mcp")
        after = config_manager.get_server("git")
        assert after.url == "http://localhost:9000/mcp"
        assert after.command == "" and after.env == {}
        assert after.server_type.value == "http"

    def test_switch_to_command_clears_the_url(self, config_manager):
        config_manager.add_server("api", url="http://localhost:8000/mcp")
        config_manager.modify_server("api", command="uvx x")
        after = config_manager.get_server("api")
        assert after.command == "uvx x" and after.url == ""
        assert after.server_type.value == "stdio"

    def test_grants_survive_a_modification(self, config_manager):
        config_manager.add_server("git", command="old")
        config_manager.add_agent("a")
        config_manager.grant_permission("a", "git", tool="git_log")
        config_manager.modify_server("git", command="new")
        policy = config_manager.load_policy("a")
        assert [t.name for t in policy.permissions[config_manager._server_id("git")]] == ["git_log"]

    def test_persists_across_reload(self, config_manager):
        config_manager.add_server("git", command="old")
        config_manager.modify_server("git", command="new")
        config_manager.reload()
        assert config_manager.get_server("git").command == "new"

    def test_rejects_an_unknown_server(self, config_manager):
        with pytest.raises(ValueError, match="not found"):
            config_manager.modify_server("ghost", command="x")

    def test_rejects_command_and_url_together(self, config_manager):
        config_manager.add_server("git", command="x")
        with pytest.raises(ValueError, match="not both"):
            config_manager.modify_server("git", command="a", url="http://x")

    def test_rejects_env_with_url(self, config_manager):
        config_manager.add_server("git", command="x")
        with pytest.raises(ValueError, match="stdio"):
            config_manager.modify_server("git", url="http://x", env={"A": "1"})

    def test_rejects_env_on_a_remote_server(self, config_manager):
        config_manager.add_server("api", url="http://localhost:8000/mcp")
        with pytest.raises(ValueError, match="remote"):
            config_manager.modify_server("api", env={"A": "1"})

    def test_validates_env_names(self, config_manager):
        config_manager.add_server("db", command="x")
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            config_manager.modify_server("db", env={"BAD NAME": "1"})


# ─── the grant cascade, keyed by id ─────────────────────────────────


class TestGrantCascade:
    """Removing either side of a grant takes the grant with it."""

    def _wire(self, cm):
        cm.add_server("git", command="x")
        cm.add_server("db", command="y")
        cm.add_agent("a")
        cm.add_agent("b")
        for agent in ("a", "b"):
            cm.grant_permission(agent, "git", tool="git_log")
            cm.grant_permission(agent, "db", tool="query")

    def test_removing_a_server_revokes_its_grants(self, config_manager):
        self._wire(config_manager)
        git_id = config_manager._server_id("git")
        db_id = config_manager._server_id("db")
        affected = config_manager.remove_server("git")
        assert affected == ["a", "b"]
        for agent in ("a", "b"):
            policy = config_manager.load_policy(agent)
            assert git_id not in policy.permissions
            assert db_id in policy.permissions      # other servers untouched

    def test_a_re_added_server_starts_with_no_grants(self, config_manager):
        # The privilege-transfer case: the same name must not inherit trust.
        self._wire(config_manager)
        old_id = config_manager._server_id("git")
        config_manager.remove_server("git")
        config_manager.add_server("git", command="something-else")
        new_id = config_manager._server_id("git")
        permissions = config_manager.load_policy("a").permissions
        assert new_id != old_id
        assert new_id not in permissions and old_id not in permissions

    def test_removing_a_server_nobody_uses_affects_nobody(self, config_manager):
        config_manager.add_server("git", command="x")
        config_manager.add_agent("a")
        assert config_manager.remove_server("git") == []

    def test_removing_an_agent_takes_its_grants(self, config_manager):
        self._wire(config_manager)
        config_manager.remove_agent("a")
        assert config_manager.load_policy("a") is None
        assert config_manager.load_policy("b") is not None

    def test_cascade_survives_reload(self, config_manager):
        self._wire(config_manager)
        git_id = config_manager._server_id("git")
        config_manager.remove_server("git")
        config_manager.reload()
        assert git_id not in config_manager.load_policy("a").permissions

    def test_grants_survive_a_server_modification(self, config_manager):
        config_manager.add_server("git", command="old")
        config_manager.add_agent("a")
        config_manager.grant_permission("a", "git", tool="git_log")
        server_id = config_manager._server_id("git")
        config_manager.modify_server("git", command="new")
        # keeping the id is what keeps the grant attached across the edit
        assert config_manager._server_id("git") == server_id
        assert [t.name for t in config_manager.load_policy("a").permissions[server_id]] == ["git_log"]


class TestAgentsWithGrants:
    def test_lists_only_agents_holding_a_grant(self, config_manager):
        config_manager.add_server("git", command="x")
        config_manager.add_agent("holder")
        config_manager.add_agent("bystander")
        config_manager.grant_permission("holder", "git", tool="*")
        assert config_manager.agents_with_grants("git") == ["holder"]

    def test_empty_when_nobody_has_grants(self, config_manager):
        config_manager.add_server("git", command="x")
        assert config_manager.agents_with_grants("git") == []

    def test_empty_for_a_server_that_does_not_exist(self, config_manager):
        assert config_manager.agents_with_grants("ghost") == []


# ─── writes resolve their path from the config, not the document ────


def test_a_policy_file_claiming_another_id_is_not_written_to(config_manager):
    # load_policy resolves the path from the config; if save_policy trusted the
    # document's own agent_id, a revoke would land elsewhere and report success
    # while the real grant stayed live.
    config_manager.add_server("git", command="echo")
    config_manager.add_agent("bob")
    config_manager.grant_permission("bob", "git", tool="*")
    path = config_manager.policy_path_for("bob")
    doc = json.loads(path.read_text())
    doc["agent_id"] = "agt_0000000000000000"
    path.write_text(json.dumps(doc))

    assert config_manager.revoke_permission("bob", "git") is True
    assert config_manager.load_policy("bob").permissions == {}
    assert not (_cfg.POLICIES_DIR / "agt_0000000000000000.json").exists()


def test_create_policy_for_an_unknown_agent_raises(config_manager):
    with pytest.raises(ValueError, match="not found"):
        config_manager.create_policy("ghost")


def test_load_policy_for_an_unknown_agent_returns_none(config_manager):
    assert config_manager.load_policy("ghost") is None


def test_a_grant_whose_server_vanished_can_still_be_revoked(config_manager):
    # `holm show agent` prints such a grant as a raw srv_ id, so that id has to
    # be something `holm revoke` accepts.
    config_manager.add_server("git", command="x")
    config_manager.add_agent("alice")
    config_manager.grant_permission("alice", "git", tool="*")
    server_id = config_manager._server_id("git")
    del config_manager.config.servers["git"]      # cascade did not run
    config_manager.save_config()

    assert config_manager.revoke_permission("alice", server_id) is True
    assert config_manager.load_policy("alice").permissions == {}


# ─── names become ids' neighbours, so they are validated ────────────


class TestEntityNameValidation:
    def test_an_empty_name_is_rejected(self, config_manager):
        with pytest.raises(ValueError, match="cannot be empty"):
            config_manager.add_agent("")

    def test_a_name_that_is_not_safe_as_a_filename_is_rejected(self, config_manager):
        for bad in ("../evil", "a/b", "a b", ".hidden", "-leading"):
            with pytest.raises(ValueError, match="must consist of letters"):
                config_manager.add_agent(bad)

    def test_a_name_shaped_like_an_id_is_rejected(self, config_manager):
        # Ids and names share a namespace in the CLI (`holm log --agent` takes
        # either), so a name must never be confusable with an id.
        with pytest.raises(ValueError, match="reserved for ids"):
            config_manager.add_agent("agt_0000000000000000")
        with pytest.raises(ValueError, match="reserved for ids"):
            config_manager.add_server("srv_0000000000000000", command="echo")

    def test_the_reserved_prefix_check_is_case_insensitive(self, config_manager):
        # macOS and Windows filesystems are case-insensitive, so `AGT_<hex>`
        # would resolve to the same policy file as a real `agt_<hex>` id.
        with pytest.raises(ValueError, match="reserved for ids"):
            config_manager.add_agent("AGT_0000000000000000")
        with pytest.raises(ValueError, match="reserved for ids"):
            config_manager.add_server("Srv_0000000000000000", command="echo")

    def test_servers_are_validated_too(self, config_manager):
        with pytest.raises(ValueError, match="must consist of letters"):
            config_manager.add_server("../evil", command="echo")

    def test_ordinary_names_are_accepted(self, config_manager):
        for good in ("git", "my-agent", "agent_2", "web.api", "_internal"):
            config_manager.add_agent(good)
        assert "web.api" in config_manager.config.agents


def test_an_agent_that_exists_without_an_id_says_so(config_manager):
    # "not found" for an agent the tool is listing sends the operator hunting
    # for a typo instead of at the real cause.
    config_manager.add_agent("bob")
    config_manager.config.agents["bob"].id = None

    with pytest.raises(ValueError, match="has no id"):
        config_manager.create_policy("bob")
    with pytest.raises(ValueError, match="not found"):
        config_manager.policy_path_for("bob")


def test_removing_an_id_less_agent_does_not_raise(config_manager):
    config_manager.add_agent("bob")
    config_manager.config.agents["bob"].id = None
    config_manager.remove_agent("bob")           # must not raise
    assert "bob" not in config_manager.config.agents


def test_removing_an_unsafely_named_agent_does_not_follow_the_name(config_manager, caplog):
    # remove_agent builds a path from the name; a config predating validation
    # can still hold an unsafe one.
    outside = _cfg.CONFIG_DIR / "secret.json"
    outside.write_text("{}")
    config_manager.add_agent("victim")
    config_manager.config.agents["../secret"] = config_manager.config.agents.pop("victim")
    config_manager.config.agents["../secret"].name = "../secret"

    config_manager.remove_agent("../secret")

    assert outside.exists()
    assert "not safe to use as a filename" in caplog.text


def test_a_corrupt_policy_file_reads_as_no_policy(config_manager):
    config_manager.add_agent("bob")
    config_manager.policy_path_for("bob").write_text("{ bad json")
    assert config_manager.load_policy("bob") is None


def test_granting_on_a_server_that_does_not_exist_raises(config_manager):
    # A grant records the server's id, so there is nothing to point at. This is
    # the invariant the change exists to enforce; it lives here, not just in the
    # CLI, so it is asserted at the layer that owns it.
    config_manager.add_agent("bob")

    with pytest.raises(ValueError, match="Add it first"):
        config_manager.grant_permission("bob", "ghost")

    assert config_manager.load_policy("bob") is None   # and nothing was written
