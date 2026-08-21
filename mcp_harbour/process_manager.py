import os
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from .models import Server, ServerType
from contextlib import AsyncExitStack

logger = logging.getLogger("mcp_harbour")


@dataclass
class ServerHealth:
    state: str
    error: Optional[str] = None


class ServerProcess:
    def __init__(self, server: Server):
        self.server_config = server
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self._session_lock = anyio.Lock()
        self.tools: List = []               # cached tool list from the last start
        self.started_at: Optional[float] = None  # time.monotonic() when it went live

    async def start(self):
        logger.info(f"Starting server {self.server_config.name}...")

        try:
            if self.server_config.server_type == ServerType.http:
                await self._start_http()
            else:
                await self._start_stdio()

            logger.info(f"Connected to {self.server_config.name}. Initialized session.")

            tools = await self.session.list_tools()
            self.tools = tools.tools
            self.started_at = time.monotonic()
            logger.info(
                f"Server {self.server_config.name} provides {len(tools.tools)} tools."
            )

        except Exception as e:
            logger.error(f"Failed to start/connect to {self.server_config.name}: {e}")
            await self.stop()
            raise

    async def _start_stdio(self):
        import shlex

        parts = shlex.split(self.server_config.command)
        if not parts:
            raise ValueError(f"Invalid empty command for {self.server_config.name}")

        executable = parts[0]
        final_args = parts[1:]

        server_params = StdioServerParameters(
            command=executable,
            args=final_args,
            env={**os.environ, **self.server_config.env},
        )

        read, write = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()

    async def _start_http(self):
        url = self.server_config.url
        read, write, _ = await self.exit_stack.enter_async_context(
            streamable_http_client(url)
        )
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()

    async def stop(self):
        logger.info(f"Stopping server {self.server_config.name}...")
        await self.exit_stack.aclose()
        self.session = None
        logger.info(f"Server {self.server_config.name} stopped.")

    async def list_tools(self):
        if not self.session:
            return []
        async with self._session_lock:
            return await self.session.list_tools()

    async def call_tool(self, name: str, arguments: dict):
        if not self.session:
            raise RuntimeError(f"Server {self.server_config.name} not connected")
        async with self._session_lock:
            return await self.session.call_tool(name, arguments)


class HarbourDaemon:
    def __init__(self):
        self.shared_processes: Dict[str, ServerProcess] = {}
        self.server_health: Dict[str, ServerHealth] = {}

    async def start_shared_server(self, server: Server):
        proc = ServerProcess(server)
        try:
            await proc.start()
        except Exception as exc:
            self.shared_processes.pop(server.name, None)
            self.server_health[server.name] = ServerHealth(
                state="failed",
                error=str(exc),
            )
            raise

        self.shared_processes[server.name] = proc
        self.server_health[server.name] = ServerHealth(state="healthy")

    async def stop_shared_server(self, name: str):
        if name in self.shared_processes:
            await self.shared_processes[name].stop()
            del self.shared_processes[name]
        self.server_health.pop(name, None)

    async def stop_all_shared(self):
        for name in list(self.shared_processes.keys()):
            await self.stop_shared_server(name)

    def get_shared_process(self, name: str) -> Optional[ServerProcess]:
        return self.shared_processes.get(name)

    def get_server_health(self, name: str) -> Optional[ServerHealth]:
        return self.server_health.get(name)
