"""Memory guard: warn the owner about memory pressure and OOM kills.

A daemon thread (mirroring the storage guard) that each tick warns when an app
nears its memory limit, when an app's container is OOM-killed (per-app, from
``podman events``), or when the host runs out of memory and the kernel OOM killer
reaps any process (from the system journal via ``journalctl``).

The "notification" is only a log line today; the ``TODO:`` markers are where the
real notification system will hook in once it exists.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time

import attr

from compute_space.config import Config
from compute_space.core.diagnostics import collect_app_resources
from compute_space.core.logging import logger
from compute_space.core.storage import format_bytes

_MEMORY_GUARD_INTERVAL_SECONDS = 60

_MEMORY_WARN_PERCENT = 90.0
_MEMORY_CLEAR_PERCENT = 80.0

_STREAM_READ_BYTES = 65536

# The two OOM detectors each stream a subprocess that emits newline-delimited JSON.
# journalctl reads the kernel log from the system journal (needs the systemd-journal
# group, granted via the unit's SupplementaryGroups=, not CAP_SYSLOG); --lines=0 so
# --follow reports only kills that happen while we run, not stale ones from the boot.
_JOURNALCTL_KERNEL_FOLLOW = ["journalctl", "--dmesg", "--follow", "--lines=0", "--output=json"]
_PODMAN_OOM_EVENTS = ["podman", "events", "--filter", "event=oom", "--filter", "type=container", "--format", "json"]

# A global kill logs "Out of memory: Killed process N (comm)"; a memcg (cgroup-limit)
# kill logs "Memory cgroup out of memory: ...". We skip the latter — that's an app
# hitting its own limit, already reported per-app via ``podman events``.
_OOM_KILLED_RE = re.compile(r"Killed process (\d+) \(([^)]+)\)")
_MEMCG_OOM_MARKER = "Memory cgroup out of memory"

_guard_thread_lock = threading.Lock()
_guard_thread: threading.Thread | None = None


@attr.s(auto_attribs=True, frozen=True)
class _HostOomKill:
    """A single host-level (global) OOM-kill event parsed from the kernel log."""

    pid: int
    comm: str


@attr.s(auto_attribs=True, frozen=True)
class _ContainerOomKill:
    """A single per-app OOM-kill event parsed from ``podman events``."""

    container_id: str
    container_name: str


def _read_stream_chunk(fd: int) -> bytes | None:
    """Return the next chunk of a streaming subprocess's stdout, or None if it would block.

    The stdout is a raw byte stream, so a chunk may hold part of a line, several
    lines, or a line split across reads — the caller reassembles on newlines. Empty
    bytes means EOF (the process exited); None means nothing is available right now.
    """
    try:
        return os.read(fd, _STREAM_READ_BYTES)  # b"" at EOF
    except BlockingIOError:
        return None


class _JsonEventStream:
    """A long-lived command whose stdout is drained as complete lines each tick.

    Both OOM detectors read newline-delimited JSON from a streaming subprocess —
    ``journalctl`` for the host, ``podman events`` per app — so this owns the shared
    plumbing: a non-blocking read, partial-line buffering across ticks, and
    reconnect-on-EOF. Owned by the one guard-loop thread, so it needs no locking. If
    the command can't start (binary missing) it stays off and ``drain_lines`` retries
    the start on a later tick; if the stream ends it reconnects on the next drain
    (lines during that brief gap are missed — acceptable for a rare outage).
    """

    def __init__(self, argv: list[str], detector: str) -> None:
        self._argv = argv
        self._detector = detector  # human label for log lines, e.g. "Host OOM detection"
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = b""
        # Suppress repeated "cannot start" warnings while the binary stays absent.
        self._start_warned = False
        self._start()

    def _start(self) -> None:
        """(Re)start the stream; leaves ``_proc`` None on failure."""
        try:
            proc = subprocess.Popen(self._argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            if not self._start_warned:
                logger.warning("Cannot start `%s` for %s: %s", self._argv[0], self._detector, e)
                self._start_warned = True
            self._proc = None
            return
        assert proc.stdout is not None
        # Non-blocking so ``drain_lines`` reads whatever is available and returns
        # rather than blocking the guard loop until the next line arrives.
        os.set_blocking(proc.stdout.fileno(), False)
        self._proc = proc
        self._buf = b""
        self._start_warned = False
        logger.info("%s active (streaming `%s`)", self._detector, " ".join(self._argv[:2]))

    def _reconnect(self) -> None:
        """Reap the ended stream and start a fresh one."""
        if self._proc is not None:
            if self._proc.stdout is not None:
                self._proc.stdout.close()
            self._proc.wait()  # Already exited (we hit EOF); reap the zombie.
            self._proc = None
        logger.warning("`%s` stream ended; reconnecting for %s", self._argv[0], self._detector)
        self._start()

    def drain_lines(self) -> list[bytes]:
        """Return the complete lines seen since the last drain; reconnect if the stream ended."""
        if self._proc is None:
            self._start()
            if self._proc is None:
                return []
        assert self._proc.stdout is not None
        fd = self._proc.stdout.fileno()

        while chunk := _read_stream_chunk(fd):
            self._buf += chunk

        # Last line may be partial. Leave it in the buffer until we see a trailing newline.
        *complete_lines, self._buf = self._buf.split(b"\n")

        # On a falsy read, b"" is EOF so we have to reconnect; None is just "nothing right now".
        if chunk == b"":
            self._reconnect()
        return [line for line in complete_lines if line.strip()]


def _parse_journal_oom(line: bytes) -> _HostOomKill | None:
    """Return a global OOM kill parsed from one ``journalctl --output=json`` line, or None.

    journalctl streams every kernel message as a JSON object; we read the ``MESSAGE``
    text, skip memcg (per-app) kills, and pull pid+comm from the kernel's "Killed
    process N (comm)". Most kernel lines aren't OOM kills, so None is the normal case
    — this is a filter over the whole kernel log, unlike the podman event stream where
    every line is a kill.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = obj.get("MESSAGE") if isinstance(obj, dict) else None
    if not isinstance(message, str) or _MEMCG_OOM_MARKER in message:
        return None
    match = _OOM_KILLED_RE.search(message)
    if match is None:
        return None
    return _HostOomKill(pid=int(match.group(1)), comm=match.group(2))


