"""The external-resolver check: it must never leave a dig hanging around."""

from __future__ import annotations

import asyncio

import pytest

import compute_space.core.dns.propagation as propagation
from compute_space.core.dns.coredns_provider.interface import RecordType


class _HungProcess:
    returncode: int | None = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False
        self.communicate_cancelled = False
        self.communicate_started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_started.set()
        try:
            return await asyncio.Future[tuple[bytes, bytes]]()
        except asyncio.CancelledError:
            self.communicate_cancelled = True
            raise

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -9
        return self.returncode


@pytest.mark.asyncio
async def test_dig_kills_and_reaps_process_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HungProcess()

    async def spawn(*args: object, **kwargs: object) -> _HungProcess:
        return proc

    monkeypatch.setattr(propagation.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(propagation, "_DIG_TIMEOUT_SECONDS", 0.01)

    # A hung resolver reads as not-yet-visible, which means keep waiting rather than fail.
    assert await propagation._dig_sees("_acme-challenge.example.com", RecordType.TXT, {"tok"}) is False
    assert proc.communicate_cancelled is True
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_dig_reaps_process_and_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HungProcess()

    async def spawn(*args: object, **kwargs: object) -> _HungProcess:
        return proc

    monkeypatch.setattr(propagation.asyncio, "create_subprocess_exec", spawn)

    task = asyncio.create_task(propagation._dig_sees("_acme-challenge.example.com", RecordType.TXT, {"tok"}))
    await proc.communicate_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.communicate_cancelled is True
    assert proc.killed is True
    assert proc.waited is True
