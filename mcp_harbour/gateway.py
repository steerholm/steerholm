import asyncio
import hashlib
import hmac
import logging
import socket
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from http import HTTPStatus
from typing import List, Optional

import bcrypt
import keyring
import mcp.types as types
from mcp.server import Server
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .config import ConfigManager, get_or_create_control_token
from .errors import authorization_denied, server_unavailable
from .models import AgentPolicy
from .permissions import PermissionEngine
from .process_manager import HarbourDaemon

logger = logging.getLogger("mcp_harbour")


class HarbourAuthenticatedStreamableHTTPApp:
    def __init__(self, gateway: "HarbourGateway", manager: StreamableHTTPSessionManager):
        self.gateway = gateway
        self.manager = manager
        # Bounded: clients that disconnect without a DELETE never remove their
        # entry, so cap it (LRU) to avoid unbounded growth over the daemon's life.
        self._session_identities: "OrderedDict[str, str]" = OrderedDict()
        self._max_sessions = 4096

    async def __call__(self, scope, receive, send):
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        identity_name = self.gateway._authenticate_authorization_header(
            headers.get("authorization")
        )
        if not identity_name:
            await self._send_auth_error(scope, receive, send)
            return

        request_session_id = headers.get(MCP_SESSION_ID_HEADER)
        if request_session_id:
            bound_identity = self._session_identities.get(request_session_id)
            if bound_identity and bound_identity != identity_name:
                await self._send_auth_error(scope, receive, send)
                return

        response_session_id = None

        async def send_with_session_binding(message):
            nonlocal response_session_id
            if message["type"] == "http.response.start":
                response_headers = {
                    key.decode("latin1").lower(): value.decode("latin1")
                    for key, value in message.get("headers", [])
                }
                response_session_id = response_headers.get(MCP_SESSION_ID_HEADER)
            await send(message)

        scope.setdefault("state", {})["harbour_identity"] = identity_name
        await self.manager.handle_request(scope, receive, send_with_session_binding)

        if response_session_id:
            self._session_identities[response_session_id] = identity_name
            self._session_identities.move_to_end(response_session_id)
            while len(self._session_identities) > self._max_sessions:
                self._session_identities.popitem(last=False)
        if scope.get("method") == "DELETE" and request_session_id:
            self._session_identities.pop(request_session_id, None)

    async def _send_auth_error(self, scope, receive, send) -> None:
        response = JSONResponse(
            {"error": "Unauthorized"},
            status_code=HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


class HarbourGateway:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.daemon = HarbourDaemon()
        self.session_server = Server("mcp-harbour")
        # token sha256 -> (identity, stored key hash). Lets repeat requests skip the
        # per-request O(identities) bcrypt; invalidated when the stored hash changes.
        self._auth_cache: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._auth_cache_max = 4096
        # Serializes lifecycle reconciliation (startup, control plane, supervisor).
        self._reconcile_lock = asyncio.Lock()
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.session_server.list_tools()
        async def list_tools() -> List[Tool]:
            return await self._list_allowed_tools(self._current_identity_name())

        @self.session_server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
            return await self._call_tool_for_identity(
                self._current_identity_name(), name, arguments
            )

    def _current_identity_name(self) -> str:
        try:
            request = self.session_server.request_context.request
            identity_name = request.state.harbour_identity if request else None
        except (AttributeError, LookupError):
            identity_name = None
        if not identity_name:
            raise authorization_denied("Missing authenticated identity.")
        return identity_name

    def _resolve_identity_from_token(self, token: str) -> Optional[str]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        cached = self._auth_cache.get(token_hash)
        if cached is not None:
            name, cached_key = cached
            try:
                current_key = keyring.get_password("mcp-harbour", name)
            except Exception:
                current_key = None
            # Trust the cache only while the identity is still in config AND its
            # stored hash is unchanged, so a removed or rotated key invalidates the
            # cached token at once (config membership is the authoritative source,
            # matching the miss-loop below).
            if (
                current_key is not None
                and current_key == cached_key
                and name in self.config_manager.config.identities
            ):
                self._auth_cache.move_to_end(token_hash)
                return name
            self._auth_cache.pop(token_hash, None)

        for name in self.config_manager.config.identities:
            try:
                hashed_key = keyring.get_password("mcp-harbour", name)
                if hashed_key and bcrypt.checkpw(token.encode(), hashed_key.encode()):
                    self._auth_cache[token_hash] = (name, hashed_key)
                    self._auth_cache.move_to_end(token_hash)
                    while len(self._auth_cache) > self._auth_cache_max:
                        self._auth_cache.popitem(last=False)
                    return name
            except Exception as e:
                logger.error(f"Keyring error checking identity '{name}': {e}")
        return None

    def _extract_bearer_token(self, authorization: Optional[str]) -> Optional[str]:
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()

    def _authenticate_authorization_header(self, authorization: Optional[str]) -> Optional[str]:
        token = self._extract_bearer_token(authorization)
        if not token:
            return None
        self.config_manager.reload()
        return self._resolve_identity_from_token(token)

    def _load_identity_policy(self, identity_name: str) -> AgentPolicy:
        policy = self.config_manager.load_policy(identity_name)
        if not policy:
            return AgentPolicy(identity_name=identity_name, permissions={})
        return policy

    def _iter_accessible_processes(self, policy: AgentPolicy):
        for server_name in policy.permissions:
            process = self.daemon.get_shared_process(server_name)
            if process and process.session:
                yield server_name, process

    async def _list_allowed_tools(self, identity_name: str) -> List[Tool]:
        policy = self._load_identity_policy(identity_name)
        all_tools = []

        for server_name, process in self._iter_accessible_processes(policy):
            try:
                ship_tools = await process.list_tools()
                for tool in ship_tools.tools:
                    for perm in policy.permissions.get(server_name, []):
                        if fnmatch(tool.name, perm.name):
                            all_tools.append(tool)
                            break
            except Exception as e:
                logger.error(f"Error listing tools from {server_name}: {e}")

        return all_tools

    async def _resolve_tool_server(self, identity_name: str, tool_name: str) -> Optional[str]:
        policy = self._load_identity_policy(identity_name)

        for server_name, process in self._iter_accessible_processes(policy):
            try:
                ship_tools = await process.list_tools()
                for tool in ship_tools.tools:
                    if tool.name == tool_name:
                        return server_name
            except Exception as e:
                logger.error(f"Error listing tools from {server_name}: {e}")

        return None

    async def _call_tool_for_identity(
        self, identity_name: str, name: str, arguments: dict
    ) -> types.CallToolResult:
        policy = self._load_identity_policy(identity_name)
        engine = PermissionEngine(policy)
        server_name = await self._resolve_tool_server(identity_name, name)

        if not server_name:
            raise authorization_denied(f"Tool '{name}' not found on any docked server.")

        process = self.daemon.get_shared_process(server_name)
        if not process or not process.session:
            raise server_unavailable(server_name)

        engine.check_permission(server_name, name, arguments)

        logger.info(f"Routing tool '{name}' to server '{server_name}'")
        try:
            result = await process.call_tool(name, arguments)
            return result
        except Exception as e:
            if hasattr(e, "error"):
                raise
            logger.error(f"Error calling tool '{name}' on '{server_name}': {e}")
            raise server_unavailable(server_name)

    async def reconcile_servers(self) -> dict:
        """The daemon's single lifecycle primitive: make the running shared servers
        match the docked config — start newly docked servers, stop undocked ones,
        and restart servers whose spec changed or whose session died. Idempotent;
        driven at startup, by the control plane on config changes, and periodically.
        """
        async with self._reconcile_lock:
            desired = {s.name: s for s in self.config_manager.list_servers()}
            started, stopped, failed = [], [], []

            async def _stop(name: str) -> bool:
                try:
                    await self.daemon.stop_shared_server(name)
                    return True
                except Exception as e:
                    logger.error(f"Failed to stop server '{name}': {e}")
                    return False

            for name in list(self.daemon.shared_processes.keys()):
                if name not in desired:
                    if await _stop(name):
                        stopped.append(name)
                    else:
                        failed.append(name)

            for name, server in desired.items():
                proc = self.daemon.get_shared_process(name)
                if proc is not None and proc.session is not None and proc.server_config == server:
                    continue  # already running with the current spec
                if proc is not None:
                    await _stop(name)  # changed or dead -> restart
                try:
                    await self.daemon.start_shared_server(server)
                    started.append(name)
                except Exception as e:
                    logger.error(f"Failed to start docked server '{name}': {e}")
                    failed.append(name)

            return {
                "started": started,
                "stopped": stopped,
                "failed": failed,
                "running": sorted(self.daemon.shared_processes.keys()),
            }

    async def start_shared_processes(self):
        # Backward-compatible name for the reconcile primitive.
        await self.reconcile_servers()

    def server_status(self) -> dict:
        """Live per-server status for the control plane / CLI: state, uptime, error,
        and the cached tool list. Reads in-memory daemon state — no network calls."""
        now = time.monotonic()
        result = {}
        for server in self.config_manager.list_servers():
            proc = self.daemon.get_shared_process(server.name)
            health = self.daemon.get_server_health(server.name)
            if proc is not None and proc.session is not None:
                result[server.name] = {
                    "state": "running",
                    "uptime_seconds": round(now - proc.started_at, 1) if proc.started_at else None,
                    "error": None,
                    "tools": [
                        {"name": t.name, "description": t.description} for t in proc.tools
                    ],
                }
            elif health is not None and health.state == "failed":
                result[server.name] = {
                    "state": "failed",
                    "uptime_seconds": None,
                    "error": health.error,
                    "tools": [],
                }
            else:
                result[server.name] = {
                    "state": "stopped",
                    "uptime_seconds": None,
                    "error": None,
                    "tools": [],
                }
        return result

    async def _reconcile_loop(self, interval: float = 30.0):
        """Supervisor backstop: periodically restart servers whose session died,
        retry failed starts, and correct any drift — all without a daemon restart."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self._restart_unhealthy_servers()
                self.config_manager.reload()
                await self.reconcile_servers()
            except Exception as e:
                logger.error(f"Periodic reconcile failed: {e}")

    async def _restart_unhealthy_servers(self):
        """Best-effort liveness: probe each running server; if its session has
        broken (the child likely died), drop it so reconcile restarts it. A timeout
        is treated as 'busy but alive' so a merely-slow server is not restarted."""
        dead = []
        for name, proc in list(self.daemon.shared_processes.items()):
            if proc.session is None:
                continue
            try:
                await asyncio.wait_for(proc.list_tools(), timeout=10)
            except asyncio.TimeoutError:
                continue  # slow but alive
            except Exception as e:
                logger.warning(f"Server '{name}' failed its health check; will restart: {e}")
                dead.append(name)
        # Stop under the reconcile lock so this never races a concurrent
        # control-plane reconcile mutating the same shared-process table.
        if dead:
            async with self._reconcile_lock:
                for name in dead:
                    try:
                        await self.daemon.stop_shared_server(name)
                    except Exception as e:
                        logger.error(f"Failed to stop unhealthy server '{name}': {e}")
        # reconcile_servers (next in the supervisor loop) restarts them.

    def _check_control_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        try:
            expected = get_or_create_control_token()
        except Exception as e:
            logger.error(f"Could not read control token: {e}")
            return False
        return hmac.compare_digest(token, expected)

    def _control_unauthorized(self, request):
        """Return a 401 JSONResponse if the request lacks a valid control token,
        else None. Shared by every /control/* endpoint."""
        token = self._extract_bearer_token(request.headers.get("authorization"))
        if self._check_control_token(token):
            return None
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _security_settings(self, host: str, port: int) -> TransportSecuritySettings:
        allowed_hosts = [
            host,
            f"{host}:{port}",
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "localhost",
            f"localhost:{port}",
        ]
        return TransportSecuritySettings(allowed_hosts=allowed_hosts)

    def create_asgi_app(self, host: str, port: int) -> Starlette:
        manager = StreamableHTTPSessionManager(
            app=self.session_server,
            json_response=False,
            stateless=False,
            security_settings=self._security_settings(host, port),
        )
        http_app = HarbourAuthenticatedStreamableHTTPApp(self, manager)

        async def health(_request):
            # Unauthenticated identity/liveness probe. Loopback-only, so exposing
            # the service name + version here is acceptable and lets tooling
            # confirm it is Harbour answering (not just any open port).
            return JSONResponse({"service": "mcp-harbour", "version": __version__})

        async def control_reconcile(request):
            # Control plane: the CLI (dock/undock) calls this so the daemon applies
            # lifecycle changes immediately. The control token authenticates it —
            # agent tokens never match, so they cannot drive lifecycle.
            denied = self._control_unauthorized(request)
            if denied is not None:
                return denied
            self.config_manager.reload()
            return JSONResponse(await self.reconcile_servers())

        async def control_servers(request):
            # Control plane: live per-server status (state, uptime, tools) for the CLI.
            denied = self._control_unauthorized(request)
            if denied is not None:
                return denied
            self.config_manager.reload()
            return JSONResponse(self.server_status())

        @asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        return Starlette(
            routes=[
                Route("/healthz", endpoint=health, methods=["GET"]),
                Route("/control/reconcile", endpoint=control_reconcile, methods=["POST"]),
                Route("/control/servers", endpoint=control_servers, methods=["GET"]),
                Route("/mcp", endpoint=http_app, methods=["GET", "POST", "DELETE"]),
            ],
            lifespan=lifespan,
        )

    async def serve(self, host: str, port: int):
        """Run the gateway over Streamable HTTP."""
        self.config_manager.reload()
        await self.reconcile_servers()
        # Create the control token now so it exists before the CLI's first call
        # (avoids a first-use race where each side would mint a different token).
        try:
            get_or_create_control_token()
        except Exception as e:
            logger.error(f"Could not initialize control token: {e}")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError as e:
                if e.errno in (98, 48, 10048):
                    logger.error(f"Port {port} is already in use. Is another harbour instance running?")
                    logger.error("Check with: harbour status")
                    logger.error("Or use a different port: harbour serve --port <port>")
                    raise SystemExit(1)
                raise

        app = self.create_asgi_app(host, port)

        import uvicorn

        logger.info(f"Listening on http://{host}:{port}/mcp")
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        supervisor = asyncio.create_task(self._reconcile_loop())
        try:
            await server.serve()
        finally:
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
