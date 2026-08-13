"""Stream an app's logs (build log followed by live container logs) as text.

Framework-neutral: :func:`stream_app_logs` is an async generator of plain ``str``
chunks (each with no trailing newline) that a transport layer — currently the
WebSocket route in ``web/routes/api/apps.py`` — forwards to the browser, which
appends each chunk as one line.

Both sources are followed through the same shared primitive,
:func:`compute_space.core.process_stream.stream_process_lines` (also used by the
memory guard): the container log via ``podman logs --follow``, and the build log
(``docker.log``) via ``tail``. The build tail waits for the file to appear first —
during a build ``docker.log`` may not exist yet, which is expected — and then runs
``tail``; an unreadable file makes ``tail`` exit non-zero, which surfaces loudly via
``raise_on_error`` rather than as a silent empty log. Container-log lines carry a
``--timestamps`` prefix and ANSI codes, which we strip.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from collections.abc import Callable

from compute_space.core.containers import _ANSI_RE
from compute_space.core.process_stream import stream_process_lines

# Initial history replayed on connect; the live follow supplies everything after.
_CONTAINER_TAIL_LINES = 2000
_BUILD_TAIL_BYTES = 256 * 1024
# How often, while a build is in flight, we re-check the app's state to notice the
# container coming up (and hand off from the build log to the container log).
_BUILD_POLL_SECONDS = 0.5
_BUILDING_STATES = frozenset({"building", "starting"})

# Distinguishes "the build tail ended" from a real line in the follow loop below.
_EOF = object()


def _build_tail_argv(build_log_path: str, *, follow: bool) -> list[str]:
    """Shell command streaming the build log's last ``_BUILD_TAIL_BYTES`` bytes.

    ``follow`` waits for the file to appear (expected during early build) and then
    ``tail -f``s it live; without it the file is dumped once (an already-built app's
    history) and a missing file is simply empty. Either way an existing-but-unreadable
    file lets ``tail`` exit non-zero, surfaced loudly by the caller's ``raise_on_error``.
    """
    tail = f'exec tail -c {_BUILD_TAIL_BYTES} {"-f " if follow else ""}"$1"'
    if follow:
        # Wait for docker.log to appear (expected during early build), then follow it.
        script = f'while [ ! -e "$1" ]; do sleep {_BUILD_POLL_SECONDS}; done; {tail}'
    else:
        # Dump once if it exists; a missing log is just empty (a clean exit-0 end).
        script = f'if [ -e "$1" ]; then {tail}; fi'
    return ["sh", "-c", script, "sh", build_log_path]


async def _podman_follow(container_id: str, tail_lines: int) -> AsyncIterator[str]:
    """Yield container log lines (no trailing newline), the last ``tail_lines`` then live.

    ``podman logs --follow`` (via the shared helper) owns the initial tail, live
    updates, and rotation. ``--timestamps`` lets nothing depend on file offsets; we
    strip that prefix (and ANSI codes) before yielding, and ``merge_stderr`` folds the
    container's stderr into the feed like the old one-shot ``podman logs``.
    """
    argv = ["podman", "logs", "--follow", "--tail", str(tail_lines), "--timestamps", container_id]
    async for raw in stream_process_lines(argv, merge_stderr=True):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        # Drop the RFC3339 timestamp that --timestamps prepends ("<ts> <log>").
        sp = line.find(" ")
        yield _ANSI_RE.sub("", line[sp + 1 :] if sp != -1 else "")


async def _follow_build_until_container(
    build_log_path: str, get_state: Callable[[], tuple[str, str | None]]
) -> AsyncIterator[str]:
    """Follow the build log live, stopping once the container appears or the build ends.

    ``asyncio.wait`` with a timeout polls ``get_state`` between lines *without*
    cancelling the in-flight read — cancelling it would tear down the ``tail`` process
    — so the pending read simply carries over to the next poll.
    """
    gen = stream_process_lines(_build_tail_argv(build_log_path, follow=True), merge_stderr=True, raise_on_error=True)
    pending: asyncio.Task[object] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(gen, _EOF))
            done, _ = await asyncio.wait({pending}, timeout=_BUILD_POLL_SECONDS)
            if pending in done:
                line = pending.result()
                pending = None
                if line is _EOF:
                    return
                assert isinstance(line, bytes)
                yield line.decode("utf-8", "replace").rstrip("\r")
            status, container_id = get_state()
            if container_id is not None or status not in _BUILDING_STATES:
                return
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        await gen.aclose()


async def stream_app_logs(
    app_name: str,
    build_log_path: str,
    get_state: Callable[[], tuple[str, str | None]],
) -> AsyncIterator[str]:
    """Yield an app's logs as text chunks: build-log tail, then live container logs.

    ``get_state`` returns ``(status, container_id)`` and is re-queried while a build is
    in flight so we can hand off from tailing ``docker.log`` to following the container
    as soon as it comes up.
    """
    status, container_id = get_state()

    if container_id is None and status in _BUILDING_STATES:
        # Build in flight: follow docker.log live until the container comes up.
        async for line in _follow_build_until_container(build_log_path, get_state):
            yield line
        _, container_id = get_state()
    else:
        # Already built: replay the build log's tail once, then show container logs.
        async for raw in stream_process_lines(
            _build_tail_argv(build_log_path, follow=False), merge_stderr=True, raise_on_error=True
        ):
            yield raw.decode("utf-8", "replace").rstrip("\r")

    if container_id:
        yield "=== Container logs ==="
        async for line in _podman_follow(container_id, _CONTAINER_TAIL_LINES):
            yield line
