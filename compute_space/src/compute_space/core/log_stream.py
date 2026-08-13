"""Stream an app's logs (build log followed by live container logs) as text.

Framework-neutral: :func:`stream_app_logs` is an async generator of plain ``str``
chunks (each with no trailing newline) that a transport layer — currently the
WebSocket route in ``web/routes/api/apps.py`` — forwards to the browser, which
appends each chunk as one line.

Two sources, matching the two phases of an app's life:

* The **build log** (``docker.log``) is a file our own build process appends to
  (only ever rotated on an explicit reload, which reloads the whole page), so we
  read it directly by byte offset. A missing file is the *expected* "build hasn't
  written yet" state and reads empty; any other read error (e.g. permission
  denied) propagates rather than being turned into a silent empty log.
* The **container log** comes from ``podman logs --follow`` via the shared
  :func:`compute_space.core.process_stream.stream_process_lines` primitive (the
  same one the memory guard uses for its ``journalctl`` / ``podman events``
  streams). podman — not us — owns incrementalism and log rotation, so we never
  compute an offset into a file that can move underneath us. We just strip its
  ``--timestamps`` prefix and ANSI codes, reproducing what the old one-shot
  ``podman logs`` call showed.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from collections.abc import Callable

from compute_space.core.containers import _ANSI_RE
from compute_space.core.process_stream import stream_process_lines

# Initial history replayed on connect; the live follow supplies everything after.
_CONTAINER_TAIL_LINES = 2000
_BUILD_TAIL_BYTES = 256 * 1024
# While a build is in flight there is no container yet, so we poll docker.log for
# appended bytes at this cadence until a container appears (or the build ends).
_BUILD_POLL_SECONDS = 0.5
_BUILDING_STATES = frozenset({"building", "starting"})


def _read_build_tail(path: str, max_bytes: int) -> tuple[str, int]:
    """Return ``(text, eof_offset)`` for the last ``max_bytes`` of ``path``.

    Drops a partial leading line when the file is longer than ``max_bytes`` so we
    never start mid-line. ``eof_offset`` is the file size, i.e. where a follow
    should resume from. A missing file (build hasn't written yet) is expected and
    returns ``("", 0)``; any other error propagates — we don't hide, say, a
    permission problem behind an empty log.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return "", 0
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1 :] if nl != -1 else b""
    return data.decode("utf-8", "replace"), size


def _read_from(path: str, offset: int) -> tuple[str, int]:
    """Return ``(new_text_since_offset, new_offset)`` for an append-only file.

    If the file shrank (rotated/truncated under us) we resync from the start and
    skip the gap rather than reading misaligned bytes. A missing file is expected
    (returns no new text); other read errors propagate.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size < offset:  # rotated/truncated — resync instead of misreading
                offset = 0
            if size <= offset:
                return "", offset
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return "", offset
    return data.decode("utf-8", "replace"), size


async def _podman_follow(container_id: str, tail_lines: int) -> AsyncIterator[str]:
    """Yield container log lines (no trailing newline), starting with the last
    ``tail_lines`` and then following live until the container stops or the
    consumer disconnects.

    Uses ``podman logs --follow`` (via the shared process-stream helper) so podman
    handles the initial tail, live updates, and rotation. ``--timestamps`` lets
    nothing here depend on file offsets; we strip that prefix (and ANSI codes)
    before yielding. ``merge_stderr`` folds the container's stderr into the feed,
    matching the old one-shot ``podman logs`` behaviour.
    """
    argv = ["podman", "logs", "--follow", "--tail", str(tail_lines), "--timestamps", container_id]
    async for raw in stream_process_lines(argv, merge_stderr=True):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        # Drop the RFC3339 timestamp that --timestamps prepends ("<ts> <log>").
        sp = line.find(" ")
        content = line[sp + 1 :] if sp != -1 else ""
        yield _ANSI_RE.sub("", content)


async def stream_app_logs(
    app_name: str,
    build_log_path: str,
    get_state: Callable[[], tuple[str, str | None]],
) -> AsyncIterator[str]:
    """Yield an app's logs as text chunks: build-log tail, then live container logs.

    ``get_state`` returns ``(status, container_id)`` and is re-queried while a
    build is in flight so we can switch from tailing ``docker.log`` to following
    the container as soon as it comes up.
    """
    status, container_id = get_state()

    tail, build_offset = _read_build_tail(build_log_path, _BUILD_TAIL_BYTES)
    if tail.strip():
        yield tail.rstrip("\n")

    # Build phase: no container yet, so follow docker.log until one appears.
    while container_id is None and status in _BUILDING_STATES:
        await asyncio.sleep(_BUILD_POLL_SECONDS)
        chunk, build_offset = _read_from(build_log_path, build_offset)
        if chunk:
            yield chunk.rstrip("\n")
        status, container_id = get_state()

    # Flush any build output written between the last poll and the switch-over.
    chunk, _ = _read_from(build_log_path, build_offset)
    if chunk:
        yield chunk.rstrip("\n")

    if container_id:
        yield "=== Container logs ==="
        async for line in _podman_follow(container_id, _CONTAINER_TAIL_LINES):
            yield line
