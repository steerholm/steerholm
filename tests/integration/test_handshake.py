"""Tests for the Streamable HTTP transport authentication and port handling."""

import socket
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import make_gateway, make_mock_process


def _make_http_gateway(config_manager, token_agent=None):
    gateway = make_gateway(config_manager)
    if token_agent is not None:
        gateway._resolve_agent_from_token = MagicMock(return_value=token_agent)
    return gateway


@asynccontextmanager
async def _asgi_client(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:4767",
            headers=headers or {},
        ) as client:
            yield client


async def _post_initialize(client: httpx.AsyncClient, headers=None):
    return await client.post(
        "/mcp",
        headers=headers or {},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        },
    )


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_healthz_returns_harbour_signature(self, config_manager):
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        body = response.json()
        assert body["service"] == "mcp-harbour"
        assert "version" in body

    @pytest.mark.asyncio
    async def test_healthz_needs_no_auth(self, config_manager):
        # The probe must work without a Bearer token (unlike /mcp).
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200


class TestControlPlane:
    @pytest.mark.asyncio
    async def test_reconcile_requires_control_token(self, config_manager):
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            no_auth = await client.post("/control/reconcile")
            bad = await client.post(
                "/control/reconcile", headers={"Authorization": "Bearer harbour_sk_wrong"}
            )
        assert no_auth.status_code == 401
        assert bad.status_code == 401

    @pytest.mark.asyncio
    async def test_reconcile_with_control_token(self, config_manager):
        from mcp_harbour.config import get_or_create_control_token

        token = get_or_create_control_token()
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            resp = await client.post(
                "/control/reconcile", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) >= {"started", "stopped", "failed", "running"}

    @pytest.mark.asyncio
    async def test_agent_token_cannot_drive_control_plane(self, config_manager):
        # An agent's Bearer token must not be accepted on /control/*.
        agent_token = config_manager.add_agent("agent")
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            resp = await client.post(
                "/control/reconcile", headers={"Authorization": f"Bearer {agent_token}"}
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_servers_status_requires_control_token(self, config_manager):
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            resp = await client.get("/control/servers")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_servers_status_returns_state(self, config_manager):
        from mcp_harbour.config import get_or_create_control_token

        config_manager.add_server("srv", command="echo")
        token = get_or_create_control_token()
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            resp = await client.get(
                "/control/servers", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200
        # Docked but not started in this gateway → reported as stopped.
        assert resp.json()["srv"]["state"] == "stopped"

    def test_control_token_isolated_from_agent_keyring(self, config_manager):
        from mcp_harbour.config import get_or_create_control_token

        token = get_or_create_control_token()
        assert token.startswith("harbour_ctl_")
        # An agent whose name would collide with the old control account must
        # not corrupt the control token (it lives in a separate keyring service).
        config_manager.add_agent("__control__")
        config_manager.add_agent("token")
        assert get_or_create_control_token() == token


class TestHTTPAuthentication:
    @pytest.mark.asyncio
    async def test_missing_authorization(self, config_manager):
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await _post_initialize(client)

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.asyncio
    async def test_malformed_authorization(self, config_manager):
        app = make_gateway(config_manager).create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await _post_initialize(client, {"Authorization": "Basic abc"})

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token(self, config_manager):
        gateway = _make_http_gateway(config_manager, None)
        app = gateway.create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await _post_initialize(client, {"Authorization": "Bearer bad_token"})

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_initializes_session(self, config_manager):
        config_manager.add_agent("test-agent")
        gateway = _make_http_gateway(config_manager, "test-agent")
        app = gateway.create_asgi_app("127.0.0.1", 4767)
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(_asgi_client(app, {"Authorization": "Bearer harbour_sk_test"}))
            read, write, get_session_id = await stack.enter_async_context(
                streamable_http_client("http://127.0.0.1:4767/mcp", http_client=http_client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            assert get_session_id()

    @pytest.mark.asyncio
    async def test_session_id_cannot_switch_agent(self, config_manager):
        config_manager.add_agent("agent-one")
        config_manager.add_agent("agent-two")
        gateway = make_gateway(config_manager)
        gateway._resolve_agent_from_token = MagicMock(side_effect=["agent-one", "agent-two"])
        app = gateway.create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            init = await _post_initialize(client, {"Authorization": "Bearer token-one"})
            session_id = init.headers["mcp-session-id"]
            response = await _post_initialize(
                client,
                {
                    "Authorization": "Bearer token-two",
                    "mcp-session-id": session_id,
                },
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_session_id_returns_404(self, config_manager):
        config_manager.add_agent("test-agent")
        gateway = _make_http_gateway(config_manager, "test-agent")
        app = gateway.create_asgi_app("127.0.0.1", 4767)
        async with _asgi_client(app) as client:
            response = await _post_initialize(
                client,
                {
                    "Authorization": "Bearer harbour_sk_test",
                    "mcp-session-id": "missing-session",
                },
            )

        assert response.status_code == 404


class TestSessionCleanup:
    @pytest.mark.asyncio
    async def test_delete_session_does_not_stop_shared_processes(self, config_manager):
        config_manager.add_agent("test-agent")
        config_manager.add_server("test-server", command="mock-server")
        config_manager.grant_permission("test-agent", "test-server", tool="*")

        proc = make_mock_process("test-server", ["read_file"])
        gateway = _make_http_gateway(config_manager, "test-agent")
        gateway.daemon.shared_processes["test-server"] = proc

        app = gateway.create_asgi_app("127.0.0.1", 4767)
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(_asgi_client(app, {"Authorization": "Bearer harbour_sk_test"}))
            read, write, get_session_id = await stack.enter_async_context(
                streamable_http_client(
                    "http://127.0.0.1:4767/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            session_id = get_session_id()
            response = await http_client.delete("/mcp", headers={"mcp-session-id": session_id})
            assert response.status_code == 200

        proc.stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gateway_uses_one_shared_mcp_server(self, config_manager):
        config_manager.add_agent("test-agent")
        gateway = _make_http_gateway(config_manager, "test-agent")
        app = gateway.create_asgi_app("127.0.0.1", 4767)
        shared_server = gateway.session_server

        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(_asgi_client(app, {"Authorization": "Bearer harbour_sk_test"}))
            first = await stack.enter_async_context(
                streamable_http_client("http://127.0.0.1:4767/mcp", http_client=http_client)
            )
            second = await stack.enter_async_context(
                streamable_http_client("http://127.0.0.1:4767/mcp", http_client=http_client)
            )
            first_session = await stack.enter_async_context(ClientSession(first[0], first[1]))
            second_session = await stack.enter_async_context(ClientSession(second[0], second[1]))
            await first_session.initialize()
            await second_session.initialize()

        assert gateway.session_server is shared_server


class TestPortConflict:
    @pytest.mark.asyncio
    async def test_exits_on_port_in_use(self, config_manager):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        try:
            with pytest.raises(SystemExit) as exc_info:
                await make_gateway(config_manager).serve("127.0.0.1", port)
            assert exc_info.value.code == 1
        finally:
            sock.close()
