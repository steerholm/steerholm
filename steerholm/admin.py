"""The admin channel: how the CLI asks the daemon to read or change state.

A Unix socket on Linux and macOS, a named pipe on Windows. Either way the OS
tells the daemon which account is on the other end, so who may do what is
decided by the daemon from the connection, never from anything the caller says.

The protocol is one JSON object per line. A request is
`{"op": <name>, "args": {...}}`; the answer is `{"ok": true, "result": ...}` or
`{"ok": false, "error": <message>}`. A streaming request (`holm log --follow`)
gets a line per result until the CLI hangs up, with a `"result": null` line
every second so both ends notice when the other is gone.
"""
import asyncio
import contextlib
import inspect
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
# How long the daemon waits for a request on a connection it has accepted.
IDLE_TIMEOUT = 30.0
_LINE_LIMIT = 1024 * 1024
_PIPE_ATTEMPTS = 5
_PIPE_WAIT_MS = 1000

Handler = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class DaemonNotRunning(Exception):
    """Nothing is answering on the admin channel."""


class AdminError(Exception):
    """The daemon refused or failed a request; the message says why."""


class ChannelInUse(Exception):
    """Another daemon already serves the admin channel."""


def socket_path():
    return _config.CONFIG_DIR / "admin.sock"


def channel_name() -> str:
    """Where the admin channel lives, for messages."""
    return PIPE_NAME if os.name == "nt" else str(socket_path())


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
    _kernel32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
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

    def _wait_for_pipe() -> bool:
        """Wait until an instance of the pipe can be opened; False if none
        becomes free in time, or the pipe is gone."""
        return bool(_kernel32.WaitNamedPipeW(PIPE_NAME, _PIPE_WAIT_MS))

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
        """Open the channel. Raises ChannelInUse when another daemon serves it:
        a second daemon on another port must not take the CLI's channel over."""
        if _channel_answers():
            # The pipe name is machine-wide; the socket lives in this account's
            # state directory.
            where = "on this machine" if os.name == "nt" else "for this account"
            raise ChannelInUse(
                f"Another Steerholm daemon is already running {where}. "
                "Check with holm status."
            )
        if os.name == "nt":
            loop = asyncio.get_running_loop()

            def protocol():
                reader = asyncio.StreamReader(limit=_LINE_LIMIT)
                return asyncio.StreamReaderProtocol(reader, self._connection)

            self._servers = await loop.start_serving_pipe(protocol, PIPE_NAME)
            return
        path = socket_path()
        # Nothing answers on it, so a socket file left here is a stopped
        # daemon's, and binding needs it gone.
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
            try:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
            except TimeoutError:
                return   # connected and never asked; don't hold the connection
            if not line:
                return
            # aclosing: when the CLI hangs up mid-stream, end the request's stream
            # now, not whenever the generator happens to be collected.
            async with contextlib.aclosing(self.answers(caller, line)) as answers:
                async for answer in answers:
                    writer.write(answer)
                    await writer.drain()
        except ConnectionError:
            pass         # the CLI hung up, e.g. Ctrl-C on `holm log --follow`
        except Exception as e:
            logger.error("Admin connection failed: %s", e)
        finally:
            writer.close()

    async def answer(self, caller: str, line: bytes) -> bytes:
        """The encoded answer to one request line from `caller`."""
        return _encode(await self._single(caller, line))

    async def answers(self, caller: str, line: bytes):
        """Every encoded answer to one request line: one, or a stream."""
        op, handler, args, refusal = self._resolve(caller, line)
        if refusal is not None:
            yield _encode(refusal)
            return
        if not inspect.isasyncgenfunction(handler):
            yield _encode(await self._call(op, handler, caller, args))
            return
        try:
            async with contextlib.aclosing(handler(caller, args)) as results:
                async for result in results:
                    yield _encode({"ok": True, "result": result})
        except ValueError as e:
            yield _encode({"ok": False, "error": str(e)})
        except Exception as e:
            logger.exception("Admin request '%s' failed", op)
            yield _encode({"ok": False, "error": f"The daemon could not complete the request: {e}"})

    async def _single(self, caller: str, line: bytes) -> dict:
        op, handler, args, refusal = self._resolve(caller, line)
        if refusal is not None:
            return refusal
        return await self._call(op, handler, caller, args)

    def _resolve(self, caller: str, line: bytes):
        """(op, handler, args, None) for a request, or a refusal in last place."""
        if caller != self._own:
            logger.warning("Refused an admin request from %s.", caller)
            return None, None, None, {"ok": False, "error": "This daemon belongs to another account."}
        try:
            request = json.loads(line)
            op, args = request["op"], request.get("args") or {}
            if not isinstance(op, str) or not isinstance(args, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, None, None, {"ok": False, "error": "Malformed request."}
        handler = self._handlers.get(op)
        if handler is None:
            return op, None, None, {
                "ok": False,
                "error": f"Unknown request '{op}'. Is the CLI the same version as the daemon?",
            }
        return op, handler, args, None

    async def _call(self, op: str, handler: Handler, caller: str, args: dict) -> dict:
        try:
            return {"ok": True, "result": await handler(caller, args)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception("Admin request '%s' failed", op)
            return {"ok": False, "error": f"The daemon could not complete the request: {e}"}


def _encode(answer: dict) -> bytes:
    return json.dumps(answer).encode("utf-8") + b"\n"


# ── CLI side ────────────────────────────────────────────────────────

def request(op: str, **args) -> Any:
    """Send one request to the daemon and return its result.

    Raises DaemonNotRunning when nothing answers, AdminError when the daemon
    refuses or fails the request.
    """
    for raw in _answers(op, args):
        return parse_answer(raw)
    return parse_answer(b"")          # hung up without answering


def stream(op: str, **args):
    """Send a streaming request and yield each result until the daemon stops
    sending. Closing the generator hangs up, which ends the stream."""
    for raw in _answers(op, args):
        result = parse_answer(raw)
        if result is not None:        # null is the daemon's keep-alive
            yield result


def _answers(op: str, args: dict):
    """Each answer line the daemon sends to one request."""
    try:
        with _open_channel() as (read, write):
            write(encode_request(op, args))
            yield from _lines(read)
    except TimeoutError:
        raise AdminError(f"The daemon did not answer within {REQUEST_TIMEOUT:.0f}s.")
    except OSError as e:
        raise AdminError(f"The connection to the daemon failed: {e}")


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


def _lines(read):
    buf = b""
    while True:
        chunk = read()
        if not chunk:
            if buf:
                yield buf             # cut off mid-line: fails to parse, as it should
            return
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            yield line


def _channel_answers() -> bool:
    """Whether a daemon is serving the admin channel right now."""
    if os.name == "nt":
        try:
            open(PIPE_NAME, "rb").close()
        except FileNotFoundError:
            return False
        except OSError:
            return True      # busy, or another account's: either way it is taken
        return True
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(str(socket_path()))
        except OSError:
            return False
        return True


@contextlib.contextmanager
def _open_channel():
    """(read, write) on a connection to this account's daemon."""
    if os.name == "nt":
        with _open_pipe() as pipe:
            handle = msvcrt.get_osfhandle(pipe.fileno())
            # Any account can create a pipe by this name; only talk to one run
            # by this account, the way the daemon only answers this account.
            if _pipe_peer_sid(handle, server=True) != own_identity():
                raise AdminError(f"{PIPE_NAME} is not served by this account's daemon.")
            mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
            _check(_kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None))
            # No timeout on a pipe read, unlike the socket: a blocking read
            # cannot be given one without overlapped I/O.
            yield (lambda: pipe.read(65536)), pipe.write
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        try:
            sock.connect(str(socket_path()))
        except (FileNotFoundError, ConnectionRefusedError):
            raise DaemonNotRunning()
        yield (lambda: sock.recv(65536)), sock.sendall


def _open_pipe():
    # Between serving one client and listening for the next, every instance of
    # the pipe can be busy. `open` reports that as a bare EINVAL, so rather than
    # read the error, wait for a free instance the way Windows intends.
    for attempt in range(_PIPE_ATTEMPTS):
        try:
            return open(PIPE_NAME, "r+b", buffering=0)
        except FileNotFoundError:
            raise DaemonNotRunning()
        except OSError:
            if attempt == _PIPE_ATTEMPTS - 1 or not _wait_for_pipe():
                raise
