"""The admin channel over a real Unix socket (Linux, macOS) or named pipe
(Windows): the transport, the peer identity the OS reports, and the answers."""
import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from steerholm import admin


@pytest.fixture
def channel(monkeypatch):
    """A private socket path / pipe name, so a daemon running on the machine is
    never reached. The socket lives in a short directory: macOS caps a Unix
    socket path at 104 bytes, which pytest's tmp_path can exceed."""
    if os.name == "nt":
        monkeypatch.setattr(admin, "PIPE_NAME", rf"\\.\pipe\steerholm-test-{uuid.uuid4().hex}")
        yield
        return
    directory = Path(tempfile.mkdtemp(prefix="holm", dir="/tmp"))
    monkeypatch.setattr(admin, "socket_path", lambda: directory / "admin.sock")
    yield
    shutil.rmtree(directory, ignore_errors=True)


async def _serve(handlers, call):
    """Run an AdminServer, make one blocking client call from a thread (as the
    CLI does), and return what it returned."""
    server = admin.AdminServer(handlers)
    await server.start()
    try:
        return await asyncio.to_thread(call)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_a_request_gets_its_result_and_the_callers_identity(channel):
    async def whoami(caller, args):
        return {"caller": caller, "args": args}

    result = await _serve({"whoami": whoami}, lambda: admin.request("whoami", x=1))

    assert result == {"caller": admin.own_identity(), "args": {"x": 1}}


@pytest.mark.asyncio
async def test_a_refusal_reaches_the_caller(channel):
    async def refuse(caller, args):
        raise ValueError("Server 'git' not found.")

    with pytest.raises(admin.AdminError, match="Server 'git' not found."):
        await _serve({"refuse": refuse}, lambda: admin.request("refuse"))


@pytest.mark.asyncio
async def test_an_unknown_request_is_refused(channel):
    with pytest.raises(admin.AdminError, match="Unknown request 'nope'"):
        await _serve({}, lambda: admin.request("nope"))


@pytest.mark.asyncio
async def test_a_handler_failure_is_reported_not_raised(channel):
    async def broken(caller, args):
        raise RuntimeError("disk on fire")

    with pytest.raises(admin.AdminError, match="could not complete the request: disk on fire"):
        await _serve({"broken": broken}, lambda: admin.request("broken"))


@pytest.mark.asyncio
async def test_another_account_is_refused(channel):
    async def whoami(caller, args):
        return caller

    server = admin.AdminServer({"whoami": whoami})
    server._own = "uid:-1" if os.name != "nt" else "S-1-5-21-0-0-0-1"   # someone else's daemon
    await server.start()
    try:
        with pytest.raises(admin.AdminError, match="another account"):
            await asyncio.to_thread(lambda: admin.request("whoami"))
    finally:
        await server.close()


def test_nothing_listening_is_daemon_not_running(channel):
    with pytest.raises(admin.DaemonNotRunning):
        admin.request("list_servers")


@pytest.mark.skipif(os.name == "nt", reason="a socket file is a Unix thing")
def test_a_leftover_socket_file_is_daemon_not_running(channel):
    import socket
    path = admin.socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()                      # the file stays; nothing listens on it
    with pytest.raises(admin.DaemonNotRunning):
        admin.request("list_servers")


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows uses the pipe's ACL")
@pytest.mark.asyncio
async def test_the_socket_is_owner_only_and_removed_on_close(channel):
    server = admin.AdminServer({})
    await server.start()
    path = admin.socket_path()
    assert path.stat().st_mode & 0o777 == 0o600
    await server.close()
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="a socket file is a Unix thing")
@pytest.mark.asyncio
async def test_a_previous_daemons_socket_file_is_replaced(channel):
    admin.socket_path().write_text("left behind")
    server = admin.AdminServer({})
    await server.start()
    try:
        with pytest.raises(admin.AdminError, match="Unknown request"):
            await asyncio.to_thread(lambda: admin.request("x"))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_a_malformed_request_is_refused():
    server = admin.AdminServer({})
    for line in (b"not json\n", b"[]\n", b'{"args": {}}\n'):
        with pytest.raises(admin.AdminError, match="Malformed request"):
            admin.parse_answer(await server.answer(admin.own_identity(), line))


def test_an_unreadable_answer_is_an_error():
    with pytest.raises(admin.AdminError, match="could not be read"):
        admin.parse_answer(b"")


@pytest.mark.skipif(os.name == "nt", reason="a raw Unix socket client")
@pytest.mark.asyncio
async def test_a_client_that_hangs_up_does_not_break_the_server(channel):
    import socket

    async def ping(caller, args):
        return "pong"

    server = admin.AdminServer({"ping": ping})
    await server.start()
    try:
        def hang_up():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(admin.socket_path()))
        await asyncio.to_thread(hang_up)
        assert await asyncio.to_thread(lambda: admin.request("ping")) == "pong"
    finally:
        await server.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix peer credentials")
@pytest.mark.asyncio
async def test_a_connection_whose_peer_cannot_be_identified_is_dropped(channel, monkeypatch):
    monkeypatch.setattr(admin, "_peer_uid", lambda sock: (_ for _ in ()).throw(OSError("no creds")))
    server = admin.AdminServer({})
    await server.start()
    try:
        # Dropped without an answer: either nothing comes back, or the reset does.
        with pytest.raises(admin.AdminError, match="could not be read|connection to the daemon failed"):
            await asyncio.to_thread(lambda: admin.request("x"))
    finally:
        await server.close()


def test_a_daemon_that_does_not_answer_in_time(monkeypatch):
    def slow(payload):
        raise TimeoutError()
    monkeypatch.setattr(admin, "_exchange_pipe" if os.name == "nt" else "_exchange_socket", slow)
    with pytest.raises(admin.AdminError, match="did not answer within 30s"):
        admin.request("x")

