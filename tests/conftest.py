import asyncio
import os
import pytest
from collections import OrderedDict

# Never let the CLI's "update available" check hit the network during tests.
os.environ.setdefault("MCP_HARBOUR_NO_UPDATE_CHECK", "1")
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

try:
    import allure
except ImportError:  # allure-pytest not installed
    allure = None


@pytest.fixture(autouse=True)
def _allure_grouping(request):
    """Group Allure results by OS (parentSuite) then test layer (suite) so the
    report reads OS -> Unit/Integration/E2E. parentSuite comes from ALLURE_OS
    (set per matrix leg in CI); suite is derived from the test's directory."""
    if allure is None:
        return
    os_label = os.environ.get("ALLURE_OS")
    if os_label:
        allure.dynamic.parent_suite(os_label)
    path = str(request.node.fspath).replace("\\", "/")
    for marker, suite in (("/unit/", "Unit"), ("/integration/", "Integration"), ("/e2e/", "E2E")):
        if marker in path:
            allure.dynamic.suite(suite)
            break

from mcp.server import Server as MCPServer
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from mcp.types import Tool, ListToolsResult, CallToolResult, TextContent, ListToolsRequest, CallToolRequest, CallToolRequestParams

from mcp_harbour.gateway import HarbourGateway
from mcp_harbour.models import Server
from mcp_harbour.process_manager import HarbourDaemon, ServerProcess


class HTTPDownstreamFixture:
    def __init__(self, server_name: str, tool_names: list[str], result_prefix: str = "http"):
        self.server_name = server_name
        self.tool_names = tool_names
        self.result_prefix = result_prefix
        self.calls: list[tuple[str, dict]] = []
        self.session = MagicMock()

    async def list_tools(self):
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description=f"Mock {name}",
                    inputSchema={"type": "object", "properties": {}},
                )
                for name in self.tool_names
            ]
        )

    async def call_tool(self, name: str, arguments: dict):
        payload = arguments or {}
        self.calls.append((name, payload))
        return CallToolResult(
            content=[TextContent(type="text", text=f"{self.result_prefix}:{payload.get('message', '')}")]
        )


@pytest.fixture
def make_http_process():
    def factory(server_name: str, tool_names: list[str], result_prefix: str = "http"):
        proc = MagicMock(spec=ServerProcess)
        proc.server_config = Server(name=server_name, url="http://localhost:3001/mcp")
        fixture = HTTPDownstreamFixture(server_name, tool_names, result_prefix)
        proc.session = fixture.session
        proc.list_tools = AsyncMock(side_effect=fixture.list_tools)
        proc.call_tool = AsyncMock(side_effect=fixture.call_tool)
        proc.stop = AsyncMock()
        proc.http_fixture = fixture
        return proc

    return factory


@pytest.fixture
def http_gateway(config_manager):
    return make_gateway(config_manager)


@pytest.fixture
def attach_process(http_gateway):
    def factory(server_name: str, process: ServerProcess):
        http_gateway.daemon.shared_processes[server_name] = process
        return process

    return factory


@pytest.fixture
def http_agent(config_manager):
    config_manager.add_agent("agent")
    return "agent"


@pytest.fixture
def grant_http_permission(config_manager, http_agent):
    def factory(server_name: str, tool: str, *, arg_policies: list[str] | None = None):
        config_manager.grant_permission(
            http_agent,
            server_name,
            tool=tool,
            arg_policies=arg_policies,
        )

    return factory


@pytest.fixture
def setup_http_downstream(config_manager, http_gateway, make_http_process, attach_process, grant_http_permission):
    def factory(
        *,
        server_name: str = "downstream-http",
        tool_names: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        arg_policy_by_tool: dict[str, list[str]] | None = None,
        result_prefix: str = "http",
    ):
        tool_names = tool_names or ["echo_http", "secret_http"]
        allowed_tools = allowed_tools or [tool_names[0]]
        arg_policy_by_tool = arg_policy_by_tool or {}

        config_manager.add_server(server_name, url="http://localhost:3001/mcp")
        process = make_http_process(server_name, tool_names, result_prefix)
        attach_process(server_name, process)

        for tool in allowed_tools:
            grant_http_permission(
                server_name,
                tool,
                arg_policies=arg_policy_by_tool.get(tool),
            )

        return http_gateway, process.http_fixture

    return factory


