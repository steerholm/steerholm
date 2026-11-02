"""The admin channel: how the CLI asks the daemon to read or change state.

A Unix socket on Linux and macOS, a named pipe on Windows. Either way the OS
tells the daemon which account is on the other end, so who may do what is
decided by the daemon from the connection, never from anything the caller says.

The protocol is one JSON object per line. A request is
`{"op": <name>, "args": {...}}`; the answer is `{"ok": true, "result": ...}` or
`{"ok": false, "error": <message>}`.
"""
import asyncio
import json
import logging
import os
import socket
import struct
import sys
from typing import Any, Awaitable, Callable, Dict

from . import config as _config
from .files import restrict

logger = logging.getLogger("steerholm.admin")

PIPE_NAME = r"\\.\pipe\steerholm"
REQUEST_TIMEOUT = 30.0
_LINE_LIMIT = 1024 * 1024

Handler = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class DaemonNotRunning(Exception):
    """Nothing is answering on the admin channel."""


class AdminError(Exception):
    """The daemon refused or failed a request; the message says why."""


def socket_path():
    return _config.CONFIG_DIR / "admin.sock"


# ── who is on the other end ─────────────────────────────────────────

def own_identity() -> str:
    """The account this process runs as, in the form `peer_identity` reports."""
    if os.name == "nt":
        return _process_sid(os.getpid())
    return f"uid:{os.getuid()}"


def _peer_uid(sock: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):                     # Linux
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    if sys.platform == "darwin":
        # LOCAL_PEERCRED (level SOL_LOCAL = 0, option 1) returns a struct xucred:
        # u_int cr_version, uid_t cr_uid, short cr_ngroups, gid_t cr_groups[16].
        creds = sock.getsockopt(0, 1, 76)
        return struct.unpack_from("I", creds, 4)[0]
    raise OSError("Cannot identify the peer of a Unix socket on this platform.")


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.GetNamedPipeClientProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    _kernel32.GetNamedPipeServerProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    _kernel32.SetNamedPipeHandleState.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p,
    )
    _advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    )
    _advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _PIPE_READMODE_BYTE = 0

    def _check(ok) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _process_sid(pid: int) -> str:
        """The SID of the account a process runs as, e.g. `S-1-5-21-…-1001`."""
        process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        _check(process)
        try:
            token = wintypes.HANDLE()
            _check(_advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)))
            try:
                size = wintypes.DWORD()
                _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
                buf = ctypes.create_string_buffer(size.value)
                _check(_advapi32.GetTokenInformation(
                    token, _TOKEN_USER, buf, size, ctypes.byref(size)))
                # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID.
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                text = wintypes.LPWSTR()
                _check(_advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)))
                try:
                    return text.value
                finally:
                    _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            finally:
                _kernel32.CloseHandle(token)
        finally:
            _kernel32.CloseHandle(process)

    def _pipe_peer_sid(handle: int, *, server: bool) -> str:
        pid = wintypes.ULONG()
        lookup = (_kernel32.GetNamedPipeServerProcessId if server
                  else _kernel32.GetNamedPipeClientProcessId)
        _check(lookup(handle, ctypes.byref(pid)))
        return _process_sid(pid.value)


def peer_identity(writer: asyncio.StreamWriter) -> str:
    """The account of the process on the other end of an accepted connection."""
    if os.name == "nt":
        pipe = writer.get_extra_info("pipe")
        return _pipe_peer_sid(pipe.handle, server=False)
    return f"uid:{_peer_uid(writer.get_extra_info('socket'))}"


# ── daemon side ─────────────────────────────────────────────────────

