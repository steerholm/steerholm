"""Tests for shared gateway tool listing, tool calling, and process lifecycle."""

from unittest.mock import AsyncMock, call

import pytest

from mcp_harbour.process_manager import ServerHealth
from tests.conftest import call_tool, get_tools, make_gateway, make_mock_process


class TestHTTPDownstreamFixtures:
    @pytest.mark.asyncio
    async def test_http_downstream_visible_tools_are_filtered(self, setup_http_downstream, http_get_tools):
        setup_http_downstream(
            tool_names=["echo_http", "secret_http"],
            allowed_tools=["echo_http"],
        )

        tools = await http_get_tools()

        assert [tool.name for tool in tools] == ["echo_http"]
        # Routing / denial / arg-policy for HTTP-typed servers run through the same
        # transport-agnostic gateway code as the stdio tests below, so one filtering
        # smoke here is enough; see TestToolCalls / TestDefaultDeny for the rest.


def create_admin_policy(config_manager, servers=None):
    if servers is None:
        servers = ["test-server"]
    for s in servers:
        config_manager.grant_permission("admin", s, tool="*")


class TestSharedProcesses:
    @pytest.mark.asyncio
    async def test_stdio_server_starts_as_shared_process(self, config_manager, sample_server):
        gateway = make_gateway(config_manager)
        gateway.daemon.start_shared_server = AsyncMock()

        await gateway.start_shared_processes()

        gateway.daemon.start_shared_server.assert_awaited_once_with(sample_server)

    @pytest.mark.asyncio
    async def test_http_server_starts_as_shared_process(self, config_manager, sample_http_server):
        gateway = make_gateway(config_manager)
        gateway.daemon.start_shared_server = AsyncMock()

        await gateway.start_shared_processes()

        gateway.daemon.start_shared_server.assert_awaited_once_with(sample_http_server)

    @pytest.mark.asyncio
    async def test_failed_shared_server_does_not_block_healthy_server(self, config_manager):
        healthy = config_manager.add_server("healthy-server", command="echo")
        broken = config_manager.add_server("broken-server", command="bad")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "healthy-server", tool="*")
        config_manager.grant_permission("agent", "broken-server", tool="*")

        gateway = make_gateway(config_manager)

        async def start_side_effect(server):
            if server.name == "broken-server":
                gateway.daemon.server_health[server.name] = ServerHealth("failed", "connection refused")
                raise RuntimeError("connection refused")

            gateway.daemon.shared_processes[server.name] = make_mock_process(server.name, ["read_file"])
            gateway.daemon.server_health[server.name] = ServerHealth("healthy")

        gateway.daemon.start_shared_server = AsyncMock(side_effect=start_side_effect)

        await gateway.start_shared_processes()

        tools = await get_tools(gateway.session_server)

        assert {tool.name for tool in tools} == {"read_file"}
        assert gateway.daemon.get_server_health("healthy-server").state == "healthy"
        assert gateway.daemon.get_server_health("broken-server").state == "failed"
        assert gateway.daemon.get_server_health("broken-server").error == "connection refused"
        assert gateway.daemon.get_shared_process("healthy-server") is not None
        assert gateway.daemon.get_shared_process("broken-server") is None
        gateway.daemon.start_shared_server.assert_has_awaits([
            call(healthy),
            call(broken),
        ], any_order=False)

    @pytest.mark.asyncio
    async def test_reconcile_applies_dock_and_undock_live(self, config_manager):
        # A server docked or undocked while the daemon runs must take effect on the
        # next reconcile — without a restart, and without churning already-running,
        # unchanged servers.
        config_manager.add_server("A", command="echo")
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
        assert set(gateway.daemon.shared_processes) == {"A"}
        assert gateway.daemon.start_shared_server.await_count == 1

        # Dock B while running -> B starts, A is left running (not restarted).
        config_manager.add_server("B", command="echo")
        result = await gateway.reconcile_servers()
        assert set(gateway.daemon.shared_processes) == {"A", "B"}
        assert result["started"] == ["B"]
        assert gateway.daemon.start_shared_server.await_count == 2

        # Remove A while running -> A stops live.
        config_manager.remove_server("A")
        result = await gateway.reconcile_servers()
        assert set(gateway.daemon.shared_processes) == {"B"}
        assert result["stopped"] == ["A"]

    @pytest.mark.asyncio
    async def test_server_status_reports_state_uptime_and_tools(self, config_manager):
        import time as _time
        from unittest.mock import MagicMock
        from mcp.types import Tool

        config_manager.add_server("running-srv", command="echo")
        config_manager.add_server("stopped-srv", command="echo")
        config_manager.add_server("failed-srv", command="bad")
        gateway = make_gateway(config_manager)

        proc = make_mock_process("running-srv", ["echo", "add"])
        proc.session = MagicMock()
        proc.started_at = _time.monotonic() - 5
        proc.tools = [
            Tool(name="echo", description="Echo it back", inputSchema={"type": "object", "properties": {}}),
            Tool(name="add", description="Add numbers", inputSchema={"type": "object", "properties": {}}),
        ]
        gateway.daemon.shared_processes["running-srv"] = proc
        gateway.daemon.server_health["running-srv"] = ServerHealth("healthy")
        gateway.daemon.server_health["failed-srv"] = ServerHealth("failed", "boom")

        status = gateway.server_status()

        assert status["running-srv"]["state"] == "running"
        assert status["running-srv"]["uptime_seconds"] >= 5
        assert [t["name"] for t in status["running-srv"]["tools"]] == ["echo", "add"]
        assert status["stopped-srv"]["state"] == "stopped"
        assert status["failed-srv"]["state"] == "failed"
        assert status["failed-srv"]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_unhealthy_server_is_dropped_for_restart(self, config_manager):
        from unittest.mock import MagicMock

        config_manager.add_server("healthy", command="echo")
        config_manager.add_server("broken", command="echo")
        gateway = make_gateway(config_manager)

        healthy = make_mock_process("healthy", ["t"])
        healthy.session = MagicMock()  # list_tools succeeds (mock default)
        broken = make_mock_process("broken", ["t"])
        broken.session = MagicMock()
        broken.list_tools = AsyncMock(side_effect=RuntimeError("broken pipe"))
        gateway.daemon.shared_processes["healthy"] = healthy
        gateway.daemon.shared_processes["broken"] = broken

        stopped = []

        async def stop_side_effect(name):
            gateway.daemon.shared_processes.pop(name, None)
            stopped.append(name)

        gateway.daemon.stop_shared_server = AsyncMock(side_effect=stop_side_effect)

        await gateway._restart_unhealthy_servers()

        # Only the server that failed its probe is dropped; the healthy one stays.
        assert stopped == ["broken"]
        assert "healthy" in gateway.daemon.shared_processes
        assert "broken" not in gateway.daemon.shared_processes


