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


async def stream_process_lines(argv: list[str], *, merge_stderr: bool) -> AsyncGenerator[bytes, None]:
    """Spawn ``argv`` and yield its stdout line by line (newline stripped) until EOF.

    ``merge_stderr`` folds the child's stderr into the yielded stream (True — right
    for ``podman logs``, where container stderr is part of the log) or discards it
    (False — right for the OOM streams, where stderr is diagnostic noise).

    Only EOF ends the generator cleanly. A spawn or read failure propagates; see the
    module docstring. The child is terminated and reaped in all exit paths.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
        limit=_LINE_LIMIT_BYTES,
    )
    _active.add(proc)
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break  # stdout closed — the process has ended (or is ending).
            yield line.rstrip(b"\n")
    finally:
        _active.discard(proc)
        _terminate(proc)
        with contextlib.suppress(Exception):
            await proc.wait()


def cleanup_all() -> None:
    """Terminate any live follow subprocesses. Called at process shutdown."""
    for proc in list(_active):
        _terminate(proc)
    _active.clear()
