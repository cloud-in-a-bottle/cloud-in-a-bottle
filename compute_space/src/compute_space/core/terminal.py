"""Pure-Python web terminal: bridges a WebSocket to a PTY running bash."""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import AnyStr
from typing import Protocol

from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown

# Track active sessions for cleanup on shutdown.
_active_sessions: dict[int, tuple[subprocess.Popen[bytes], "_Pty", list[asyncio.Task[None]]]] = {}


class _Pty:
    """Nonblocking PTY I/O, including ownership of readiness callbacks."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.waiters: set[asyncio.Future[None]] = set()
        os.set_blocking(fd, False)

    async def _ready(self, *, writing: bool) -> None:
        if self.closed:
            raise asyncio.CancelledError
        ready: asyncio.Future[None] = self.loop.create_future()
        self.waiters.add(ready)

        def wake() -> None:
            if not ready.done():
                ready.set_result(None)

        add = self.loop.add_writer if writing else self.loop.add_reader
        remove = self.loop.remove_writer if writing else self.loop.remove_reader
        add(self.fd, wake)
        try:
            await ready
        finally:
            self.waiters.discard(ready)
            # close() already removed callbacks. Never touch a possibly reused fd.
            if not self.closed:
                remove(self.fd)

    async def read(self) -> bytes:
        while not self.closed:
            try:
                return os.read(self.fd, 4096)
            except BlockingIOError:
                await self._ready(writing=False)
        raise asyncio.CancelledError

    async def write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining and not self.closed:
            try:
                written = os.write(self.fd, remaining[:65536])
            except BlockingIOError:
                await self._ready(writing=True)
                continue
            if written == 0:
                raise OSError("PTY write made no progress")
            remaining = remaining[written:]
            if remaining:
                await asyncio.sleep(0)
        if remaining:
            raise asyncio.CancelledError

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.loop.remove_reader(self.fd)
        self.loop.remove_writer(self.fd)
        for waiter in self.waiters:
            waiter.cancel()
        os.close(self.fd)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Bound shell teardown; normal disconnects run this outside the event loop."""
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.warning(f"Terminal shell did not exit after SIGKILL (pid {proc.pid})")


class TerminalWebsocket(Protocol):
    """Framework-neutral websocket interface used by the terminal bridge."""

    async def send(self, data: bytes) -> None: ...

    async def receive(self) -> AnyStr: ...


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the terminal window size on a PTY file descriptor."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def handle_terminal_ws(ws: TerminalWebsocket) -> None:
    """Handle a WebSocket connection by bridging it to a PTY running bash.

    Wire protocol:
      Client → Server:
        0x00 + bytes  = terminal input (keystrokes)
        0x01 + JSON   = control message (e.g. {"type":"resize","cols":80,"rows":24})
      Server → Client:
        raw bytes     = terminal output
    """
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)

    try:
        proc = subprocess.Popen(
            ["bash", "-l"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env={**os.environ, "TERM": "xterm-256color"},
        )
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)

    terminal = _Pty(master_fd)
    session_id = id(ws)

    async def pty_to_ws() -> None:
        """Read from PTY master fd, send to WebSocket as binary."""
        try:
            while True:
                data = await terminal.read()
                if not data:
                    break
                await ws.send(data)
        except Exception as e:
            logger.info(f"pty_to_ws ended: {e}")

    async def ws_to_pty() -> None:
        """Read from WebSocket, write to PTY master fd."""
        try:
            while True:
                msg = await ws.receive()
                if isinstance(msg, bytes) and len(msg) > 0:
                    kind = msg[0]
                    payload = msg[1:]
                    if kind == 0x00:
                        await terminal.write(payload)
                    elif kind == 0x01:
                        ctrl = json.loads(payload)
                        if ctrl.get("type") == "resize":
                            _set_winsize(master_fd, ctrl["rows"], ctrl["cols"])
                elif isinstance(msg, str):
                    await terminal.write(msg.encode())
        except Exception as e:
            logger.info(f"ws_to_pty ended: {e}")

    tasks = [asyncio.create_task(t) for t in [pty_to_ws(), ws_to_pty(), wait_for_shutdown()]]
    _active_sessions[session_id] = (proc, terminal, tasks)
    try:
        logger.info(f"Terminal session started (pid {proc.pid})")
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if _active_sessions.pop(session_id, None) is not None:
            terminal.close()
            try:
                os.kill(proc.pid, signal.SIGHUP)
            except OSError:
                pass
            await asyncio.to_thread(_reap, proc)


def cleanup_all() -> None:
    """Kill any remaining PTY sessions. Called at shutdown."""
    logger.info("Cleaning up terminal sessions...")
    for sid, (proc, terminal, tasks) in list(_active_sessions.items()):
        _active_sessions.pop(sid, None)
        for task in tasks:
            task.cancel()
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        terminal.close()
        _reap(proc)
    logger.info("Terminal cleanup complete.")