class TestToolDiscovery:
    @pytest.mark.asyncio
    async def test_single_server(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="*")

        gateway = make_gateway(config_manager)
        gateway.daemon.shared_processes["test-server"] = make_mock_process(
            "test-server", ["read_file", "write_file", "list_dir"]
        )

        tools = await get_tools(gateway.session_server)

        assert len(tools) == 3
        assert {t.name for t in tools} == {"read_file", "write_file", "list_dir"}

    @pytest.mark.asyncio
    async def test_multiple_servers(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_server("git", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="*")
        config_manager.grant_permission("agent", "git", tool="*")

        gateway = make_gateway(config_manager)
        gateway.daemon.shared_processes["test-server"] = make_mock_process(
            "test-server", ["read_file", "write_file"]
        )
        gateway.daemon.shared_processes["git"] = make_mock_process(
            "git", ["git_status", "git_log"]
        )

        tools = await get_tools(gateway.session_server)

        assert {t.name for t in tools} == {"read_file", "write_file", "git_status", "git_log"}

    @pytest.mark.asyncio
    async def test_filtered_by_exact_tool_name(self, config_manager, sample_server):
        config_manager.add_agent("reader")
        config_manager.grant_permission("reader", "test-server", tool="read_file")

        gateway = make_gateway(config_manager)
        gateway.daemon.shared_processes["test-server"] = make_mock_process(
            "test-server", ["read_file", "write_file", "delete_file"]
        )

        tool_names = [t.name for t in await get_tools(gateway.session_server, "reader")]

        assert "read_file" in tool_names
        assert "write_file" not in tool_names

    @pytest.mark.asyncio
    async def test_filtered_by_glob(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="read_*")

        gateway = make_gateway(config_manager)
        gateway.daemon.shared_processes["test-server"] = make_mock_process(
            "test-server", ["read_file", "read_dir", "write_file", "delete_file"]
        )

        assert {t.name for t in await get_tools(gateway.session_server)} == {"read_file", "read_dir"}

    @pytest.mark.asyncio
    async def test_server_not_in_policy_skipped(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_server("bash", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="*")

        gateway = make_gateway(config_manager)
        test_proc = make_mock_process("test-server", ["read_file"])
        bash_proc = make_mock_process("bash", ["run_command"])
        gateway.daemon.shared_processes["test-server"] = test_proc
        gateway.daemon.shared_processes["bash"] = bash_proc

        tools = await get_tools(gateway.session_server)

        assert {t.name for t in tools} == {"read_file"}
        bash_proc.list_tools.assert_not_awaited()


class TestDefaultDeny:
    @pytest.mark.asyncio
    async def test_no_policy(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("unknown-agent")

        gateway = make_gateway(config_manager)
        gateway.daemon.shared_processes["test-server"] = make_mock_process(
            "test-server", ["read_file"]
        )

        assert len(await get_tools(gateway.session_server, "unknown-agent")) == 0

    @pytest.mark.asyncio
    async def test_empty_policy(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("empty-agent")
        config_manager.create_policy("empty-agent")

        gateway = make_gateway(config_manager)
        proc = make_mock_process("test-server", ["read_file"])
        gateway.daemon.shared_processes["test-server"] = proc

        assert len(await get_tools(gateway.session_server, "empty-agent")) == 0
        proc.list_tools.assert_not_awaited()


class TestToolCalls:
    @pytest.mark.asyncio
    async def test_routes_to_correct_server(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_server("git", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="*")
        config_manager.grant_permission("agent", "git", tool="*")

        gateway = make_gateway(config_manager)
        fs_proc = make_mock_process("test-server", ["read_file"])
        git_proc = make_mock_process("git", ["git_status"])
        gateway.daemon.shared_processes["test-server"] = fs_proc
        gateway.daemon.shared_processes["git"] = git_proc

        await call_tool(gateway.session_server, "read_file", {"path": "/tmp/test"})
        fs_proc.call_tool.assert_called_once_with("read_file", {"path": "/tmp/test"})
        git_proc.call_tool.assert_not_called()

        await call_tool(gateway.session_server, "git_status")
        git_proc.call_tool.assert_called_once_with("git_status", {})

    @pytest.mark.asyncio
    async def test_argument_policy_allowed(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission(
            "agent", "test-server", tool="read_file", arg_policies=["path=/home/user/**"]
        )

        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file"])
        gateway.daemon.shared_processes["test-server"] = mock_proc

        await call_tool(gateway.session_server, "read_file", {"path": "/home/user/project/main.py"})
        mock_proc.call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_argument_policy_denied(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission(
            "agent", "test-server", tool="read_file", arg_policies=["path=/home/user/**"]
        )

        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file"])
        gateway.daemon.shared_processes["test-server"] = mock_proc

        result = await call_tool(gateway.session_server, "read_file", {"path": "/etc/shadow"})

        assert result.root.isError is True
        mock_proc.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_tool_returns_error(self, config_manager, sample_server):
        config_manager.add_agent("readonly")
        config_manager.grant_permission("readonly", "test-server", tool="read_file")

        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file", "write_file"])
        gateway.daemon.shared_processes["test-server"] = mock_proc

        result = await call_tool(
            gateway.session_server,
            "write_file",
            {"path": "/etc/passwd", "content": "x"},
            "readonly",
        )

        mock_proc.call_tool.assert_not_called()
        assert result.root.isError is True
        assert "not allowed" in result.root.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="read_file")

        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file"])
        gateway.daemon.shared_processes["test-server"] = mock_proc

        result = await call_tool(gateway.session_server, "nonexistent_tool")

        assert result.root.isError is True
        mock_proc.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_server(self, config_manager):
        config_manager.add_server("test-server", command="echo")
        config_manager.add_agent("agent")
        config_manager.grant_permission("agent", "test-server", tool="*")

        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file"])
        mock_proc.session = None
        gateway.daemon.shared_processes["test-server"] = mock_proc

        result = await call_tool(gateway.session_server, "read_file")

        assert result.root.isError is True


class TestProcessLifecycle:
    @pytest.mark.asyncio
    async def test_shared_processes_can_be_stopped(self, config_manager, sample_server):
        gateway = make_gateway(config_manager)
        mock_proc = make_mock_process("test-server", ["read_file"])
        gateway.daemon.shared_processes["test-server"] = mock_proc

        await gateway.daemon.stop_all_shared()

        mock_proc.stop.assert_awaited_once()
        assert "test-server" not in gateway.daemon.shared_processes

    @pytest.mark.asyncio
    async def test_multiple_agents_reuse_same_process(self, config_manager, sample_server):
        config_manager.add_agent("admin")
        config_manager.add_agent("reader")
        config_manager.grant_permission("admin", "test-server", tool="*")
        config_manager.grant_permission("reader", "test-server", tool="read_file")

        gateway = make_gateway(config_manager)
        proc = make_mock_process("test-server", ["read_file", "write_file"])
        gateway.daemon.shared_processes["test-server"] = proc

        await get_tools(gateway.session_server, "admin")
        await get_tools(gateway.session_server, "reader")

        assert gateway.daemon.shared_processes["test-server"] is proc
        assert proc.list_tools.await_count == 2