class _HostOomReader:
    """Reports host-level (global) OOM kills from the kernel log, via ``journalctl``.

    Reads kernel messages from the system journal rather than /dev/kmsg, so the unit
    needs only membership in the ``systemd-journal`` group (``SupplementaryGroups=``),
    not CAP_SYSLOG. Most kernel lines aren't OOM kills, so it filters for the ones
    that are.
    """

    def __init__(self) -> None:
        self._stream = _JsonEventStream(_JOURNALCTL_KERNEL_FOLLOW, "Host OOM detection")

    def check(self) -> None:
        """Report any host OOM kills seen since the last check."""
        for line in self._stream.drain_lines():
            kill = _parse_journal_oom(line)
            if kill is not None:
                logger.warning(
                    "TODO: make this a notification — the host OOM killer killed process %d (%s); "
                    "the machine is out of memory",
                    kill.pid,
                    kill.comm,
                )


def _parse_podman_event(line: bytes) -> _ContainerOomKill:
    """Parse one ``podman events --format json`` line into a container OOM kill.

    Every line the stream delivers is an OOM event (that's what ``--filter`` selects),
    so a line we can't parse is a kill we'd fail to report. Rather than quietly skip
    it, we commit to the shape podman emits — a JSON object with top-level ``ID`` and
    ``Name`` keys (verified against podman 5.8, and the version is pinned so it won't
    move underneath us) — and raise, with the offending line, on anything else. A
    format change then surfaces loudly at the guard loop's handler instead of
    silently dropping every OOM kill.
    """
    try:
        obj = json.loads(line)
        return _ContainerOomKill(container_id=str(obj["ID"]), container_name=str(obj["Name"]))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"unrecognized `podman events` line {line[:300]!r}") from e


class _PodmanOomReader:
    """Streams per-app (cgroup-limit) OOM kills from a long-lived ``podman events``.

    We stream events rather than poll ``podman inspect`` because ``--restart`` resets
    ``OOMKilled`` to false on the restarted run, so a poll races the restart and can
    miss the kill; the event is emitted the instant the kill happens and delivered
    exactly once.
    """

    def __init__(self) -> None:
        self._stream = _JsonEventStream(_PODMAN_OOM_EVENTS, "Per-app OOM detection")

    def drain(self) -> list[_ContainerOomKill]:
        """Return per-app OOM kills seen since the last drain.

        Every line is (by our ``--filter``) an OOM event, and ``_parse_podman_event``
        raises on an unrecognized shape rather than silently dropping the kill.
        """
        return [_parse_podman_event(line) for line in self._stream.drain_lines()]


