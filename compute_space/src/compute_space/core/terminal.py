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
_active_sessions: dict[int, subprocess.Popen[bytes]] = {}


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

    session_id = id(ws)
    _active_sessions[session_id] = proc

    # The PTY master is driven through asyncio's pipe transports, which make the
    # fd nonblocking and handle partial writes and buffering. A blocking write
    # on the event loop can deadlock the whole router: the loop waits for bash
    # to consume input while bash waits for the loop to drain its output (#493).
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    async def pty_to_ws() -> None:
        """Read from PTY master fd, send to WebSocket as binary."""
        try:
            while data := await reader.read(4096):
                await ws.send(data)
        except Exception as e:  # EIO once every slave fd is closed
            logger.info(f"pty_to_ws ended: {e}")

    async def write_pty(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def ws_to_pty() -> None:
        """Read from WebSocket, write to PTY master fd."""
        try:
            while True:
                msg = await ws.receive()
                if isinstance(msg, bytes) and len(msg) > 0:
                    kind = msg[0]
                    payload = msg[1:]
                    if kind == 0x00:
                        await write_pty(payload)
                    elif kind == 0x01:
                        ctrl = json.loads(payload)
                        if ctrl.get("type") == "resize":
                            _set_winsize(master_fd, ctrl["rows"], ctrl["cols"])
                elif isinstance(msg, str):
                    await write_pty(msg.encode())
        except Exception as e:
            logger.info(f"ws_to_pty ended: {e}")

    read_transport: asyncio.BaseTransport | None = None
    write_transport: asyncio.WriteTransport | None = None
    try:
        # Each transport owns and closes its own fd, hence the dups.
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(master_fd), "rb", buffering=0)
        )
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(os.dup(master_fd), "wb", buffering=0)
        )
        writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)

        logger.info(f"Terminal session started (pid {proc.pid})")
        tasks = [asyncio.create_task(t) for t in [pty_to_ws(), ws_to_pty(), wait_for_shutdown()]]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _active_sessions.pop(session_id, None)
        if read_transport is not None:
            read_transport.close()
        if write_transport is not None:
            write_transport.abort()  # drop unsent input; close() would wait for it
        os.close(master_fd)
        try:
            os.kill(proc.pid, signal.SIGHUP)
        except OSError:
            pass
        await asyncio.to_thread(_reap, proc)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Wait for the shell to exit, escalating to SIGKILL. Bounded so teardown can't hang."""
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.warning(f"Terminal shell did not exit after SIGKILL (pid {proc.pid})")


def cleanup_all() -> None:
    """Kill any remaining PTY sessions. Called at shutdown.

    The PTY fds belong to each session's transports, which close them when the
    session task is cancelled or the process exits.
    """
    logger.info("Cleaning up terminal sessions...")
    for proc in list(_active_sessions.values()):
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        _reap(proc)
    _active_sessions.clear()
    logger.info("Terminal cleanup complete.")
