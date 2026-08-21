"""Unit tests for compute_space.core.process_stream.

These drive real subprocesses (``printf``/``sh``/``sys``) rather than mocks, so the
spawn, EOF, stderr-merge, error-propagation, and reap-on-early-exit paths are all
exercised end to end.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest

from compute_space.core import process_stream
from compute_space.core.process_stream import stream_process_lines


async def _collect(gen: AsyncIterator[bytes], limit: int | None = None) -> list[bytes]:
    out: list[bytes] = []
    async for line in gen:
        out.append(line)
        if limit is not None and len(out) >= limit:
            break
    return out


def test_yields_lines_until_eof() -> None:
    lines = asyncio.run(_collect(stream_process_lines(["printf", "a\nb\nc\n"], merge_stderr=False)))
    assert lines == [b"a", b"b", b"c"]


def test_reaps_process_after_eof() -> None:
    asyncio.run(_collect(stream_process_lines(["printf", "x\n"], merge_stderr=False)))
    assert not process_stream._active


def test_merge_stderr_true_includes_stderr() -> None:
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('from-stderr\\n')"]
    lines = asyncio.run(_collect(stream_process_lines(argv, merge_stderr=True)))
    assert b"from-stderr" in lines


def test_merge_stderr_false_discards_stderr() -> None:
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('from-stderr\\n'); print('from-stdout')"]
    lines = asyncio.run(_collect(stream_process_lines(argv, merge_stderr=False)))
    assert lines == [b"from-stdout"]


def test_stderr_sink_diverts_stderr_and_keeps_stdout_clean() -> None:
    # stderr goes to the sink (not folded into the yielded stdout), and a
    # normally-exiting child's buffered stderr is fully delivered before teardown.
    argv = [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err1\\nerr2\\n')"]
    captured: list[bytes] = []
    lines = asyncio.run(_collect(stream_process_lines(argv, merge_stderr=False, stderr_sink=captured.append)))
    assert lines == [b"out"]
    assert captured == [b"err1", b"err2"]


def test_stderr_sink_conflicts_with_merge_stderr() -> None:
    # merge_stderr folds stderr into stdout, so a sink couldn't also observe it —
    # the combination is a programming error, caught loudly.
    with pytest.raises(AssertionError):
        asyncio.run(_collect(stream_process_lines(["printf", "x\n"], merge_stderr=True, stderr_sink=lambda _b: None)))


def test_missing_binary_raises_loudly() -> None:
    # A missing binary is exactly the "weird" case we refuse to paper over: it
    # propagates instead of yielding an empty stream.
    with pytest.raises(FileNotFoundError):
        asyncio.run(_collect(stream_process_lines(["definitely-not-a-real-binary-xyz"], merge_stderr=False)))


# A child that emits one line then blocks forever, and is itself the process we
# signal (no shell wrapper spawning a grandchild that would survive SIGTERM), so
# terminate() reaps it promptly — matching the real podman/journalctl followers.
_EMIT_THEN_BLOCK = [sys.executable, "-u", "-c", "print('one', flush=True); import time; time.sleep(30)"]


def test_early_break_terminates_and_reaps_process() -> None:
    # Consumer reads one line then stops; the still-running child must be killed
    # and reaped rather than leaked.
    async def run() -> None:
        gen = stream_process_lines(_EMIT_THEN_BLOCK, merge_stderr=False)
        got = await _collect(gen, limit=1)
        assert got == [b"one"]
        # Closing the generator runs its finally (terminate + wait).
        await gen.aclose()

    asyncio.run(run())
    assert not process_stream._active


def test_raise_on_error_is_quiet_on_clean_exit() -> None:
    lines = asyncio.run(_collect(stream_process_lines(["printf", "ok\n"], merge_stderr=False, raise_on_error=True)))
    assert lines == [b"ok"]


def test_raise_on_error_raises_on_nonzero_self_exit() -> None:
    # The child streams a line then exits non-zero on its own: surfaced, not a
    # silent EOF.
    async def run() -> None:
        await _collect(stream_process_lines(["sh", "-c", "echo hi; exit 3"], merge_stderr=True, raise_on_error=True))

    with pytest.raises(RuntimeError, match="status 3"):
        asyncio.run(run())


def test_raise_on_error_does_not_raise_when_consumer_stops_early() -> None:
    # We terminated it (early break), so a non-zero exit-from-signal is expected,
    # not a failure — no raise.
    async def run() -> None:
        gen = stream_process_lines(_EMIT_THEN_BLOCK, merge_stderr=False, raise_on_error=True)
        got = await _collect(gen, limit=1)
        assert got == [b"one"]
        await gen.aclose()

    asyncio.run(run())
    assert not process_stream._active


def test_cleanup_all_terminates_live_process() -> None:
    async def run() -> None:
        gen = stream_process_lines(_EMIT_THEN_BLOCK, merge_stderr=False)
        await _collect(gen, limit=1)  # leaves the child running, registered in _active
        assert process_stream._active
        process_stream.cleanup_all()
        assert not process_stream._active
        await gen.aclose()

    asyncio.run(run())