@pytest.fixture
def http_get_tools(http_gateway, http_agent):
    async def factory():
        return await get_tools(http_gateway.session_server, http_agent)

    return factory


@pytest.fixture
def http_call_tool(http_gateway, http_agent):
    async def factory(name: str, arguments: dict | None = None):
        return await call_tool(http_gateway.session_server, name, arguments, http_agent)

    return factory


@pytest.fixture
def tmp_config_dir(tmp_path):
    config_dir = tmp_path / ".mcp-harbour"
    config_dir.mkdir()
    (config_dir / "policies").mkdir()
    return config_dir


@pytest.fixture
def config_manager(tmp_config_dir, monkeypatch):
    import mcp_harbour.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_config_dir)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_config_dir / "config.json")
    monkeypatch.setattr(config_mod, "POLICIES_DIR", tmp_config_dir / "policies")
    monkeypatch.setattr(config_mod, "DEFAULT_HOST", "127.0.0.1")
    monkeypatch.setattr(config_mod, "DEFAULT_PORT", 0)

    from mcp_harbour.config import ConfigManager

    return ConfigManager()


@pytest.fixture
def sample_server(config_manager):
    return config_manager.add_server("test-server", command="echo")


@pytest.fixture
def sample_http_server(config_manager):
    return config_manager.add_server("test-http-server", url="http://localhost:3001/mcp")


def make_gateway(config_manager) -> HarbourGateway:
    """Create a gateway with mocked internals pointing at the test config."""
    gateway = HarbourGateway.__new__(HarbourGateway)
    gateway.config_manager = config_manager
    gateway.daemon = HarbourDaemon()
    gateway.session_server = MCPServer("mcp-harbour")
    gateway._auth_cache = OrderedDict()
    gateway._auth_cache_max = 4096
    gateway._reconcile_lock = asyncio.Lock()
    gateway._register_handlers()
    return gateway


def _set_request_agent(agent_name: str):
    request = SimpleNamespace(
        state=SimpleNamespace(harbour_agent=agent_name)
    )
    return request_ctx.set(
        RequestContext(
            request_id="test",
            meta=None,
            session=MagicMock(),
            lifespan_context=None,
            request=request,
        )
    )


async def get_tools(session_server, agent_name: str = "agent") -> list:
    """Call list_tools on a session server and return the tool list."""
    token = _set_request_agent(agent_name)
    try:
        result = await session_server.request_handlers[ListToolsRequest](MagicMock())
        return result.root.tools
    finally:
        request_ctx.reset(token)


async def call_tool(session_server, name: str, arguments: dict = None, agent_name: str = "agent"):
    """Call a tool on a session server and return the raw handler result."""
    token = _set_request_agent(agent_name)
    try:
        handler = session_server.request_handlers[CallToolRequest]
        request = MagicMock()
        request.params = CallToolRequestParams(name=name, arguments=arguments or {})
        return await handler(request)
    finally:
        request_ctx.reset(token)


def make_mock_process(server_name: str, tool_names: list[str]) -> ServerProcess:
    """Create a mock ServerProcess with fake tools. Shared across integration tests."""
    proc = MagicMock(spec=ServerProcess)
    proc.server_config = Server(name=server_name, command="echo")
    proc.session = MagicMock()

    mock_tools = [
        Tool(
            name=name,
            description=f"Mock {name}",
            inputSchema={"type": "object", "properties": {}},
        )
        for name in tool_names
    ]

    proc.list_tools = AsyncMock(return_value=ListToolsResult(tools=mock_tools))
    proc.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text=f"result from {server_name}")]
        )
    )
    proc.stop = AsyncMock()

    return proc
