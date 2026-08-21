"""Tests for Pydantic data models."""

import pytest
from pydantic import ValidationError
from mcp_harbour.models import (
    Server,
    ServerType,
    Identity,
    Config,
    AgentPolicy,
    ToolPermission,
    ArgumentPolicy,
)


class TestServer:
    def test_create_minimal(self):
        s = Server(name="test", command="echo hello")
        assert s.name == "test"
        assert s.command == "echo hello"
        assert s.env == {}
        assert s.server_type == ServerType.stdio

    def test_create_http(self):
        s = Server(
            name="remote",
            url="http://localhost:8000/mcp",
            server_type=ServerType.http,
        )
        assert s.server_type == ServerType.http
        assert s.url == "http://localhost:8000/mcp"
        assert s.command == ""

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            Server(command="echo")

    def test_defaults(self):
        s = Server(name="test")
        assert s.command == ""
        assert s.url == ""
        assert s.server_type == ServerType.stdio


class TestIdentity:
    def test_create(self):
        i = Identity(name="agent", key_prefix="harbour_sk_a...")
        assert i.name == "agent"
        assert i.key_prefix.startswith("harbour_sk_")

    def test_missing_fields_raises(self):
        with pytest.raises(ValidationError):
            Identity(name="agent")


class TestArgumentPolicy:
    def test_valid_glob(self):
        p = ArgumentPolicy(arg_name="path", match_type="glob", pattern="/tmp/**")
        assert p.match_type == "glob"

    def test_valid_regex(self):
        p = ArgumentPolicy(arg_name="query", match_type="regex", pattern="^SELECT.*")
        assert p.match_type == "regex"

    def test_default_match_type_is_glob(self):
        p = ArgumentPolicy(arg_name="path", pattern="/tmp/**")
        assert p.match_type == "glob"

    @pytest.mark.parametrize("bad", ["exact", "fuzzy", "GLOB", ""])
    def test_invalid_match_type_raises(self, bad):
        with pytest.raises(ValidationError):
            ArgumentPolicy(arg_name="x", match_type=bad, pattern="*")


class TestToolPermission:
    def test_with_policies(self):
        tp = ToolPermission(
            name="read_file",
            policies=[
                ArgumentPolicy(arg_name="path", match_type="glob", pattern="/safe/**")
            ],
        )
        assert len(tp.policies) == 1

    def test_without_policies(self):
        tp = ToolPermission(name="*")
        assert tp.policies == []


class TestAgentPolicy:
    def test_create(self):
        p = AgentPolicy(
            identity_name="test",
            permissions={"filesystem": [ToolPermission(name="read_file")]},
        )
        assert "filesystem" in p.permissions
        assert len(p.permissions["filesystem"]) == 1

    def test_empty_permissions(self):
        p = AgentPolicy(identity_name="empty", permissions={})
        assert p.permissions == {}


class TestConfig:
    def test_empty_config(self):
        c = Config()
        assert c.servers == {}
        assert c.identities == {}

    def test_with_data(self):
        c = Config(
            servers={"fs": Server(name="fs", command="echo")},
            identities={"a": Identity(name="a", key_prefix="harbour_sk_x")},
        )
        assert "fs" in c.servers
        assert "a" in c.identities


class TestJsonRoundtrip:
    def test_server_roundtrip(self):
        s = Server(name="test", command="echo")
        json_str = s.model_dump_json()
        s2 = Server.model_validate_json(json_str)
        assert s == s2

    def test_policy_roundtrip(self):
        p = AgentPolicy(
            identity_name="agent",
            permissions={
                "fs": [
                    ToolPermission(
                        name="read_*",
                        policies=[
                            ArgumentPolicy(
                                arg_name="path",
                                match_type="glob",
                                pattern="/safe/**",
                            )
                        ],
                    )
                ]
            },
        )
        json_str = p.model_dump_json()
        p2 = AgentPolicy.model_validate_json(json_str)
        assert p == p2
