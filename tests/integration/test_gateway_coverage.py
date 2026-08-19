"""Coverage for gateway branches: auth cache, reconcile edges, supervisor, serve."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_harbour.process_manager import ServerHealth
from tests.conftest import make_gateway, make_mock_process


# ─── auth cache ─────────────────────────────────────────────────────


def test_auth_cache_evicts_oldest_over_cap(config_manager):
    t1 = config_manager.add_agent("a")
    t2 = config_manager.add_agent("b")
    t3 = config_manager.add_agent("c")
    gateway = make_gateway(config_manager)
    gateway._auth_cache_max = 2

    assert gateway._resolve_agent_from_token(t1) == "a"
    assert gateway._resolve_agent_from_token(t2) == "b"
    assert gateway._resolve_agent_from_token(t3) == "c"

    assert len(gateway._auth_cache) == 2  # oldest (a) evicted


def test_auth_cache_hit_returns_without_rebcrypt(config_manager, monkeypatch):
    import mcp_harbour.gateway as gw
    token = config_manager.add_agent("a")
    gateway = make_gateway(config_manager)

    assert gateway._resolve_agent_from_token(token) == "a"  # populates cache
    spy = MagicMock(wraps=gw.bcrypt.checkpw)
    monkeypatch.setattr(gw.bcrypt, "checkpw", spy)
    assert gateway._resolve_agent_from_token(token) == "a"  # cache hit
    spy.assert_not_called()


def test_auth_cache_hit_keyring_error_invalidates(config_manager, monkeypatch):
    import mcp_harbour.gateway as gw
    token = config_manager.add_agent("a")
    gateway = make_gateway(config_manager)
    assert gateway._resolve_agent_from_token(token) == "a"  # populate cache

    monkeypatch.setattr(gw.keyring, "get_password", MagicMock(side_effect=RuntimeError("boom")))
    assert gateway._resolve_agent_from_token(token) is None  # invalidated, then miss


# ─── reconcile edge branches ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_restarts_on_spec_change(config_manager):
    config_manager.add_server("srv", command="echo one")
    gateway = make_gateway(config_manager)

    async def start_side_effect(server):
        proc = make_mock_process(server.name, ["t"])
        proc.server_config = server
        gateway.daemon.shared_processes[server.name] = proc

    async def stop_side_effect(name):
        gateway.daemon.shared_processes.pop(name, None)

    gateway.daemon.start_shared_server = AsyncMock(side_effect=start_side_effect)
    gateway.daemon.stop_shared_server = AsyncMock(side_effect=stop_side_effect)

    await gateway.reconcile_servers()
    assert gateway.daemon.start_shared_server.await_count == 1

    # Change the server's command -> reconcile must stop + restart it.
    from mcp_harbour.models import Server, ServerType
    config_manager.config.servers["srv"] = Server(
        name="srv", command="echo TWO", server_type=ServerType.stdio
    )
    result = await gateway.reconcile_servers()
    assert result["started"] == ["srv"]
    assert gateway.daemon.start_shared_server.await_count == 2
    gateway.daemon.stop_shared_server.assert_awaited()


@pytest.mark.asyncio
async def test_reconcile_reports_failed_start(config_manager):
    config_manager.add_server("broken", command="bad")
    gateway = make_gateway(config_manager)
    gateway.daemon.start_shared_server = AsyncMock(side_effect=RuntimeError("nope"))

    result = await gateway.reconcile_servers()
    assert result["failed"] == ["broken"]
    assert result["started"] == []


@pytest.mark.asyncio
async def test_reconcile_reports_failed_stop(config_manager):
    gateway = make_gateway(config_manager)
    gateway.daemon.shared_processes["ghost"] = make_mock_process("ghost", ["t"])
    gateway.daemon.stop_shared_server = AsyncMock(side_effect=RuntimeError("stuck"))

    result = await gateway.reconcile_servers()  # ghost not in config -> stop, which fails
    assert result["failed"] == ["ghost"]


# ─── supervisor / liveness ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_leaves_slow_server_alone(config_manager):
    gateway = make_gateway(config_manager)
    slow = make_mock_process("slow", ["t"])
    slow.session = MagicMock()

    async def hang():
        await asyncio.sleep(3600)

    slow.list_tools = AsyncMock(side_effect=hang)
    gateway.daemon.shared_processes["slow"] = slow
    gateway.daemon.stop_shared_server = AsyncMock()

    # Patch the timeout so the test doesn't actually wait 10s.
    import mcp_harbour.gateway as gw
    orig_wait_for = gw.asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await orig_wait_for(coro, 0.05)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(gw.asyncio, "wait_for", fast_wait_for)
        await gateway._restart_unhealthy_servers()

    gateway.daemon.stop_shared_server.assert_not_called()  # slow != dead


@pytest.mark.asyncio
async def test_reconcile_loop_runs_and_survives_errors(config_manager, monkeypatch):
    import mcp_harbour.gateway as gw
    gateway = make_gateway(config_manager)
    gateway._restart_unhealthy_servers = AsyncMock()
    gateway.reconcile_servers = AsyncMock(side_effect=RuntimeError("transient"))

    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(gw.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await gateway._reconcile_loop(interval=0)

    gateway._restart_unhealthy_servers.assert_awaited()
    gateway.reconcile_servers.assert_awaited()  # raised, was caught + logged


# ─── serve() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_serve_runs_and_cancels_supervisor(config_manager, monkeypatch):
    import uvicorn
    gateway = make_gateway(config_manager)
    monkeypatch.setattr(gateway, "reconcile_servers", AsyncMock())
    monkeypatch.setattr("mcp_harbour.gateway.get_or_create_control_token", lambda: "tok")
    monkeypatch.setattr(gateway, "create_asgi_app", lambda host, port: MagicMock())

    async def hang(interval=30.0):
        await asyncio.sleep(3600)

    monkeypatch.setattr(gateway, "_reconcile_loop", hang)

    fake_server = MagicMock()
    fake_server.serve = AsyncMock()
    monkeypatch.setattr(uvicorn, "Server", lambda config: fake_server)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: MagicMock())

    await gateway.serve("127.0.0.1", 0)
    fake_server.serve.assert_awaited_once()


# ─── __init__, agent-context, tool error paths ─────────────────────


def test_gateway_real_init(config_manager):
    from mcp_harbour.gateway import HarbourGateway
    gw = HarbourGateway()
    assert gw._auth_cache_max == 4096
    assert gw.session_server is not None


def test_current_agent_name_without_context_denies(config_manager):
    from mcp.shared.exceptions import McpError
    gateway = make_gateway(config_manager)
    with pytest.raises(McpError):
        gateway._current_agent_name()


@pytest.mark.asyncio
async def test_list_allowed_tools_logs_and_skips_on_error(config_manager):
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    proc = make_mock_process("srv", ["t"])
    proc.session = MagicMock()
    proc.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
    gateway.daemon.shared_processes["srv"] = proc
    assert await gateway._list_allowed_tools("agent") == []


@pytest.mark.asyncio
async def test_resolve_tool_server_logs_and_returns_none_on_error(config_manager):
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    proc = make_mock_process("srv", ["t"])
    proc.session = MagicMock()
    proc.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
    gateway.daemon.shared_processes["srv"] = proc
    assert await gateway._resolve_tool_server("agent", "t") is None


@pytest.mark.asyncio
async def test_call_tool_server_unavailable_when_no_process(config_manager, monkeypatch):
    from mcp.shared.exceptions import McpError
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    monkeypatch.setattr(gateway, "_resolve_tool_server", AsyncMock(return_value="srv"))
    with pytest.raises(McpError):
        await gateway._call_tool_for_agent("agent", "t", {})


@pytest.mark.asyncio
async def test_call_tool_wraps_generic_error(config_manager):
    from mcp.shared.exceptions import McpError
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    proc = make_mock_process("srv", ["t"])
    proc.session = MagicMock()
    proc.call_tool = AsyncMock(side_effect=RuntimeError("kaboom"))
    gateway.daemon.shared_processes["srv"] = proc
    with pytest.raises(McpError):
        await gateway._call_tool_for_agent("agent", "t", {})


@pytest.mark.asyncio
async def test_call_tool_reraises_mcp_error(config_manager):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    proc = make_mock_process("srv", ["t"])
    proc.session = MagicMock()
    proc.call_tool = AsyncMock(side_effect=McpError(ErrorData(code=-31001, message="denied")))
    gateway.daemon.shared_processes["srv"] = proc
    with pytest.raises(McpError):
        await gateway._call_tool_for_agent("agent", "t", {})


@pytest.mark.asyncio
async def test_restart_unhealthy_skips_none_session(config_manager):
    gateway = make_gateway(config_manager)
    dead = make_mock_process("dead", ["t"])
    dead.session = None
    gateway.daemon.shared_processes["dead"] = dead
    gateway.daemon.stop_shared_server = AsyncMock()
    await gateway._restart_unhealthy_servers()
    gateway.daemon.stop_shared_server.assert_not_called()


@pytest.mark.asyncio
async def test_restart_unhealthy_stop_failure_is_logged(config_manager):
    gateway = make_gateway(config_manager)
    broken = make_mock_process("broken", ["t"])
    broken.session = MagicMock()
    broken.list_tools = AsyncMock(side_effect=RuntimeError("dead"))
    gateway.daemon.shared_processes["broken"] = broken
    gateway.daemon.stop_shared_server = AsyncMock(side_effect=RuntimeError("cant stop"))
    await gateway._restart_unhealthy_servers()  # must not raise


def test_check_control_token_returns_false_on_error(config_manager, monkeypatch):
    gateway = make_gateway(config_manager)
    monkeypatch.setattr("mcp_harbour.gateway.get_or_create_control_token",
                        MagicMock(side_effect=RuntimeError("keyring down")))
    assert gateway._check_control_token("anytoken") is False


# ─── serve() error branches ─────────────────────────────────────────


def _mock_bind_socket(errno):
    err = OSError()
    err.errno = errno
    sock = MagicMock()
    sock.__enter__ = lambda s: sock
    sock.__exit__ = lambda s, *a: False
    sock.bind = MagicMock(side_effect=err)
    return sock


@pytest.mark.asyncio
async def test_serve_control_token_init_error_is_logged(config_manager, monkeypatch):
    import uvicorn
    gateway = make_gateway(config_manager)
    monkeypatch.setattr(gateway, "reconcile_servers", AsyncMock())
    monkeypatch.setattr("mcp_harbour.gateway.get_or_create_control_token",
                        MagicMock(side_effect=RuntimeError("keyring")))
    monkeypatch.setattr(gateway, "create_asgi_app", lambda host, port: MagicMock())

    async def hang(interval=30.0):
        await asyncio.sleep(3600)

    monkeypatch.setattr(gateway, "_reconcile_loop", hang)
    fake = MagicMock()
    fake.serve = AsyncMock()
    monkeypatch.setattr(uvicorn, "Server", lambda config: fake)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: MagicMock())
    await gateway.serve("127.0.0.1", 0)  # token init error swallowed
    fake.serve.assert_awaited_once()


@pytest.mark.asyncio
async def test_serve_port_in_use_exits(config_manager, monkeypatch):
    gateway = make_gateway(config_manager)
    monkeypatch.setattr(gateway, "reconcile_servers", AsyncMock())
    monkeypatch.setattr("mcp_harbour.gateway.get_or_create_control_token", lambda: "t")
    monkeypatch.setattr("socket.socket", lambda *a, **k: _mock_bind_socket(98))
    with pytest.raises(SystemExit):
        await gateway.serve("127.0.0.1", 4767)


@pytest.mark.asyncio
async def test_serve_other_bind_error_reraises(config_manager, monkeypatch):
    gateway = make_gateway(config_manager)
    monkeypatch.setattr(gateway, "reconcile_servers", AsyncMock())
    monkeypatch.setattr("mcp_harbour.gateway.get_or_create_control_token", lambda: "t")
    monkeypatch.setattr("socket.socket", lambda *a, **k: _mock_bind_socket(13))
    with pytest.raises(OSError):
        await gateway.serve("127.0.0.1", 4767)


# ─── session-agent binding eviction ────────────────────────────────


@pytest.mark.asyncio
async def test_session_agent_binding_evicts_oldest(config_manager):
    from mcp_harbour.gateway import HarbourAuthenticatedStreamableHTTPApp
    from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

    gateway = make_gateway(config_manager)
    gateway._authenticate_authorization_header = MagicMock(return_value="agent")

    ids = iter(["s1", "s2"])

    async def handle_request(scope, receive, send):
        sid = next(ids)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(MCP_SESSION_ID_HEADER.encode("latin1"), sid.encode("latin1"))]})

    manager = MagicMock()
    manager.handle_request = AsyncMock(side_effect=handle_request)
    app = HarbourAuthenticatedStreamableHTTPApp(gateway, manager)
    app._max_sessions = 1

    async def recv():
        return {}

    async def snd(_):
        return None

    scope = {"headers": [(b"authorization", b"Bearer tok")], "method": "POST", "state": {}}
    await app(dict(scope), recv, snd)
    await app(dict(scope), recv, snd)

    assert list(app._session_agents.keys()) == ["s2"]  # s1 evicted
