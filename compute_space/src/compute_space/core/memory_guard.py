"""Memory guard: warn the owner about memory pressure and OOM kills.

A daemon thread (mirroring the storage guard) that each tick warns when an app
nears its memory limit, when an app's container is OOM-killed (per-app, from
``podman events``), or when the host runs out of memory and the kernel OOM killer
reaps any process (from ``/dev/kmsg``).

The "notification" is only a log line today; the ``TODO:`` markers are where the
real notification system will hook in once it exists.
"""

from __future__ import annotations

import errno
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

# The two thresholds debounce the pressure warning; see ``_check_memory_pressure``.
_MEMORY_WARN_PERCENT = 90.0
_MEMORY_CLEAR_PERCENT = 80.0

# One kernel-pipe read; sized to the 64K pipe buffer so a tick drains it in one go.
_PODMAN_EVENTS_READ_BYTES = 65536

# Reading /dev/kmsg needs CAP_SYSLOG (granted to the openhost unit) on a default
# dmesg_restrict=1 host.
_KMSG_PATH = "/dev/kmsg"
# /dev/kmsg is record-oriented: each read() returns exactly one whole log record
# (never split across reads, never two coalesced). The buffer only has to be big
# enough to hold one record, or the read fails with EINVAL — 8192 is the kernel's
# max record size (the same size journald reads with).
_KMSG_RECORD_MAX_BYTES = 8192
# A global kill logs "Out of memory: Killed process N (comm)"; a memcg (cgroup-
# limit) kill logs "Memory cgroup out of memory: ...". We skip the latter — that's
# an app hitting its own limit, already reported per-app via ``podman events``.
_OOM_KILLED_RE = re.compile(r"Killed process (\d+) \(([^)]+)\)")
_MEMCG_OOM_MARKER = "Memory cgroup out of memory"

# At most one guard thread runs per process; the lock makes ensure_memory_guard's
# check-and-start atomic. It's the only cross-thread state here — a guard run's own
# state lives on the _MemoryGuard its loop thread owns, so that needs no locking.
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


