"""Tests for the PermissionEngine."""

import pytest
from mcp.shared.exceptions import McpError
from mcp_harbour.models import AgentPolicy, ToolPermission, ArgumentPolicy
from mcp_harbour.permissions import PermissionEngine
from mcp_harbour.errors import AUTHORIZATION_DENIED_CODE


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def engine_read_only():
    policy = AgentPolicy(
        identity_name="readonly",
        permissions={
            "filesystem": [
                ToolPermission(
                    name="read_file",
                    policies=[
                        ArgumentPolicy(
                            arg_name="path",
                            match_type="glob",
                            pattern="/home/user/public/**",
                        )
                    ],
                )
            ]
        },
    )
    return PermissionEngine(policy)


@pytest.fixture
def engine_wildcard():
    policy = AgentPolicy(
        identity_name="admin",
        permissions={"filesystem": [ToolPermission(name="*", policies=[])]},
    )
    return PermissionEngine(policy)


@pytest.fixture
def engine_glob_tools():
    policy = AgentPolicy(
        identity_name="reader",
        permissions={"filesystem": [ToolPermission(name="read_*", policies=[])]},
    )
    return PermissionEngine(policy)


@pytest.fixture
def engine_multi_policy():
    policy = AgentPolicy(
        identity_name="strict",
        permissions={
            "database": [
                ToolPermission(
                    name="query",
                    policies=[
                        ArgumentPolicy(
                            arg_name="sql",
                            match_type="regex",
                            pattern=r"^SELECT\s.*",
                        ),
                        ArgumentPolicy(
                            arg_name="db",
                            match_type="glob",
                            pattern="readonly_db",
                        ),
                    ],
                )
            ]
        },
    )
    return PermissionEngine(policy)


def assert_authorization_denied(exc_info):
    """Helper to verify an McpError has the AUTHORIZATION_DENIED code."""
    assert exc_info.value.error.code == AUTHORIZATION_DENIED_CODE


# ─── Server-Level Permission Tests ──────────────────────────────────


class TestServerPermission:
    def test_allowed_server(self, engine_read_only):
        assert engine_read_only.check_permission(
            "filesystem", "read_file", {"path": "/home/user/public/file.txt"}
        )

    def test_denied_server(self, engine_read_only):
        with pytest.raises(McpError) as exc_info:
            engine_read_only.check_permission("git", "git_status")
        assert_authorization_denied(exc_info)

    def test_denied_unregistered_server(self, engine_wildcard):
        with pytest.raises(McpError) as exc_info:
            engine_wildcard.check_permission("database", "query")
        assert_authorization_denied(exc_info)


# ─── Tool-Level Permission Tests ────────────────────────────────────


class TestToolPermission:
    def test_exact_tool_denied(self, engine_read_only):
        with pytest.raises(McpError) as exc_info:
            engine_read_only.check_permission("filesystem", "write_file")
        assert_authorization_denied(exc_info)

    def test_wildcard_allows_all(self, engine_wildcard):
        assert engine_wildcard.check_permission("filesystem", "write_file")
        assert engine_wildcard.check_permission("filesystem", "delete_file")
        assert engine_wildcard.check_permission("filesystem", "read_file")

    def test_glob_match(self, engine_glob_tools):
        assert engine_glob_tools.check_permission("filesystem", "read_file")
        assert engine_glob_tools.check_permission("filesystem", "read_dir")

    def test_glob_no_match(self, engine_glob_tools):
        with pytest.raises(McpError) as exc_info:
            engine_glob_tools.check_permission("filesystem", "write_file")
        assert_authorization_denied(exc_info)


# ─── Argument Policy Tests ──────────────────────────────────────────


