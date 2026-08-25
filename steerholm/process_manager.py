import asyncio
import contextlib
import os
import logging
import shlex
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from .models import Server, ServerType

logger = logging.getLogger("steerholm")

# A server that hasn't completed its MCP handshake within this budget is treated
# as failed, so one misbehaving server can't hang reconcile indefinitely.
CONNECT_TIMEOUT = 120  # seconds


@dataclass
class ServerHealth:
    state: str
    error: Optional[str] = None


class ServerProcess:
    """A connection to one MCP server.

    The MCP client contexts (`stdio_client` / `streamable_http_client` and
    `ClientSession`) are opened AND closed inside a single dedicated task
    (`_run`). anyio's structured concurrency requires the task that enters a
    cancel scope to be the one that exits it; opening in the reconcile task and
    closing from the shutdown task (an `AsyncExitStack.aclose()` elsewhere) raised
    "Attempted to exit cancel scope in a different task", which corrupted shutdown
    and left the daemon holding its port. `stop()` signals `_run` to unwind on its
    own task instead.
    """

    def __init__(self, server: Server):
        self.server_config = server
        self.session: Optional[ClientSession] = None
        self._session_lock = anyio.Lock()
        self.tools: List = []               # cached tool list from the last start
        self.started_at: Optional[float] = None  # time.monotonic() when it went live
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()       # set once connected OR failed
        self._error: Optional[BaseException] = None

    async def start(self):
        logger.info(f"Starting server {self.server_config.name}...")
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                f"Server {self.server_config.name} did not connect within "
                f"{CONNECT_TIMEOUT}s."
            )
            await self.stop()
            raise TimeoutError(
                f"Server {self.server_config.name} did not connect within "
                f"{CONNECT_TIMEOUT}s"
            )
        if self._error is not None:
            # _run already unwound its own contexts; just surface the failure.
            with contextlib.suppress(Exception):
                await self._task
            raise self._error

    async def _run(self):
        """Own the connection for its whole lifetime: open the contexts, stay
        alive until asked to stop, then close them here — all on this task."""
        try:
            if self.server_config.server_type == ServerType.http:
                async with streamable_http_client(self.server_config.url) as (read, write, _):
                    await self._serve_session(read, write)
            else:
                parts = shlex.split(self.server_config.command)
                if not parts:
                    raise ValueError(
                        f"Invalid empty command for {self.server_config.name}"
                    )
                params = StdioServerParameters(
                    command=parts[0],
                    args=parts[1:],
                    env={**os.environ, **self.server_config.env},
                )
                async with stdio_client(params) as (read, write):
                    await self._serve_session(read, write)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                f"Failed to start/connect to {self.server_config.name}: {e}"
            )
            self._error = e
        finally:
            self.session = None
            self._ready.set()

    async def _serve_session(self, read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            self.tools = tools.tools
            self.session = session
            self.started_at = time.monotonic()
            logger.info(
                f"Connected to {self.server_config.name}. "
                f"Provides {len(tools.tools)} tools."
            )
            self._ready.set()
            await self._stop.wait()

    async def stop(self):
        logger.info(f"Stopping server {self.server_config.name}...")
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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


class SteerholmDaemon:
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
        # Stop concurrently so one server with a slow/hung teardown can't
        # serialize the whole shutdown past the service manager's stop budget.
        await asyncio.gather(
            *(self.stop_shared_server(name)
              for name in list(self.shared_processes.keys())),
            return_exceptions=True,
        )

    def get_shared_process(self, name: str) -> Optional[ServerProcess]:
        return self.shared_processes.get(name)

    def get_server_health(self, name: str) -> Optional[ServerHealth]:
        return self.server_health.get(name)
