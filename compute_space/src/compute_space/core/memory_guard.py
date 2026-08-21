"""Memory guard: warn the owner about memory pressure and OOM kills.

A daemon thread (mirroring the storage guard) that warns when an app nears its
memory limit, when an app's container is OOM-killed (per-app, from ``podman
events``), or when the host runs out of memory and the kernel OOM killer reaps any
process (from the system journal via ``journalctl``).

The two OOM detectors each *follow a subprocess for log lines*, which is the same
job the app-log stream does with ``podman logs --follow``; they share the
:func:`compute_space.core.process_stream.stream_process_lines` primitive. Here we
wrap it in :func:`_follow_lines`, which reconnects when a stream ends and treats a
missing binary (no podman/journalctl on a dev host) as an expected "detector off"
state rather than an error.

The guard runs its own asyncio event loop on its own daemon thread — deliberately
*not* the loop hypercorn serves requests on. It does its blocking work (``podman
stats`` via ``collect_app_resources``, sqlite) inline on that loop: on a loop that
serves nothing else this is fine — a slow pressure check just pushes out its own next
tick and briefly holds off the OOM followers (whose output buffers in the pipe
meanwhile). It only mustn't share hypercorn's loop, where it would stall requests.

The "notification" is only a log line today; the ``TODO:`` markers are where the
real notification system will hook in once it exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import AsyncIterator

import attr

from compute_space.config import Config
from compute_space.core.diagnostics import collect_app_resources
from compute_space.core.logging import logger
from compute_space.core.process_stream import stream_process_lines
from compute_space.core.storage import format_bytes

_MEMORY_GUARD_INTERVAL_SECONDS = 60

_MEMORY_WARN_PERCENT = 90.0
_MEMORY_CLEAR_PERCENT = 80.0

# After a follow stream ends (or its binary is missing) wait this long before
# (re)connecting, so a persistent failure retries steadily instead of spinning.
_STREAM_RECONNECT_SECONDS = 5

# How many trailing stderr lines to keep from a follow, to name the reason it ended.
_STDERR_TAIL_LINES = 5

# The two OOM detectors each follow a subprocess that emits newline-delimited JSON.
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


async def _follow_lines(argv: list[str], detector: str) -> AsyncIterator[bytes]:
    """Follow a long-lived streaming command forever, yielding its non-blank lines.

    Reconnects when the stream ends (the daemon restarted, the journal rotated).
    A **missing binary** — no podman/journalctl, e.g. a macOS dev host — is an
    *expected* state: it's caught, warned once, and retried, so the detector just
    stays off rather than crashing the guard. Anything else (a read/permission
    error) propagates to the guard's top-level handler; we don't paper it over as
    a silent no-op.
    """
    warned_missing = False
    while True:
        # Divert stderr (not into the parsed JSON) so an abnormal end — e.g. the
        # journal grant not taking effect, so journalctl exits "permission denied" —
        # surfaces its real reason below instead of a bare "stream ended".
        recent_stderr: deque[bytes] = deque(maxlen=_STDERR_TAIL_LINES)
        try:
            async for line in stream_process_lines(argv, merge_stderr=False, stderr_sink=recent_stderr.append):
                if line.strip():
                    yield line
        except FileNotFoundError:
            if not warned_missing:
                logger.warning("Cannot start `%s` for %s: not installed; detection disabled", argv[0], detector)
                warned_missing = True
            await asyncio.sleep(_STREAM_RECONNECT_SECONDS)
            continue
        # Clean EOF: the stream ended. It had connected, so re-arm the missing
        # warning and reconnect after a pause, naming any stderr it left behind.
        warned_missing = False
        reason = b" / ".join(recent_stderr).decode("utf-8", "replace").strip()
        if reason:
            logger.warning("`%s` stream ended for %s (%s); reconnecting", argv[0], detector, reason)
        else:
            logger.warning("`%s` stream ended; reconnecting for %s", argv[0], detector)
        await asyncio.sleep(_STREAM_RECONNECT_SECONDS)


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


def _query_container_rows(config: Config) -> list[sqlite3.Row]:
    """Rows for every app that currently has a container (the guard's unit of work)."""
    with contextlib.closing(sqlite3.connect(config.db_path)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT app_id, name, container_id, cpu_cores, memory_mb FROM apps WHERE container_id IS NOT NULL"
        ).fetchall()


class _MemoryGuard:
    """The guard's detectors and per-app pressure debounce state.

    ``run`` drives three concurrent detectors on the guard's own loop, each doing its
    blocking work (podman stats, sqlite) inline since the loop serves nothing else.
    ``_pressure_notified`` (the debounce state) is touched only by the pressure loop,
    so it needs no locking.
    """

    def __init__(self) -> None:
        self._pressure_notified: set[str] = set()

    async def run(self, config: Config) -> None:
        """Run host-OOM, per-app-OOM, and memory-pressure detectors until cancelled."""
        await asyncio.gather(
            self._run_host_oom(),
            self._run_podman_oom(config),
            self._run_pressure_loop(config),
        )

    async def _run_host_oom(self) -> None:
        """Warn on each host-level (global) OOM kill seen on the kernel journal."""
        async for line in _follow_lines(_JOURNALCTL_KERNEL_FOLLOW, "Host OOM detection"):
            kill = _parse_journal_oom(line)
            if kill is not None:
                logger.warning(
                    "TODO: make this a notification — the host OOM killer killed process %d (%s); "
                    "the machine is out of memory",
                    kill.pid,
                    kill.comm,
                )

    async def _run_podman_oom(self, config: Config) -> None:
        """Report each per-app OOM kill, naming the owning app when we can map it.

        A malformed event line is logged and skipped, not propagated: letting the
        ``ValueError`` escape would fail ``run``'s gather and tear down the sibling
        host-OOM and pressure detectors, losing far more than the one dropped kill.
        """
        async for line in _follow_lines(_PODMAN_OOM_EVENTS, "Per-app OOM detection"):
            try:
                kill = _parse_podman_event(line)
            except ValueError:
                logger.exception("Skipping unparseable podman OOM event")
                continue
            rows = _query_container_rows(config)
            self._report_container_ooms([kill], rows)

    async def _run_pressure_loop(self, config: Config) -> None:
        """Every ``_MEMORY_GUARD_INTERVAL_SECONDS``, check each app's memory pressure.

        The check runs inline, so ``podman stats`` blocks the loop for the seconds it
        takes; that just pushes out the next tick (the interval is a floor, not a
        guarantee) and briefly delays the OOM followers, which is fine here.
        """
        while True:
            self._check_pressure_once(config)
            await asyncio.sleep(_MEMORY_GUARD_INTERVAL_SECONDS)

    def _check_pressure_once(self, config: Config) -> None:
        for row in _query_container_rows(config):
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
    """Run the guard's asyncio loop on this dedicated daemon thread.

    A *separate* event loop from the one hypercorn serves requests on (see the module
    docstring). ``_MemoryGuard.run`` only returns by raising — a detector hit an
    unexpected error — so we log it and restart, mirroring the old per-tick handler.
    """
    while True:
        try:
            asyncio.run(_MemoryGuard().run(config))
        except Exception:
            logger.exception("Memory guard crashed; restarting in %ds", _STREAM_RECONNECT_SECONDS)
            time.sleep(_STREAM_RECONNECT_SECONDS)


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
