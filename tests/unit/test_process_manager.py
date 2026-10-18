"""Tests for process_manager command parsing and SteerholmDaemon."""

from unittest.mock import AsyncMock, patch

import pytest
from steerholm.models import Server
from steerholm.process_manager import SteerholmDaemon, ServerHealth
from tests.conftest import make_mock_process


class TestSteerholmDaemon:
    def test_init_empty(self):
        daemon = SteerholmDaemon()
        assert daemon.shared_processes == {}
        assert daemon.server_health == {}

    def test_get_shared_nonexistent(self):
        assert SteerholmDaemon().get_shared_process("nope") is None

    def test_get_server_health_nonexistent(self):
        assert SteerholmDaemon().get_server_health("nope") is None

    @pytest.mark.asyncio
    async def test_start_shared_server_records_healthy_state(self):
        daemon = SteerholmDaemon()
        server = Server(name="test", command="echo")

        with patch("steerholm.process_manager.ServerProcess.start", new=AsyncMock()) as start:
            await daemon.start_shared_server(server)

        start.assert_awaited_once()
        assert daemon.get_shared_process("test") is not None
        health = daemon.get_server_health("test")
        assert health is not None
        assert health.state == "healthy"
        assert health.error is None

    @pytest.mark.asyncio
    async def test_start_shared_server_records_failed_state(self):
        daemon = SteerholmDaemon()
        server = Server(name="broken", command="echo")

        with patch(
            "steerholm.process_manager.ServerProcess.start",
            new=AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            with pytest.raises(RuntimeError, match="connection refused"):
                await daemon.start_shared_server(server)

        assert daemon.get_shared_process("broken") is None
        health = daemon.get_server_health("broken")
        assert health is not None
        assert health.state == "failed"
        assert health.error == "connection refused"

    @pytest.mark.asyncio
    async def test_stop_shared_server_clears_health_state(self):
        daemon = SteerholmDaemon()
        proc = make_mock_process("test", ["tool"])
        daemon.shared_processes["test"] = proc
        daemon.server_health["test"] = ServerHealth(state="healthy")

        await daemon.stop_shared_server("test")

        proc.stop.assert_called_once()
        assert daemon.get_shared_process("test") is None
        assert daemon.get_server_health("test") is None

    @pytest.mark.asyncio
    async def test_stop_all_shared(self):
        daemon = SteerholmDaemon()
        proc = make_mock_process("test", ["tool"])
        daemon.shared_processes["test"] = proc
        daemon.server_health["test"] = ServerHealth(state="healthy")

        await daemon.stop_all_shared()
        proc.stop.assert_called_once()
        assert daemon.shared_processes == {}
        assert daemon.server_health == {}

    @pytest.mark.asyncio
    async def test_stop_shared_server_clears_failed_health_without_process(self):
        daemon = SteerholmDaemon()
        daemon.server_health["broken"] = ServerHealth(state="failed", error="boom")

        await daemon.stop_shared_server("broken")

        assert daemon.get_server_health("broken") is None


# ─── ServerProcess (the class the daemon tests mock out) ────────────

import pytest as _pytest
from unittest.mock import AsyncMock as _AM, MagicMock as _MM

from steerholm import process_manager as _pm
from steerholm.models import Server as _Server, ServerType as _ST


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *a):
        return False


def _session_mock(tools=()):
    s = _MM()
    s.initialize = _AM()
    result = _MM()
    result.tools = list(tools)
    s.list_tools = _AM(return_value=result)
    return s


@_pytest.mark.asyncio
async def test_serverprocess_start_stdio_success(monkeypatch):
    monkeypatch.setattr(_pm, "stdio_client", lambda params: _AsyncCM(("r", "w")))
    session = _session_mock(tools=[_MM()])
    monkeypatch.setattr(_pm, "ClientSession", lambda r, w: _AsyncCM(session))
    proc = _pm.ServerProcess(_Server(name="x", command="echo hi", server_type=_ST.stdio))
    await proc.start()
    assert proc.session is session
    assert proc.started_at is not None and len(proc.tools) == 1