class _MemoryGuard:
    """One guard loop's state and checks: OOM readers plus per-app pressure debounce.

    Everything here is owned by the one loop thread, so none of it needs locking.
    ``_pressure_notified`` is the debounce state (see ``_check_memory_pressure``);
    OOM kills need no such state, as each ``podman events`` event arrives once.
    """

    def __init__(self) -> None:
        self._host_oom = _HostOomReader()
        self._podman_oom = _PodmanOomReader()
        self._pressure_notified: set[str] = set()

    def check_once(self, config: Config) -> None:
        """Run one pass: host OOM + per-app OOM (events) + per-app memory pressure."""
        self._host_oom.check()
        container_ooms = self._podman_oom.drain()

        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT app_id, name, container_id, cpu_cores, memory_mb FROM apps WHERE container_id IS NOT NULL"
            ).fetchall()
        finally:
            db.close()

        self._report_container_ooms(container_ooms, rows)
        for row in rows:
            self._check_memory_pressure(row)

    def _report_container_ooms(self, kills: list[_ContainerOomKill], rows: list[sqlite3.Row]) -> None:
        """Notify once per podman OOM event, naming the owning app when we can map it."""
        by_container: dict[str, sqlite3.Row] = {row["container_id"]: row for row in rows}
        for kill in kills:
            row = by_container.get(kill.container_id)
            if row is not None:
                logger.warning(
                    "TODO: make this a notification — app %s was killed by the OOM killer "
                    "(exceeded its %sMB memory limit)",
                    row["name"],
                    row["memory_mb"],
                )
            else:
                # The DB row lags podman (container already gone/replaced): fall
                # back to the container name from the event so the kill still
                # surfaces rather than being dropped.
                logger.warning(
                    "TODO: make this a notification — container %s was killed by the OOM killer (out of memory)",
                    kill.container_name,
                )

    def _check_memory_pressure(self, row: sqlite3.Row) -> None:
        """Warn once when an app reaches the pressure threshold; re-arm only once it recovers well below it.

        Debounced with two thresholds: warn at ``_MEMORY_WARN_PERCENT``, but hold
        the notified state (suppressing repeats, and keeping a user-dismissed
        notification dismissed) until usage falls below ``_MEMORY_CLEAR_PERCENT``.
        Between the two — an app hovering around the line — we do nothing.
        """
        resources = collect_app_resources(row["container_id"], row["cpu_cores"], row["memory_mb"])
        percent = resources.memory_percent
        # If we know nothing, do nothing.
        if percent is None:
            return

        app_id = row["app_id"]
        if percent < _MEMORY_CLEAR_PERCENT:
            self._pressure_notified.discard(app_id)
            return

        if percent >= _MEMORY_WARN_PERCENT and app_id not in self._pressure_notified:
            self._pressure_notified.add(app_id)
            usage = format_bytes(resources.memory_usage_bytes) if resources.memory_usage_bytes is not None else "?"
            logger.warning(
                "TODO: make this a notification — app %s is at %.0f%% of its memory limit "
                "(%s of %sMB); it may be OOM-killed if usage keeps climbing",
                row["name"],
                percent,
                usage,
                row["memory_mb"],
            )


def _memory_guard_loop(config: Config) -> None:
    guard = _MemoryGuard()
    while True:
        try:
            guard.check_once(config)
        except Exception:
            logger.exception(
                "Memory guard tick failed; skipping this cycle and retrying in %ds",
                _MEMORY_GUARD_INTERVAL_SECONDS,
            )
        time.sleep(_MEMORY_GUARD_INTERVAL_SECONDS)


def ensure_memory_guard(config: Config) -> None:
    """Ensure the single memory guard daemon thread is running.

    Idempotent: repeated calls (or a second call with a different config) are no-ops
    once the one guard thread is up.
    """
    global _guard_thread
    with _guard_thread_lock:
        if _guard_thread is not None and _guard_thread.is_alive():
            return
        _guard_thread = threading.Thread(target=_memory_guard_loop, args=(config,), daemon=True, name="memory-guard")
        _guard_thread.start()
