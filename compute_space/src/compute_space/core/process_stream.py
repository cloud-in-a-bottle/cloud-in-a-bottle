"""Watch a subprocess and consume its stdout as a stream of lines.

:func:`stream_process_lines` spawns a command and yields its stdout one line at a
time (trailing newline stripped) until the process closes stdout. It is the shared
primitive behind anything that "follows" a long-lived command — ``podman logs
--follow`` for an app's container log (see ``core.log_stream``) and the
``journalctl`` / ``podman events`` OOM streams (see ``core.memory_guard``).

Error philosophy — we do **not** paper over failures:

* **EOF is the one normal terminator.** stdout closing just ends the iteration;
  the caller decides whether that means "done" (a stopped container) or "reconnect"
  (a restarted daemon).
* **Everything else propagates.** A missing binary, an exec failure, a read error
  (e.g. permission denied) raises out of the generator rather than being swallowed
  into a silent empty stream. Whether such a state is *expected* is the caller's
  call: the memory guard treats "binary not installed" as expected on a dev host
  and catches it to disable a detector; the log-stream route lets it error the
  request. Neither one gets a quiet "no data" that hides a real problem.

The subprocess is always terminated and reaped, even when the consumer stops early
(its ``async for`` breaks or the surrounding task is cancelled).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from collections.abc import Callable

# Generous per-line cap so a long (but legitimate) log line doesn't trip asyncio's
# default 64 KiB StreamReader limit and raise mid-stream. A line beyond this is
# pathological and still fails loudly rather than being silently truncated.
_LINE_LIMIT_BYTES = 1024 * 1024

# Live follow subprocesses, so a process-wide shutdown can reap any that outlive
# their consumer (mirrors ``core.terminal._active_sessions``).
_active: set[asyncio.subprocess.Process] = set()


def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()


async def _drain_stderr(reader: asyncio.StreamReader, sink: Callable[[bytes], None]) -> None:
    """Forward the child's stderr to ``sink`` line by line (newline stripped) until it closes.

    Drained concurrently with stdout so a child that chatters on stderr can't fill its
    stderr pipe buffer and block — which would then stall the stdout we actually read.
    """
    while line := await reader.readline():
        sink(line.rstrip(b"\n"))


async def stream_process_lines(
    argv: list[str],
    *,
    merge_stderr: bool,
    raise_on_error: bool = False,
    stderr_sink: Callable[[bytes], None] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Spawn ``argv`` and yield its stdout line by line (newline stripped) until EOF.

    ``merge_stderr`` folds the child's stderr into the yielded stream (True — right
    for ``podman logs``, where container stderr is part of the log) or discards it
    (False — right for the OOM streams, where stderr is diagnostic noise).

    ``stderr_sink`` (only valid with ``merge_stderr=False``) instead diverts each
    stderr line to the callback — for callers that want to keep stderr *out* of the
    parsed stdout stream (e.g. the OOM followers' JSON) yet still surface it, say to
    log the real reason a follow ended instead of a bare EOF. It's drained on its own
    task so it can't stall stdout.

    ``raise_on_error`` turns a non-zero *self*-exit into a raised ``RuntimeError``
    instead of a silent EOF — for callers (like the build-log tail) where the child
    exiting non-zero means a real failure, not a normal end. It only fires when the
    child closes stdout on its own; a consumer that stops early (cancels) still tears
    the child down quietly, since we terminated it, not it failing.

    Only EOF ends the generator cleanly otherwise. A spawn or read failure propagates;
    see the module docstring. The child is terminated and reaped in all exit paths.
    """
    assert not (merge_stderr and stderr_sink is not None), "stderr_sink can't observe stderr that's merged into stdout"
    if merge_stderr:
        stderr = asyncio.subprocess.STDOUT
    elif stderr_sink is not None:
        stderr = asyncio.subprocess.PIPE
    else:
        stderr = asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        limit=_LINE_LIMIT_BYTES,
    )
    _active.add(proc)
    drain: asyncio.Task[None] | None = None
    if stderr_sink is not None and proc.stderr is not None:
        drain = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_sink))
    try:
        assert proc.stdout is not None
        # readline() returns b"" only at EOF; a blank line still carries its newline,
        # so the walrus loop stops on stream close, not on empty output.
        while line := await proc.stdout.readline():
            yield line.rstrip(b"\n")
        if raise_on_error:
            await proc.wait()
            if proc.returncode:
                raise RuntimeError(f"`{argv[0]}` exited with status {proc.returncode}")
    finally:
        _active.discard(proc)
        _terminate(proc)
        with contextlib.suppress(Exception):
            await proc.wait()
        if drain is not None:
            # The child is reaped above, so its stderr is now closed; the drain task
            # reads the last buffered lines, sees EOF, and ends on its own. Await it
            # (rather than cancel) so a normally-exiting child's buffered stderr is
            # fully delivered to the sink instead of being cut off mid-drain.
            with contextlib.suppress(Exception):
                await drain


def cleanup_all() -> None:
    """Terminate any live follow subprocesses. Called at process shutdown."""
    for proc in list(_active):
        _terminate(proc)
    _active.clear()
