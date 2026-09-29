"""Tests for agent resolution from tokens."""

import pytest
from unittest.mock import patch

from tests.conftest import make_gateway


class TestResolveAgentFromToken:
    def test_resolves_correct_agent(self, config_manager):
        token_a = config_manager.add_agent("agent-a")
        token_b = config_manager.add_agent("agent-b")

        gateway = make_gateway(config_manager)
        assert gateway._resolve_agent_from_token(token_a) == "agent-a"
        assert gateway._resolve_agent_from_token(token_b) == "agent-b"

    def test_returns_none_for_unknown_token(self, config_manager):
        config_manager.add_agent("agent-a")

        gateway = make_gateway(config_manager)
        assert gateway._resolve_agent_from_token("steer_sk_wrong_token_here") is None

    def test_returns_none_when_no_agents(self, config_manager):
        gateway = make_gateway(config_manager)
        assert gateway._resolve_agent_from_token("steer_sk_any") is None

    def test_an_agent_with_no_stored_hash_cannot_authenticate(self, config_manager):
        # The state m0002 leaves behind when it could not read the keyring: the
        # agent is in the config but has no verifier. It must deny, and say why
        # — the CLI shows such an agent as perfectly normal.
        token = config_manager.add_agent("agent")
        config_manager.config.agents["agent"].key_hash = None
        config_manager.save_config()
        gateway = make_gateway(config_manager)

        assert gateway._resolve_agent_from_token(token) is None

    def test_does_not_match_partial_token(self, config_manager):
        token = config_manager.add_agent("agent")

        gateway = make_gateway(config_manager)
        assert gateway._resolve_agent_from_token(token[:20]) is None

    def test_cached_token_is_revoked_when_key_rotates(self, config_manager):
        # A resolved token is cached to skip bcrypt on repeat requests; rotating
        # the agent's key must invalidate that cache so the old token is denied.
        token = config_manager.add_agent("agent")
        gateway = make_gateway(config_manager)

        assert gateway._resolve_agent_from_token(token) == "agent"  # populates cache

        config_manager.remove_agent("agent")
        config_manager.add_agent("agent")  # same name, new key
        config_manager.reload()

        assert gateway._resolve_agent_from_token(token) is None

    def test_cached_token_is_revoked_when_agent_deleted(self, config_manager):
        token = config_manager.add_agent("agent")
        gateway = make_gateway(config_manager)

        assert gateway._resolve_agent_from_token(token) == "agent"

        config_manager.remove_agent("agent")
        config_manager.reload()

        assert gateway._resolve_agent_from_token(token) is None
