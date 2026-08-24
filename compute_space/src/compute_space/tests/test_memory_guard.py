import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import attr
import pytest

from compute_space.config import Config
from compute_space.core import memory_guard
from compute_space.core.diagnostics import AppResourceUsage


def _row(**kwargs: object) -> sqlite3.Row:
    """Build a sqlite3.Row with the given columns (mirrors the guard's query)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(kwargs)
    placeholders = ", ".join(["?"] * len(kwargs))
    conn.execute(f"CREATE TABLE apps ({cols})")
    conn.execute(f"INSERT INTO apps ({cols}) VALUES ({placeholders})", tuple(kwargs.values()))
    row: sqlite3.Row = conn.execute("SELECT * FROM apps").fetchone()
    return row


def _app_row(app_id: str = "a1", name: str = "myapp") -> sqlite3.Row:
    return _row(app_id=app_id, name=name, container_id="cid", cpu_cores=0.5, memory_mb=128)


def _usage(percent: float | None) -> AppResourceUsage:
    return AppResourceUsage(
        running=percent is not None,
        cpu_percent=None,
        memory_usage_bytes=100,
        memory_limit_bytes=128 * 1024 * 1024,
        memory_percent=percent,
        cpu_cores_limit=0.5,
        memory_mb_limit=128,
    )


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    memory_guard._guard_thread = None


def _guard() -> memory_guard._MemoryGuard:
    """A guard with no live streams — construction no longer starts any subprocess."""
    return memory_guard._MemoryGuard()


def _journal_line(message: str) -> bytes:
    """Build one `journalctl --output=json` line carrying a kernel MESSAGE."""
    return json.dumps({"MESSAGE": message, "_TRANSPORT": "kernel"}).encode()


def _oom_event_line(container_id: str, name: str) -> bytes:
    """Build one `podman events --format json` line for a container OOM kill."""
    return json.dumps({"ID": container_id, "Name": name, "Status": "oom", "Type": "container"}).encode()


async def _agen(items: list[bytes]) -> AsyncIterator[bytes]:
    for it in items:
        yield it


# ─── per-app memory pressure debounce ─────────────────────────────────────────


def test_pressure_warns_once_then_debounces_until_recovery() -> None:
    guard = _guard()
    row = _app_row()
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(95.0)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
        guard._check_memory_pressure(row)  # still high -> debounced
    assert warn.call_count == 1
    assert "myapp" in warn.call_args.args[1]

    # Drops back below the threshold: no warning, and the debounce is cleared.
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(40.0)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    assert warn.call_count == 0
    assert "a1" not in guard._pressure_notified

    # A fresh spike notifies again.
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(99.0)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    assert warn.call_count == 1


def test_pressure_below_threshold_never_warns() -> None:
    guard = _guard()
    row = _app_row()
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(89.9)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    assert warn.call_count == 0


def _pressure_warn_count(guard: memory_guard._MemoryGuard, row: sqlite3.Row, percent: float) -> int:
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(percent)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    return warn.call_count


def test_pressure_debounce_holds_through_band_and_rearms_below_clear() -> None:
    guard = _guard()
    row = _app_row()
    assert _pressure_warn_count(guard, row, 92.0) == 1
    for pct in (88.0, 91.0, 85.0, 89.9, 80.0):
        assert _pressure_warn_count(guard, row, pct) == 0
    assert "a1" in guard._pressure_notified
    assert _pressure_warn_count(guard, row, 79.0) == 0
    assert "a1" not in guard._pressure_notified
    assert _pressure_warn_count(guard, row, 95.0) == 1


def test_pressure_unknown_usage_is_ignored() -> None:
    guard = _guard()
    row = _app_row()
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(None)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    assert warn.call_count == 0


def test_check_pressure_once_checks_every_container_row() -> None:
    guard = _guard()
    rows = [_app_row(app_id="a1", name="one"), _app_row(app_id="a2", name="two")]
    with (
        patch("compute_space.core.memory_guard._query_container_rows", return_value=rows),
        patch.object(guard, "_check_memory_pressure") as check,
    ):
        guard._check_pressure_once(cast(Config, object()))
    assert check.call_count == 2


# ─── per-app OOM (podman events) ──────────────────────────────────────────────


def test_report_container_ooms_maps_to_app_and_falls_back() -> None:
    guard = _guard()
    rows = [_app_row(app_id="a1", name="myapp")]  # container_id == "cid"
    known = memory_guard._ContainerOomKill(container_id="cid", container_name="myapp-ctr")
    unknown = memory_guard._ContainerOomKill(container_id="gone", container_name="ghost-ctr")
    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        guard._report_container_ooms([known, unknown], rows)
    assert warn.call_count == 2
    joined = " ".join(str(call.args) for call in warn.call_args_list)
    assert "myapp" in joined
    assert "ghost-ctr" in joined


def test_report_container_ooms_empty_is_noop() -> None:
    guard = _guard()
    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        guard._report_container_ooms([], [_app_row()])
    warn.assert_not_called()


def test_parse_podman_event_extracts_container() -> None:
    kill = memory_guard._parse_podman_event(_oom_event_line("abc123", "myapp"))
    assert kill == memory_guard._ContainerOomKill(container_id="abc123", container_name="myapp")


def test_parse_podman_event_raises_on_unrecognized_shape() -> None:
    # We commit to podman's documented top-level ID/Name keys and fail loudly on
    # anything else — every line is an OOM event, so a shape change we didn't parse
    # is a dropped kill, and we'd rather see it than silently lose it.
    with pytest.raises(ValueError, match="not json"):
        memory_guard._parse_podman_event(b"not json")
    with pytest.raises(ValueError):  # valid JSON, but a list rather than an object
        memory_guard._parse_podman_event(b'[{"ID": "abc", "Name": "x"}]')
    with pytest.raises(ValueError):  # object missing the ID key
        memory_guard._parse_podman_event(json.dumps({"Name": "x", "Status": "oom"}).encode())
    with pytest.raises(ValueError):  # lowercase keys are no longer tolerated
        memory_guard._parse_podman_event(json.dumps({"id": "abc", "name": "x"}).encode())


def test_run_podman_oom_reports_kill_and_maps_app() -> None:
    guard = _guard()
    rows = [_app_row(app_id="a1", name="myapp")]  # container_id == "cid"

    def fake_follow(argv: list[str], detector: str) -> AsyncIterator[bytes]:
        return _agen([_oom_event_line("cid", "myapp-ctr")])

    with (
        patch("compute_space.core.memory_guard._follow_lines", fake_follow),
        patch("compute_space.core.memory_guard._query_container_rows", return_value=rows),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        asyncio.run(guard._run_podman_oom(cast(Config, object())))
    assert warn.call_count == 1
    assert "myapp" in warn.call_args.args[1]


def test_run_podman_oom_skips_bad_event_and_keeps_going() -> None:
    # A shape we can't parse is logged and skipped, not raised: raising would fail
    # run()'s gather and take down the sibling detectors. A later valid event still lands.
    guard = _guard()
    rows = [_app_row(app_id="a1", name="myapp")]  # container_id == "cid"

    def fake_follow(argv: list[str], detector: str) -> AsyncIterator[bytes]:
        return _agen([b'{"unexpected": true}', _oom_event_line("cid", "myapp-ctr")])

    with (
        patch("compute_space.core.memory_guard._follow_lines", fake_follow),
        patch("compute_space.core.memory_guard._query_container_rows", return_value=rows),
        patch("compute_space.core.memory_guard.logger.exception") as log_exc,
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        asyncio.run(guard._run_podman_oom(cast(Config, object())))
    log_exc.assert_called_once()  # the unparseable line surfaced, not silently dropped
    assert warn.call_count == 1  # and the loop survived to report the good one
    assert "myapp" in warn.call_args.args[1]


# ─── host OOM (journalctl) ────────────────────────────────────────────────────


def test_parse_journal_oom_global_kill() -> None:
    line = _journal_line("Out of memory: Killed process 4242 (python3) total-vm:1024kB, anon-rss:512kB")
    event = memory_guard._parse_journal_oom(line)
    assert event is not None
    assert event.pid == 4242
    assert event.comm == "python3"


def test_parse_journal_oom_skips_memcg_kill() -> None:
    line = _journal_line("Memory cgroup out of memory: Killed process 99 (node)")
    assert memory_guard._parse_journal_oom(line) is None


def test_parse_journal_oom_ignores_non_oom_and_malformed_lines() -> None:
    assert memory_guard._parse_journal_oom(_journal_line("usb 1-1: new high-speed USB device")) is None
    assert memory_guard._parse_journal_oom(b"not json") is None
    assert memory_guard._parse_journal_oom(json.dumps({"_TRANSPORT": "kernel"}).encode()) is None


def test_run_host_oom_warns_only_for_oom_lines() -> None:
    guard = _guard()
    lines = [
        _journal_line("usb 1-1: new high-speed USB device"),  # unrelated kernel line
        _journal_line("Out of memory: Killed process 4242 (python3)"),  # a global OOM kill
    ]

    def fake_follow(argv: list[str], detector: str) -> AsyncIterator[bytes]:
        return _agen(lines)

    with (
        patch("compute_space.core.memory_guard._follow_lines", fake_follow),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        asyncio.run(guard._run_host_oom())
    assert warn.call_count == 1  # only the OOM line, not the unrelated one
    assert warn.call_args.args[1] == 4242
    assert warn.call_args.args[2] == "python3"


# ─── _follow_lines: the shared reconnect / missing-binary behaviour ───────────


def test_follow_lines_skips_blanks_and_reconnects_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    # First connection: a real line then a blank (skipped), then EOF. It reconnects
    # and the second connection supplies the next line.
    streams = iter([_agen([b"a", b""]), _agen([b"b"])])

    def fake_stream(argv: list[str], *, merge_stderr: bool, stderr_sink: object = None) -> AsyncIterator[bytes]:
        return next(streams)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(memory_guard, "stream_process_lines", fake_stream)
    monkeypatch.setattr("asyncio.sleep", no_sleep)

    async def run() -> list[bytes]:
        out: list[bytes] = []
        async for line in memory_guard._follow_lines(["x"], "detector"):
            out.append(line)
            if len(out) >= 2:
                break
        return out

    assert asyncio.run(run()) == [b"a", b"b"]


def test_follow_lines_surfaces_stderr_reason_when_stream_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    # A follow that ends after writing to stderr (e.g. journalctl "permission denied"
    # because the journal grant didn't take) names that reason in the reconnect
    # warning instead of a bare "stream ended".
    class _Stop(Exception):
        pass

    def fake_stream(argv: list[str], *, merge_stderr: bool, stderr_sink: object = None) -> AsyncIterator[bytes]:
        async def g() -> AsyncIterator[bytes]:
            assert callable(stderr_sink)
            stderr_sink(b"Failed to open journal: Permission denied")
            return
            yield b""  # pragma: no cover - marks g as an async generator

        return g()

    async def breaking_sleep(_seconds: float) -> None:
        raise _Stop  # bail out after the first reconnect pause

    monkeypatch.setattr(memory_guard, "stream_process_lines", fake_stream)
    monkeypatch.setattr("asyncio.sleep", breaking_sleep)

    async def run() -> None:
        async for _ in memory_guard._follow_lines(["journalctl"], "Host OOM detection"):
            pass

    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        with pytest.raises(_Stop):
            asyncio.run(run())
    joined = " ".join(str(c.args) for c in warn.call_args_list)
    assert "Permission denied" in joined


def test_follow_lines_missing_binary_warns_once_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stop(Exception):
        pass

    def fake_stream(argv: list[str], *, merge_stderr: bool, stderr_sink: object = None) -> AsyncIterator[bytes]:
        async def g() -> AsyncIterator[bytes]:
            raise FileNotFoundError
            yield b""  # pragma: no cover - marks g as an async generator

        return g()

    sleeps = {"n": 0}

    async def breaking_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise _Stop  # bail out of the otherwise-infinite retry loop

    monkeypatch.setattr(memory_guard, "stream_process_lines", fake_stream)
    monkeypatch.setattr("asyncio.sleep", breaking_sleep)

    async def run() -> None:
        async for _ in memory_guard._follow_lines(["missing-binary"], "detector"):
            pass

    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        with pytest.raises(_Stop):
            asyncio.run(run())
    # Warned once about the missing binary despite several retries.
    missing_warnings = [c for c in warn.call_args_list if "not installed" in c.args[0]]
    assert len(missing_warnings) == 1


# ─── query + thread lifecycle ─────────────────────────────────────────────────


def _fake_config(db_path: str) -> Config:
    return cast(Config, attr.make_class("_Cfg", ["db_path"], frozen=True)(db_path=db_path))


def test_query_container_rows_only_returns_apps_with_a_container(tmp_path: Path) -> None:
    db_path = str(tmp_path / "apps.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE apps (app_id, name, container_id, cpu_cores, memory_mb)")
    conn.execute("INSERT INTO apps VALUES ('a1', 'has', 'cid1', 0.5, 128)")
    conn.execute("INSERT INTO apps VALUES ('a2', 'none', NULL, 0.5, 128)")
    conn.commit()
    conn.close()

    rows = memory_guard._query_container_rows(_fake_config(db_path))
    assert [r["app_id"] for r in rows] == ["a1"]


def test_ensure_memory_guard_starts_thread() -> None:
    with patch("compute_space.core.memory_guard.threading.Thread") as thread:
        memory_guard.ensure_memory_guard(_fake_config("/tmp/start.db"))
    thread.assert_called_once()
    assert thread.call_args.kwargs["target"] is memory_guard._memory_guard_loop
    thread.return_value.start.assert_called_once()
    assert memory_guard._guard_thread is thread.return_value


def test_ensure_memory_guard_starts_only_one_thread() -> None:
    with patch("compute_space.core.memory_guard.threading.Thread") as thread:
        thread.return_value.is_alive.return_value = True
        memory_guard.ensure_memory_guard(_fake_config("/tmp/a.db"))
        memory_guard.ensure_memory_guard(_fake_config("/tmp/b.db"))
    assert thread.call_count == 1
    thread.return_value.start.assert_called_once()