def _open_kmsg() -> int | None:
    """Open ``/dev/kmsg`` positioned at its end, or return None if it can't be read.

    Seeking to the end (rather than replaying the ring buffer) is what limits us to
    kills that happen while we're running, not stale ones from earlier in the boot.
    """
    try:
        fd = os.open(_KMSG_PATH, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        # No /dev/kmsg (e.g. macOS dev host): host OOM detection is simply off.
        return None
    except PermissionError:
        logger.warning(
            "Cannot read %s for host OOM detection (needs CAP_SYSLOG); host-level OOM kills will not be reported",
            _KMSG_PATH,
        )
        return None
    except OSError as e:
        logger.warning("Failed to open %s for host OOM detection: %s", _KMSG_PATH, e)
        return None
    try:
        os.lseek(fd, 0, os.SEEK_END)
    except OSError as e:
        # Without SEEK_END we'd replay the whole ring buffer and warn about
        # ancient kills; refuse rather than emit stale false positives.
        logger.warning("Cannot seek %s to end (%s); host OOM detection disabled", _KMSG_PATH, e)
        os.close(fd)
        return None
    return fd


def _parse_kmsg_oom(record: str) -> _HostOomKill | None:
    """Return a global OOM kill parsed from one ``/dev/kmsg`` record, or None.

    A record is ``<prio>,<seq>,<ts>,<flags>[,...];<message>\\n<continuations>``.
    We look at the message line only, skip memcg (cgroup) kills, and match the
    victim's pid + comm from the kernel's "Killed process N (comm)" text.
    """
    _, sep, body = record.partition(";")
    if not sep:
        return None
    message = body.split("\n", 1)[0]
    if _MEMCG_OOM_MARKER in message:
        return None
    match = _OOM_KILLED_RE.search(message)
    if match is None:
        return None
    return _HostOomKill(pid=int(match.group(1)), comm=match.group(2))


def _read_next_kmsg_record(fd: int) -> bytes | None:
    """Return the next available ``/dev/kmsg`` record, or None when none remain right now.

    Each read returns one whole record — a single kernel log line, never split or
    coalesced. The only looping here skips records overwritten while we lagged
    behind: the kernel signals that with EPIPE and advances the read position to the
    next still-available record, so we retry. Every exit is a ``return``.
    """
    while True:
        try:
            return os.read(fd, _KMSG_RECORD_MAX_BYTES) or None  # empty read == EOF
        except BlockingIOError:
            return None  # No more records available right now.
        except OSError as e:
            if e.errno != errno.EPIPE:
                raise
            # Overwritten while we lagged; the read position advanced to the next
            # record — loop around and read it.


def _drain_kmsg_oom_kills(fd: int) -> list[_HostOomKill]:
    """Read all currently-available ``/dev/kmsg`` records and return global OOM kills."""
    kills: list[_HostOomKill] = []
    while (record := _read_next_kmsg_record(fd)) is not None:
        event = _parse_kmsg_oom(record.decode("utf-8", "replace"))
        if event is not None:
            kills.append(event)
    return kills


class _HostOomReader:
    """Reports host-level (global) OOM kills from ``/dev/kmsg``.

    The open (and so the CAP_SYSLOG check) happens once at construction: it can't
    change while we run, so if it fails host OOM detection stays off and ``check``
    is a no-op for the life of the thread.
    """

    def __init__(self) -> None:
        self._fd = _open_kmsg()
        if self._fd is not None:
            logger.info("Host OOM detection active (reading %s)", _KMSG_PATH)

    def check(self) -> None:
        """Report any host OOM kills seen since the last check; no-op if unavailable."""
        if self._fd is None:
            return
        for kill in _drain_kmsg_oom_kills(self._fd):
            logger.warning(
                "TODO: make this a notification — the host OOM killer killed process %d (%s); "
                "the machine is out of memory",
                kill.pid,
                kill.comm,
            )


def _parse_podman_event(line: bytes) -> _ContainerOomKill | None:
    """Return a container OOM kill parsed from one ``podman events`` JSON line, or None.

    ``podman events --format json`` emits one JSON object per line. We only ask
    for ``event=oom`` container events, so every well-formed line is a kill; we
    just pull the container's id and name out of it (tolerating the field-name
    casing differences seen across podman versions).
    """
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring unparseable `podman events` line: %r", text[:200])
        return None
    if not isinstance(obj, dict):
        return None
    container_id = obj.get("ID") or obj.get("Id") or obj.get("id") or ""
    name = obj.get("Name") or obj.get("name") or ""
    if not container_id:
        return None
    return _ContainerOomKill(container_id=str(container_id), container_name=str(name))


def _read_events_chunk(fd: int) -> bytes | None:
    """Return the next chunk of ``podman events`` output, or None if the pipe would block.

    Unlike /dev/kmsg this is a raw byte stream, so a chunk may hold part of a line,
    several lines, or a line split across reads — the caller reassembles them. An
    empty ``bytes`` means EOF (the events process exited); None means nothing is
    available right now.
    """
    try:
        return os.read(fd, _PODMAN_EVENTS_READ_BYTES)  # b"" at EOF
    except BlockingIOError:
        return None


class _PodmanOomReader:
    """Streams per-app (cgroup-limit) OOM kills from a long-lived ``podman events``.

    We stream events rather than poll ``podman inspect`` because ``--restart``
    resets ``OOMKilled`` to false on the restarted run, so a poll races the restart
    and can miss the kill; the event is emitted the instant the kill happens and
    delivered exactly once. If podman is missing, ``drain`` retries the start on a
    later tick. If the stream ends (podman restarted), ``drain`` reconnects — kills
    during that brief gap are missed, acceptable for a rare podman outage.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = b""
        # Suppress repeated "cannot start" warnings while podman stays absent.
        self._start_warned = False
        self._start()

    def _start(self) -> None:
        """(Re)start the ``podman events`` stream; leaves ``_proc`` None on failure."""
        try:
            proc = subprocess.Popen(
                [
                    "podman",
                    "events",
                    "--filter",
                    "event=oom",
                    "--filter",
                    "type=container",
                    "--format",
                    "json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            if not self._start_warned:
                logger.warning("Cannot start `podman events` for per-app OOM detection: %s", e)
                self._start_warned = True
            self._proc = None
            return
        assert proc.stdout is not None
        # Non-blocking so ``drain`` reads whatever is available and returns rather
        # than blocking the guard loop until the next event arrives.
        os.set_blocking(proc.stdout.fileno(), False)
        self._proc = proc
        self._buf = b""
        self._start_warned = False
        logger.info("Per-app OOM detection active (streaming `podman events`)")

    def _reconnect(self) -> None:
        """Reap the ended stream and start a fresh one."""
        if self._proc is not None:
            if self._proc.stdout is not None:
                self._proc.stdout.close()
            self._proc.wait()  # Already exited (we hit EOF); reap the zombie.
            self._proc = None
        logger.warning("`podman events` stream ended; reconnecting for per-app OOM detection")
        self._start()

    def drain(self) -> list[_ContainerOomKill]:
        """Return per-app OOM kills seen since the last drain; reconnect if the stream ended."""
        if self._proc is None:
            self._start()
            if self._proc is None:
                return []
        assert self._proc.stdout is not None
        fd = self._proc.stdout.fileno()

        while chunk := _read_events_chunk(fd):
            self._buf += chunk
        # The loop stops on the first falsy read: b"" is EOF (the events process
        # exited), None is "nothing available right now" but the stream's still open.
        stream_ended = chunk == b""

        lines = self._buf.split(b"\n")
        # The last element is a trailing partial line (or b"" if we stopped on a
        # newline); hold it for the next drain. On stream end there's nothing more
        # coming on this fd, so parse everything and drop the buffer.
        self._buf = b"" if stream_ended else lines.pop()

        kills: list[_ContainerOomKill] = []
        for line in lines:
            kill = _parse_podman_event(line)
            if kill is not None:
                kills.append(kill)

        if stream_ended:
            self._reconnect()
        return kills


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
        if percent is None:
            # Not running / stats unavailable — nothing to compare against. Leave
            # the debounce state untouched; an OOM crash is handled via podman events.
            return

        app_id = row["app_id"]
        if percent < _MEMORY_CLEAR_PERCENT:
            # Recovered well below the line: re-arm so a future spike (or a
            # dismissed notification) can notify again.
            self._pressure_notified.discard(app_id)
            return

        # Notify only on the first crossing into the pressure zone; the debounce
        # set keeps us quiet while it stays high (and a dismissed one dismissed).
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
    # Built here so all its state is owned by this thread (see _MemoryGuard).
    guard = _MemoryGuard()
    # The one place errors are swallowed: a bad tick is logged and the loop keeps
    # going, so everything downstream is free to raise rather than degrade.
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
