"""The gateway emits a DecisionEvent for every tool call (Mission Control M1)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import call_tool, make_gateway, make_mock_process


def _wire(config_manager, *, granted="read_file", tools=("read_file", "write_file")):
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool=granted)
    gateway = make_gateway(config_manager)
    gateway.daemon.shared_processes["srv"] = make_mock_process("srv", list(tools))
    return gateway


@pytest.mark.asyncio
async def test_allowed_call_emits_allowed_event(config_manager):
    gateway = _wire(config_manager)
    await call_tool(gateway.session_server, "read_file", {"path": "/x"}, "agent")

    events = gateway.event_log.recent()
    assert len(events) == 1
    e = events[0]
    assert (e.decision, e.result) == ("allowed", "ok")
    assert (e.agent, e.tool, e.server) == ("agent", "read_file", "srv")
    assert "path=/x" in e.args_summary
    assert e.latency_ms is not None and e.latency_ms >= 0


@pytest.mark.asyncio
async def test_denied_not_allowed_tool_emits_denied_event(config_manager):
    gateway = _wire(config_manager)
    await call_tool(gateway.session_server, "write_file", {"path": "/etc/passwd"}, "agent")

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.decision == "denied"
    assert e.tool == "write_file"
    assert e.server == "srv"
    assert "not allowed" in (e.reason or "").lower()


@pytest.mark.asyncio
async def test_denied_tool_not_found_emits_event_with_no_server(config_manager):
    gateway = _wire(config_manager)
    await call_tool(gateway.session_server, "nonexistent", {}, "agent")

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.decision == "denied"
    assert e.tool == "nonexistent"
    assert e.server is None
    assert "not found" in (e.reason or "").lower()


@pytest.mark.asyncio
async def test_event_records_arguments_faithfully(config_manager):
    # By design the audit records real argument values (no scrubbing); see events.py.
    gateway = _wire(config_manager, granted="connect", tools=("connect",))
    await call_tool(
        gateway.session_server, "connect",
        {"host": "db", "password": "hunter2"}, "agent",
    )

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.decision == "allowed"
    assert "host=db" in e.args_summary
    assert "password=hunter2" in e.args_summary   # faithful, not masked


@pytest.mark.asyncio
async def test_event_carries_agent_id(config_manager):
    gateway = _wire(config_manager)
    await call_tool(gateway.session_server, "read_file", {"path": "/x"}, "agent")

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.agent == "agent"
    assert e.agent_id == config_manager.get_agent("agent").id
    assert e.agent_id.startswith("agt_")


@pytest.mark.asyncio
async def test_recreated_same_name_stays_distinct_by_id(config_manager):
    # The reason the id exists: delete + recreate the same name is a different
    # principal, so per-agent filtering by id must not conflate the two.
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="read_file")
    gateway = make_gateway(config_manager)
    gateway.daemon.shared_processes["srv"] = make_mock_process("srv", ["read_file"])

    await call_tool(gateway.session_server, "read_file", {"path": "/a"}, "agent")
    first_id = gateway.event_log.recent()[-1].agent_id

    config_manager.remove_agent("agent")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="read_file")

    await call_tool(gateway.session_server, "read_file", {"path": "/b"}, "agent")
    second_id = gateway.event_log.recent()[-1].agent_id

    assert first_id and second_id and first_id != second_id
    events = gateway.event_log.recent()
    assert {e.agent for e in events} == {"agent"}          # same display name
    assert {e.agent_id for e in events} == {first_id, second_id}  # distinct principals


@pytest.mark.asyncio
async def test_event_is_written_to_the_jsonl_log(config_manager):
    import json
    from steerholm.events import DecisionEvent

    gateway = _wire(config_manager)
    await call_tool(gateway.session_server, "read_file", {"path": "/x"}, "agent")

    lines = gateway.event_log.path.read_text().strip().splitlines()
    assert len(lines) == 1
    persisted = DecisionEvent(**json.loads(lines[0]))
    assert (persisted.tool, persisted.decision) == ("read_file", "allowed")


@pytest.mark.asyncio
async def test_server_unavailable_records_error_not_denied(config_manager, monkeypatch):
    from mcp.shared.exceptions import McpError
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    # Resolve succeeds but the process is gone -> server_unavailable BEFORE the
    # permission verdict; that must not be logged as a policy "denied".
    monkeypatch.setattr(gateway, "_resolve_tool_server", AsyncMock(return_value="srv"))

    with pytest.raises(McpError):
        await gateway._call_tool_for_agent("agent", "t", {})

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.decision == "error"          # infra failure, not a policy denial
    assert e.result == "error"
    assert "not running" in (e.reason or "")


@pytest.mark.asyncio
async def test_allowed_but_downstream_error_stays_allowed(config_manager):
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

    events = gateway.event_log.recent()
    assert len(events) == 1          # exactly one event per call
    e = events[0]
    assert e.decision == "allowed"   # policy permitted it (a downstream error is not a denial)
    assert e.result == "error"       # but the tool call failed


@pytest.mark.asyncio
async def test_cancelled_call_records_no_event(config_manager):
    config_manager.add_server("srv", command="echo")
    config_manager.add_agent("agent")
    config_manager.grant_permission("agent", "srv", tool="*")
    gateway = make_gateway(config_manager)
    proc = make_mock_process("srv", ["t"])
    proc.session = MagicMock()
    proc.call_tool = AsyncMock(side_effect=asyncio.CancelledError())
    gateway.daemon.shared_processes["srv"] = proc

    with pytest.raises(asyncio.CancelledError):
        await gateway._call_tool_for_agent("agent", "t", {})

    # A cancelled call was never adjudicated -> no phantom event recorded.
    assert gateway.event_log.recent() == []
