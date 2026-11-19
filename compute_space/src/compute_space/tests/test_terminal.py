"""Tests for the web terminal bridge, driving a real bash on a real PTY."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from compute_space.core import terminal


class _Websocket:
    """Fake client: types each message once the shell has printed something, then
    waits for ``marker`` in the output before disconnecting."""

    def __init__(self, messages: list[bytes | str], marker: bytes) -> None:
        self.messages = iter(messages)
        self.marker = marker
        self.output = bytearray()
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        # Cleared to model a client that has stopped reading output.
        self.draining = asyncio.Event()
        self.draining.set()

    async def receive(self) -> bytes | str:
        # Bash's terminal setup can flush input queued before it starts.
        await self.started.wait()
        try:
            return next(self.messages)
        except StopIteration:
            await self.completed.wait()
            raise EOFError("test client disconnected") from None

    async def send(self, data: bytes) -> None:
        await self.draining.wait()
        self.output.extend(data)
        self.started.set()
        if self.marker in self.output:
            self.completed.set()


async def _run(ws: _Websocket) -> None:
    await terminal.handle_terminal_ws(cast(terminal.TerminalWebsocket, ws))


@pytest.fixture
def plain_bash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[subprocess.Popen[bytes]]:
    """Swap the login shell for a bare bash and stub out router shutdown."""
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def popen(_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        kwargs["env"] = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TERM": "xterm", "PS1": "$ "}
        proc = real_popen(["bash", "--noprofile", "--norc"], **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(terminal, "wait_for_shutdown", asyncio.Event().wait)
    return processes


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_large_input_with_stalled_output_does_not_block_loop(
    plain_bash: list[subprocess.Popen[bytes]],
) -> None:
    """Regression for #493: a paste far larger than the PTY buffers, while the
    client is not reading output, must leave the event loop free."""
    ws = _Websocket([b"\x00" + b"# filler line\n" * 20000 + b"echo DONE-$((40+2))\n"], b"DONE-42")
    handler = asyncio.create_task(_run(ws))
    try:
        await asyncio.wait_for(ws.started.wait(), 5)
        ws.draining.clear()
        # Echo of the paste fills the output buffer, then the input buffer fills
        # behind it. The old blocking write wedged the loop here for good.
        ticks = 0
        for _ in range(20):
            await asyncio.sleep(0.05)
            ticks += 1
        assert ticks == 20
        assert not handler.done()
        ws.draining.set()
        await asyncio.wait_for(handler, 20)
    finally:
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
    assert b"DONE-42" in ws.output
    assert plain_bash[0].poll() is not None
    assert id(ws) not in terminal._active_sessions


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_binary_text_and_resize(plain_bash: list[subprocess.Popen[bytes]]) -> None:
    ws = _Websocket(
        [
            b'\x01{"type":"resize","rows":35,"cols":95}',
            b"\x00part=BIN\n",
            "stty size; printf '%sTEXT\\n' \"$part\"\n",
        ],
        b"BINTEXT\r\n",
    )
    await asyncio.wait_for(_run(ws), 10)
    assert b"35 95\r\n" in ws.output
    assert plain_bash[0].poll() is not None
    assert id(ws) not in terminal._active_sessions


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancel_reaps_shell_off_the_event_loop(
    plain_bash: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    reap_threads: list[int] = []
    real_reap = terminal._reap

    def reap(proc: subprocess.Popen[bytes]) -> None:
        reap_threads.append(threading.get_ident())
        real_reap(proc)

    monkeypatch.setattr(terminal, "_reap", reap)
    ws = _Websocket([], b"unused")
    handler = asyncio.create_task(_run(ws))
    await asyncio.wait_for(ws.started.wait(), 5)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(handler, 5)
    assert reap_threads and reap_threads[0] != loop_thread
    assert plain_bash[0].poll() is not None
    assert id(ws) not in terminal._active_sessions