class AdminServer:
    """Serves admin requests. `handlers` maps an op name to
    `async handler(caller, args)`; a ValueError from it is the refusal the
    caller sees, anything else is logged and reported as an internal error."""

    def __init__(self, handlers: Dict[str, Handler]):
        self._handlers = handlers
        self._own = own_identity()
        self._servers = []

    async def start(self) -> None:
        if os.name == "nt":
            loop = asyncio.get_running_loop()

            def protocol():
                reader = asyncio.StreamReader(limit=_LINE_LIMIT)
                return asyncio.StreamReaderProtocol(reader, self._connection)

            self._servers = await loop.start_serving_pipe(protocol, PIPE_NAME)
            return
        path = socket_path()
        # The daemon already holds its TCP port, so it is the only one: a socket
        # file left here is a previous daemon's, and binding needs it gone.
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._connection, path=str(path), limit=_LINE_LIMIT)
        restrict(path, 0o600)
        self._servers = [server]

    async def close(self) -> None:
        for server in self._servers:
            server.close()
        if os.name != "nt":
            for server in self._servers:
                await server.wait_closed()
            socket_path().unlink(missing_ok=True)
        self._servers = []

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            caller = peer_identity(writer)
            line = await reader.readline()
            if not line:
                return
            writer.write(await self.answer(caller, line))
            await writer.drain()
        except Exception as e:
            logger.error("Admin connection failed: %s", e)
        finally:
            writer.close()

    async def answer(self, caller: str, line: bytes) -> bytes:
        """The encoded answer to one request line from `caller`."""
        return json.dumps(await self._answer(caller, line)).encode("utf-8") + b"\n"

    async def _answer(self, caller: str, line: bytes) -> dict:
        if caller != self._own:
            logger.warning("Refused an admin request from %s.", caller)
            return {"ok": False, "error": "This daemon belongs to another account."}
        try:
            request = json.loads(line)
            op, args = request["op"], request.get("args") or {}
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Malformed request."}
        handler = self._handlers.get(op)
        if handler is None:
            return {"ok": False, "error": f"Unknown request '{op}'. Is the CLI the same version as the daemon?"}
        try:
            return {"ok": True, "result": await handler(caller, args)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception("Admin request '%s' failed", op)
            return {"ok": False, "error": f"The daemon could not complete the request: {e}"}


# ── CLI side ────────────────────────────────────────────────────────

def request(op: str, **args) -> Any:
    """Send one request to the daemon and return its result.

    Raises DaemonNotRunning when nothing answers, AdminError when the daemon
    refuses or fails the request.
    """
    payload = encode_request(op, args)
    try:
        raw = _exchange_pipe(payload) if os.name == "nt" else _exchange_socket(payload)
    except TimeoutError:
        raise AdminError(f"The daemon did not answer within {REQUEST_TIMEOUT:.0f}s.")
    except (DaemonNotRunning, AdminError):
        raise
    except OSError as e:
        raise AdminError(f"The connection to the daemon failed: {e}")
    return parse_answer(raw)


def encode_request(op: str, args: dict) -> bytes:
    return json.dumps({"op": op, "args": args}).encode("utf-8") + b"\n"


def parse_answer(raw: bytes) -> Any:
    try:
        answer = json.loads(raw)
    except ValueError:
        raise AdminError("The daemon sent an answer that could not be read.")
    if not answer.get("ok"):
        raise AdminError(answer.get("error") or "The daemon refused the request.")
    return answer.get("result")


def _read_line(read) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = read()
        if not chunk:
            break
        data += chunk
    return data


def _exchange_socket(payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        try:
            sock.connect(str(socket_path()))
        except (FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning()
        sock.sendall(payload)
        return _read_line(lambda: sock.recv(65536))


def _exchange_pipe(payload: bytes) -> bytes:
    try:
        pipe = open(PIPE_NAME, "r+b", buffering=0)
    except FileNotFoundError:
        raise DaemonNotRunning()
    with pipe:
        handle = msvcrt.get_osfhandle(pipe.fileno())
        # Any account can create a pipe by this name; only talk to one run by
        # this account, the way the daemon only answers this account.
        if _pipe_peer_sid(handle, server=True) != own_identity():
            raise AdminError(f"{PIPE_NAME} is not served by this account's daemon.")
        mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
        _check(_kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None))
        pipe.write(payload)
        return _read_line(lambda: pipe.read(65536))