@_pytest.mark.asyncio
async def test_a_stdio_server_gets_the_users_environment_plus_its_own(monkeypatch):
    # Not the frozen daemon's: its bundled OpenSSL would be loaded by anything
    # the server runs that uses the system's (git over HTTPS, Python, curl).
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI12345")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    captured = {}

    def fake_stdio_client(params):
        captured["env"] = params.env
        return _AsyncCM(("r", "w"))

    monkeypatch.setattr(_pm, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(_pm, "ClientSession", lambda r, w: _AsyncCM(_session_mock()))
    proc = _pm.ServerProcess(_Server(name="x", command="echo hi", server_type=_ST.stdio,
                                     env={"STEERVAL_TOKEN": "t"}))
    await proc.start()
    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert captured["env"]["STEERVAL_TOKEN"] == "t"


@_pytest.mark.asyncio
async def test_a_start_cancelled_midway_leaves_no_server_running(monkeypatch):
    # The daemon now starts servers behind an open port, so it can shut down
    # while one is still connecting. That server is registered nowhere yet;
    # cancelling its start must unwind the task that owns its process.
    import asyncio
    owner = {}

    async def run(self):
        owner["task"] = asyncio.current_task()
        await asyncio.Event().wait()

    monkeypatch.setattr(_pm.ServerProcess, "_run", run)
    daemon = _pm.SteerholmDaemon()
    server = _Server(name="x", command="echo hi", server_type=_ST.stdio)
    starting = asyncio.create_task(daemon.start_shared_server(server))
    await asyncio.sleep(0.01)
    assert daemon.get_server_health("x").state == "starting"

    starting.cancel()
    with _pytest.raises(asyncio.CancelledError):
        await starting
    await asyncio.sleep(0)
    assert owner["task"].cancelled()
    assert daemon.get_server_health("x") is None
    assert "x" not in daemon.shared_processes


@_pytest.mark.asyncio
async def test_serverprocess_start_http_success(monkeypatch):
    monkeypatch.setattr(_pm, "streamable_http_client", lambda url: _AsyncCM(("r", "w", None)))
    session = _session_mock()
    monkeypatch.setattr(_pm, "ClientSession", lambda r, w: _AsyncCM(session))
    proc = _pm.ServerProcess(_Server(name="x", url="http://y/mcp", server_type=_ST.http))
    await proc.start()
    assert proc.session is session


@_pytest.mark.asyncio
async def test_serverprocess_start_empty_command_raises_and_cleans_up():
    proc = _pm.ServerProcess(_Server(name="x", command="", server_type=_ST.stdio))
    with _pytest.raises(ValueError):
        await proc.start()
    assert proc.session is None  # stop() ran on the failure path


@_pytest.mark.asyncio
async def test_serverprocess_stop_clears_session():
    proc = _pm.ServerProcess(_Server(name="x", command="echo", server_type=_ST.stdio))
    proc.session = _MM()
    await proc.stop()
    assert proc.session is None


@_pytest.mark.asyncio
async def test_serverprocess_list_tools_empty_without_session():
    proc = _pm.ServerProcess(_Server(name="x", command="echo", server_type=_ST.stdio))
    proc.session = None
    assert await proc.list_tools() == []


@_pytest.mark.asyncio
async def test_serverprocess_call_tool_raises_without_session():
    proc = _pm.ServerProcess(_Server(name="x", command="echo", server_type=_ST.stdio))
    proc.session = None
    with _pytest.raises(RuntimeError):
        await proc.call_tool("t", {})


@_pytest.mark.asyncio
async def test_serverprocess_list_tools_with_session():
    proc = _pm.ServerProcess(_Server(name="x", command="echo", server_type=_ST.stdio))
    proc.session = _MM()
    proc.session.list_tools = _AM(return_value="TOOLS")
    assert await proc.list_tools() == "TOOLS"


@_pytest.mark.asyncio
async def test_serverprocess_call_tool_with_session():
    proc = _pm.ServerProcess(_Server(name="x", command="echo", server_type=_ST.stdio))
    proc.session = _MM()
    proc.session.call_tool = _AM(return_value="RES")
    assert await proc.call_tool("t", {"a": 1}) == "RES"
    proc.session.call_tool.assert_awaited_once_with("t", {"a": 1})
