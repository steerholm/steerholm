"""
End-to-end tests that start a real daemon, connect over Streamable HTTP,
and exercise the full MCP protocol through Harbour.

Uses @modelcontextprotocol/server-everything as the downstream MCP server.
Requires `npx` to be available.
"""

import asyncio
import json
import socket
from contextlib import AsyncExitStack

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_harbour.config import ConfigManager
from mcp_harbour.gateway import HarbourGateway


# ─── Helpers ────────────────────────────────────────────────────────


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MCPHTTPClient:
    def __init__(self, host: str, port: int):
        self.url = f"http://{host}:{port}/mcp"
        self._stack = AsyncExitStack()
        self._get_session_id = None
        self.session = None

    async def connect(self, token: str):
        http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        await self._stack.enter_async_context(http_client)
        read, write, get_session_id = await self._stack.enter_async_context(
            streamable_http_client(self.url, http_client=http_client, terminate_on_close=False)
        )
        self._get_session_id = get_session_id
        self.session = await self._stack.enter_async_context(ClientSession(read, write))

    async def initialize(self):
        result = await self.session.initialize()
        return result

    def session_id(self) -> str:
        return self._get_session_id()

    async def list_tools(self):
        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: dict = None):
        return await self.session.call_tool(name, arguments or {})

    async def close(self):
        await self._stack.aclose()


# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def e2e_dir(tmp_path):
    config_dir = tmp_path / "harbour-config"
    config_dir.mkdir()
    (config_dir / "policies").mkdir()
    return {"config_dir": config_dir}


@pytest.fixture
def e2e_config(e2e_dir, monkeypatch):
    import mcp_harbour.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", e2e_dir["config_dir"])
    monkeypatch.setattr(config_mod, "CONFIG_FILE", e2e_dir["config_dir"] / "config.json")
    monkeypatch.setattr(config_mod, "POLICIES_DIR", e2e_dir["config_dir"] / "policies")
    monkeypatch.setattr(config_mod, "DEFAULT_HOST", "127.0.0.1")
    monkeypatch.setattr(config_mod, "DEFAULT_PORT", 0)

    return ConfigManager()


@pytest.fixture
def e2e_port():
    return find_free_port()


@pytest.fixture
def e2e_setup(e2e_config, e2e_dir, e2e_port):
    e2e_config.add_server("everything", command="npx -y @modelcontextprotocol/server-everything")

    full_token = e2e_config.add_agent("full-access")
    restricted_token = e2e_config.add_agent("restricted")
    no_policy_token = e2e_config.add_agent("no-policy")

    e2e_config.grant_permission("full-access", "everything", tool="*")
    e2e_config.grant_permission("restricted", "everything", tool="echo")
    e2e_config.grant_permission(
        "restricted",
        "everything",
        tool="get-sum",
        arg_policies=["a=re:^\\d+$"],
    )

    return {
        "config": e2e_config,
        "port": e2e_port,
        "tokens": {
            "full-access": full_token,
            "restricted": restricted_token,
            "no-policy": no_policy_token,
        },
    }


@pytest_asyncio.fixture
async def e2e_daemon(e2e_setup):
    gateway = HarbourGateway()
    port = e2e_setup["port"]

    daemon_task = asyncio.create_task(gateway.serve("127.0.0.1", port))

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        # serve() spawns the npx downstream before it binds the port, so a cold
        # npx download can push first-start well past a few seconds on CI. Probe
        # the unauthenticated /healthz and allow a generous budget.
        for _ in range(180):  # ~90s cap; breaks as soon as it is up
            try:
                if (await client.get("/healthz")).status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.5)
        else:
            daemon_task.cancel()
            pytest.fail("Daemon did not start in time")

    yield {"port": port, **e2e_setup}

    daemon_task.cancel()
    try:
        await daemon_task
    except (asyncio.CancelledError, Exception):
        pass


# ─── Authentication Tests ────────────────────────────────────────────


class TestE2EAuthentication:
    @pytest.mark.asyncio
    async def test_valid_token_initializes_session(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["full-access"])
            result = await client.initialize()

            assert client.session_id()
            assert result.serverInfo.name == "mcp-harbour"
            assert result.capabilities.tools is not None
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, e2e_daemon):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{e2e_daemon['port']}") as client:
            response = await client.post(
                "/mcp",
                headers={"Authorization": "Bearer harbour_sk_bogus_token_that_doesnt_exist"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "e2e-test", "version": "0.1.0"},
                    },
                },
            )

        assert response.status_code == 401


# ─── List Tools Tests ───────────────────────────────────────────────


class TestE2EListTools:
    @pytest.mark.asyncio
    async def test_full_access_sees_all_tools(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["full-access"])
            await client.initialize()
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]

            assert len(tools) > 5
            assert "echo" in tool_names
            assert "get-sum" in tool_names
            assert "get-tiny-image" in tool_names
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_restricted_sees_filtered_tools(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["restricted"])
            await client.initialize()
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]

            assert "echo" in tool_names
            assert "get-sum" in tool_names
            assert "get-tiny-image" not in tool_names
            assert "get-env" not in tool_names
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_no_policy_sees_no_tools(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["no-policy"])
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools) == 0
        finally:
            await client.close()


# ─── Tool Call Tests ────────────────────────────────────────────────


class TestE2ECallTool:
    @pytest.mark.asyncio
    async def test_echo_returns_input(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["full-access"])
            await client.initialize()

            result = await client.call_tool("echo", {"message": "hello harbour"})
            result_str = json.dumps(result.model_dump())
            assert "hello harbour" in result_str
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_get_sum_returns_correct_result(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["full-access"])
            await client.initialize()

            result = await client.call_tool("get-sum", {"a": 7, "b": 3})
            result_str = json.dumps(result.model_dump())
            assert "10" in result_str
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_restricted_can_call_echo(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["restricted"])
            await client.initialize()

            result = await client.call_tool("echo", {"message": "allowed"})
            result_str = json.dumps(result.model_dump())
            assert "allowed" in result_str
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_restricted_denied_on_unpermitted_tool(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["restricted"])
            await client.initialize()

            result = await client.call_tool("get-env")
            assert result.isError is True
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_restricted_denied_on_argument_policy(self, e2e_daemon):
        client = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client.connect(e2e_daemon["tokens"]["restricted"])
            await client.initialize()

            result = await client.call_tool("get-sum", {"a": "not_a_number", "b": 3})
            assert result.isError is True
        finally:
            await client.close()


# ─── Multi-Session Tests ────────────────────────────────────────────


class TestE2EMultipleSessions:
    @pytest.mark.asyncio
    async def test_two_clients_get_isolated_sessions(self, e2e_daemon):
        client_a = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        client_b = MCPHTTPClient("127.0.0.1", e2e_daemon["port"])
        try:
            await client_a.connect(e2e_daemon["tokens"]["full-access"])
            await client_b.connect(e2e_daemon["tokens"]["restricted"])

            await client_a.initialize()
            await client_b.initialize()

            tools_a = await client_a.list_tools()
            tools_b = await client_b.list_tools()

            assert len(tools_a) > len(tools_b)
            names_a = {t.name for t in tools_a}
            names_b = {t.name for t in tools_b}
            assert "get-env" in names_a
            assert "get-env" not in names_b
        finally:
            await client_b.close()
            await client_a.close()