class TestArgumentPolicy:
    def test_glob_path_allowed(self, engine_read_only):
        assert engine_read_only.check_permission(
            "filesystem",
            "read_file",
            {"path": "/home/user/public/docs/readme.md"},
        )

    def test_glob_path_denied(self, engine_read_only):
        with pytest.raises(McpError) as exc_info:
            engine_read_only.check_permission(
                "filesystem",
                "read_file",
                {"path": "/etc/passwd"},
            )
        assert_authorization_denied(exc_info)

    def test_policy_skipped_when_no_args(self, engine_read_only):
        assert engine_read_only.check_permission("filesystem", "read_file")

    def test_wildcard_arg_policy_allows_tools_without_that_arg(self):
        # Regression: `--tool "*" --args "path=..."` must not reject tools that
        # have no `path` argument.
        policy = AgentPolicy(
            identity_name="fs",
            permissions={
                "filesystem": [
                    ToolPermission(
                        name="*",
                        policies=[
                            ArgumentPolicy(
                                arg_name="path",
                                match_type="glob",
                                pattern="/home/user/projects/**",
                            )
                        ],
                    )
                ]
            },
        )
        engine = PermissionEngine(policy)

        # A tool with no `path` argument is allowed (was wrongly denied before).
        assert engine.check_permission("filesystem", "list_allowed_directories", {})
        # A tool that provides `path` is still constrained.
        assert engine.check_permission(
            "filesystem", "read_file", {"path": "/home/user/projects/app.py"}
        )
        with pytest.raises(McpError) as exc_info:
            engine.check_permission(
                "filesystem", "read_file", {"path": "/etc/passwd"}
            )
        assert_authorization_denied(exc_info)

    def test_regex_policy_allowed(self, engine_multi_policy):
        assert engine_multi_policy.check_permission(
            "database",
            "query",
            {"sql": "SELECT * FROM users", "db": "readonly_db"},
        )

    def test_regex_policy_denied(self, engine_multi_policy):
        with pytest.raises(McpError) as exc_info:
            engine_multi_policy.check_permission(
                "database",
                "query",
                {"sql": "DROP TABLE users", "db": "readonly_db"},
            )
        assert_authorization_denied(exc_info)

    def test_glob_literal_policy_denied(self, engine_multi_policy):
        with pytest.raises(McpError) as exc_info:
            engine_multi_policy.check_permission(
                "database",
                "query",
                {"sql": "SELECT 1", "db": "production_db"},
            )
        assert_authorization_denied(exc_info)


# ─── Edge Cases ─────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_policy(self):
        engine = PermissionEngine(AgentPolicy(identity_name="empty", permissions={}))
        with pytest.raises(McpError) as exc_info:
            engine.check_permission("any", "any")
        assert_authorization_denied(exc_info)

    def test_policy_with_empty_tool_list(self):
        engine = PermissionEngine(
            AgentPolicy(identity_name="no_tools", permissions={"filesystem": []})
        )
        with pytest.raises(McpError) as exc_info:
            engine.check_permission("filesystem", "read_file")
        assert_authorization_denied(exc_info)


# ─── GPARS Error Code Tests ─────────────────────────────────────────


class TestGPARSErrorCodes:
    def test_denied_error_carries_gpars_data(self, engine_read_only):
        # The code/message facets are covered by test_errors.py; this asserts the
        # engine's denial actually carries the GPARS data payload end to end.
        with pytest.raises(McpError) as exc_info:
            engine_read_only.check_permission("git", "git_status")
        assert exc_info.value.error.data == {"gpars_code": "AUTHORIZATION_DENIED"}


# ─── Multiple Permissions & Regex ──────────────────────────────────


class TestMultipleToolPermissions:
    def test_multiple_patterns_on_same_server(self):
        engine = PermissionEngine(AgentPolicy(
            identity_name="agent",
            permissions={"fs": [
                ToolPermission(name="read_*"),
                ToolPermission(name="write_file"),
            ]},
        ))
        assert engine.check_permission("fs", "read_file")
        assert engine.check_permission("fs", "read_dir")
        assert engine.check_permission("fs", "write_file")
        with pytest.raises(McpError):
            engine.check_permission("fs", "delete_file")

    def test_first_matching_permission_wins(self):
        engine = PermissionEngine(AgentPolicy(
            identity_name="agent",
            permissions={"fs": [
                ToolPermission(name="read_file", policies=[
                    ArgumentPolicy(arg_name="path", match_type="glob", pattern="/safe/**"),
                ]),
                ToolPermission(name="*"),
            ]},
        ))
        with pytest.raises(McpError):
            engine.check_permission("fs", "read_file", {"path": "/etc/passwd"})
        assert engine.check_permission("fs", "write_file")


class TestRegexAnchoring:
    def test_regex_anchored_at_start_not_end(self):
        engine = PermissionEngine(AgentPolicy(
            identity_name="agent",
            permissions={"db": [
                ToolPermission(name="query", policies=[
                    ArgumentPolicy(arg_name="sql", match_type="regex", pattern=r"^SELECT"),
                ]),
            ]},
        ))
        assert engine.check_permission("db", "query", {"sql": "SELECT; DROP TABLE"})

    def test_regex_full_match_with_dollar(self):
        engine = PermissionEngine(AgentPolicy(
            identity_name="agent",
            permissions={"db": [
                ToolPermission(name="query", policies=[
                    ArgumentPolicy(arg_name="sql", match_type="regex", pattern=r"^SELECT\s+\w+$"),
                ]),
            ]},
        ))
        assert engine.check_permission("db", "query", {"sql": "SELECT users"})
        with pytest.raises(McpError):
            engine.check_permission("db", "query", {"sql": "SELECT users; DROP TABLE"})
