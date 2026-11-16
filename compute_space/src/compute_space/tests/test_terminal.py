"""Terminal backpressure must never stop unrelated work on the router loop."""

import asyncio
import errno
import os
import pty
import subprocess
import termios
import threading
import tty

import pytest

from compute_space.core import terminal


@pytest.mark.asyncio
async def test_partial_writes_and_real_backpressure_preserve_all_input(monkeypatch: pytest.MonkeyPatch) -> None:
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(slave, False)
    bridge = terminal._Pty(master)
    blocked = asyncio.Event()
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        if fd != master:
            return real_write(fd, data)
        assert not os.get_blocking(fd)
        try:
            return real_write(fd, data[:137])
        except BlockingIOError:
            blocked.set()
            raise

    monkeypatch.setattr(terminal.os, "write", short_write)
    payload = bytes(range(256)) * 1024
    writer = asyncio.create_task(bridge.write(payload))
    try:
        # Hold the slave unread until its real kernel buffer fills. A separate
        # router coroutine must still run while the writer is backpressured.
        await asyncio.wait_for(blocked.wait(), 2)
        assert not writer.done()
        heartbeat = asyncio.create_task(asyncio.sleep(0))
        await asyncio.wait_for(heartbeat, 1)

        async def drain() -> bytes:
            received = bytearray()
            while len(received) < len(payload):
                try:
                    received.extend(os.read(slave, 4096))
                except BlockingIOError:
                    await asyncio.sleep(0)
            return bytes(received)

        received, _ = await asyncio.wait_for(asyncio.gather(drain(), writer), 3)
        assert received == payload
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        bridge.close()
        os.close(slave)


@pytest.mark.asyncio
@pytest.mark.parametrize("writing", [False, True])
async def test_close_wakes_pending_io_and_preserves_reused_descriptor(writing: bool) -> None:
    master, slave = pty.openpty()
    tty.setraw(slave)
    bridge = terminal._Pty(master)
    pending = asyncio.create_task(bridge.write(b"x" * 262144) if writing else bridge.read())
    async with asyncio.timeout(2):
        while not bridge.waiters:
            await asyncio.sleep(0)
    bridge.close()
    replacement, sender = os.pipe()
    readable = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        assert replacement == master  # Exercise actual descriptor reuse.
        loop.add_reader(replacement, readable.set)
        with pytest.raises(asyncio.CancelledError):
            await pending
        bridge.close()
        os.write(sender, b"replacement")
        await asyncio.wait_for(readable.wait(), 1)
        assert os.read(replacement, 11) == b"replacement"
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        loop.remove_reader(replacement)
        os.close(replacement)
        os.close(sender)
        os.close(slave)


class _Websocket:
    def __init__(self, messages: list[bytes | str], marker: bytes) -> None:
        self.messages = iter(messages)
        self.marker = marker
        self.output = bytearray()
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

    async def receive(self) -> bytes | str:
        # Like a browser, wait for the shell's first output before typing.
        # Bash's initial terminal setup can flush input queued before startup.
        await self.started.wait()
        try:
            return next(self.messages)
        except StopIteration:
            await self.completed.wait()
            raise EOFError("test client disconnected") from None

    async def send(self, data: bytes) -> None:
        self.output.extend(data)
        self.started.set()
        if self.marker in self.output:
            self.completed.set()


@pytest.mark.asyncio
async def test_websocket_binary_text_resize_and_disconnect(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    real_openpty = pty.openpty
    real_popen = subprocess.Popen
    real_write = os.write
    fds = []
    processes = []
    shutdown = asyncio.Event()

    def openpty():
        master, slave = real_openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        fds.append(master)
        return master, slave

    def popen(_argv, **kwargs):
        kwargs["env"] = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TERM": "xterm"}
        proc = real_popen(["bash", "--noprofile", "--norc"], **kwargs)
        processes.append(proc)
        return proc

    def short_write(fd, data):
        if fd in fds:
            assert not os.get_blocking(fd), "terminal input must not block the router loop"
            data = data[:37]
        return real_write(fd, data)

    monkeypatch.setattr(terminal.pty, "openpty", openpty)
    monkeypatch.setattr(terminal.subprocess, "Popen", popen)
    monkeypatch.setattr(terminal.os, "write", short_write)
    monkeypatch.setattr(terminal, "wait_for_shutdown", shutdown.wait)
    ws = _Websocket(
        [
            b"\x00" + b"# terminal bulk input\n" * 2500,
            b'\x01{"type":"resize","rows":35,"cols":95}',
            b"\x00part=BIN\n",
            "stty size; printf '%sTEXT\\n' \"$part\"\n",
        ],
        b"BINTEXT\r\n",
    )
    try:
        await asyncio.wait_for(terminal.handle_terminal_ws(ws), 5)
    except TimeoutError:
        pytest.fail(f"Shell did not complete input; output tail: {bytes(ws.output[-2000:])!r}")
    assert b"BINTEXT\r\n" in ws.output
    assert b"35 95\r\n" in ws.output
    assert processes[0].poll() is not None
    assert id(ws) not in terminal._active_sessions
    with pytest.raises(OSError) as exc:
        os.fstat(fds[0])
    assert exc.value.errno == errno.EBADF


@pytest.mark.asyncio
async def test_disconnect_reaps_shell_outside_router_loop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    real_popen = subprocess.Popen
    loop_thread = threading.get_ident()
    reap_threads = []
    real_reap = terminal._reap
    shutdown = asyncio.Event()

    def popen(_argv, **kwargs):
        kwargs["env"] = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        return real_popen(["bash", "--noprofile", "--norc"], **kwargs)

    def reap(proc):
        reap_threads.append(threading.get_ident())
        real_reap(proc)

    monkeypatch.setattr(terminal.subprocess, "Popen", popen)
    monkeypatch.setattr(terminal, "_reap", reap)
    monkeypatch.setattr(terminal, "wait_for_shutdown", shutdown.wait)
    ws = _Websocket([], b"unused")
    handler = asyncio.create_task(terminal.handle_terminal_ws(ws))
    await asyncio.sleep(0)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(handler, 3)
    assert reap_threads and reap_threads[0] != loop_thread
    assert id(ws) not in terminal._active_sessions
