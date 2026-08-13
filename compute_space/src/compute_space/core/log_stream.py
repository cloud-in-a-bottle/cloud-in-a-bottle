"""Stream an app's logs (build log followed by live container logs) as text.

Framework-neutral: :func:`stream_app_logs` is an async generator of plain ``str``
chunks (each with no trailing newline) that a transport layer — currently the
WebSocket route in ``web/routes/api/apps.py`` — forwards to the browser, which
appends each chunk as one line.

Both sources are followed through the same shared primitive,
:func:`compute_space.core.process_stream.stream_process_lines` (also used by the
memory guard): the container log via ``podman logs --follow``, and the build log
(``docker.log``) via ``tail``. We check for docker.log's existence in Python — it's
briefly absent at build start and across a reload's log rotation, which is expected —
so ``tail`` only ever runs against a file that exists; if that file is unreadable
(permission denied) ``tail`` exits non-zero and ``raise_on_error`` surfaces it loudly
rather than as a silent empty log.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

from compute_space.core.containers import _ANSI_RE
from compute_space.core.process_stream import stream_process_lines
from compute_space.db.connection import get_db

# Initial history replayed on connect; the live follow supplies everything after.
_CONTAINER_TAIL_LINES = 2000
_BUILD_TAIL_BYTES = 256 * 1024
# How often, while a build is in flight, we re-check the app's state (to notice the
# container coming up) and docker.log's existence (to notice the build starting).
_BUILD_POLL_SECONDS = 0.5
_BUILDING_STATES = frozenset({"building", "starting"})

# Distinguishes "the build tail ended" from a real line in the follow loop below.
_EOF = object()


def _tail_argv(build_log_path: str, *, follow: bool) -> list[str]:
    """``tail`` argv for the build log's last ``_BUILD_TAIL_BYTES`` bytes (``-f`` to follow).

    Callers guard existence themselves (a missing docker.log is expected mid-build); an
    existing-but-unreadable file lets ``tail`` exit non-zero, surfaced by ``raise_on_error``.
    """
    return ["tail", "-c", str(_BUILD_TAIL_BYTES), *(["-f"] if follow else []), build_log_path]


async def _podman_follow(container_id: str, tail_lines: int) -> AsyncIterator[str]:
    """Yield container log lines (no trailing newline), the last ``tail_lines`` then live.

    ``podman logs --follow`` (via the shared helper) owns the initial tail, live updates,
    and rotation; ``merge_stderr`` folds the container's stderr into the feed like the old
    one-shot ``podman logs``. Each line is ANSI-stripped to match that call.
    """
    argv = ["podman", "logs", "--follow", "--tail", str(tail_lines), container_id]
    async for raw in stream_process_lines(argv, merge_stderr=True):
        yield _ANSI_RE.sub("", raw.decode("utf-8", "replace").rstrip("\r"))


def _get_state(app_id: str) -> tuple[str, str | None]:
    """Return ``(status, container_id)`` for an app, or ``("removed", None)`` if it's gone.

    The stream outlives the request that started it, so we open (and close) a fresh
    short-lived connection each call rather than holding one for the connection's life.
    """
    with contextlib.closing(get_db()) as conn:
        row = conn.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if row is None:
        return "removed", None
    return row["status"], row["container_id"]


def _still_building(app_id: str) -> bool:
    """True while a build is in flight and no container has appeared yet."""
    status, container_id = _get_state(app_id)
    return container_id is None and status in _BUILDING_STATES


async def _next_build_line(pending: asyncio.Future[object], app_id: str) -> object:
    """Await the next build-log line, or ``_EOF`` once the follow should hand off.

    Each wait is bounded to ``_BUILD_POLL_SECONDS`` so a quiet interval lets us re-query
    the app state; ``shield`` keeps the in-flight read alive across that timeout
    (cancelling it would kill ``tail``) so it resumes on the next call. The tail ending
    surfaces as ``_EOF`` from ``pending`` itself; the container coming up we detect here
    and report as ``_EOF`` too, since either way the caller stops tailing docker.log. The
    caller owns ``pending``'s lifecycle and reaps the shielded read in its ``finally``.
    """
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(pending), _BUILD_POLL_SECONDS)
        except TimeoutError:
            if not _still_building(app_id):
                return _EOF


async def _follow_build_until_container(app_id: str, build_log_path: str) -> AsyncIterator[str]:
    """Follow the build log live, stopping once the container appears or the build ends.

    docker.log is briefly absent at build start and across a reload's log rotation, so we
    first wait for it to appear (giving up if the build ends first). Then we forward its
    lines until :func:`_next_build_line` reports ``_EOF`` — the tail ending, or a quiet
    interval in which the container has come up so we hand off to following it.
    """
    while not os.path.exists(build_log_path):
        if not _still_building(app_id):
            return
        await asyncio.sleep(_BUILD_POLL_SECONDS)

    gen = stream_process_lines(_tail_argv(build_log_path, follow=True), merge_stderr=True, raise_on_error=True)
    pending = asyncio.ensure_future(anext(gen, _EOF))
    try:
        while (line := await _next_build_line(pending, app_id)) is not _EOF:
            assert isinstance(line, bytes)
            yield line.decode("utf-8", "replace").rstrip("\r")
            pending = asyncio.ensure_future(anext(gen, _EOF))
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        await gen.aclose()


async def stream_app_logs(app_id: str, build_log_path: str) -> AsyncIterator[str]:
    """Yield an app's logs as text chunks: build-log tail, then live container logs.

    The app's ``(status, container_id)`` is re-queried while a build is in flight so we
    can hand off from tailing ``docker.log`` to following the container as it comes up.
    """
    if _still_building(app_id):
        # Build in flight: follow docker.log live until the container comes up.
        async for line in _follow_build_until_container(app_id, build_log_path):
            yield line
    elif os.path.exists(build_log_path):
        # Already built: replay the build log's tail once, then show container logs.
        async for raw in stream_process_lines(
            _tail_argv(build_log_path, follow=False), merge_stderr=True, raise_on_error=True
        ):
            yield raw.decode("utf-8", "replace").rstrip("\r")

    _, container_id = _get_state(app_id)
    if container_id:
        yield "=== Container logs ==="
        async for line in _podman_follow(container_id, _CONTAINER_TAIL_LINES):
            yield line
