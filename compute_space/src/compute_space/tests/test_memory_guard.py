import errno
import json
import sqlite3
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
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


def _make_guard() -> memory_guard._MemoryGuard:
    """A _MemoryGuard with both OOM readers stubbed inert (no /dev/kmsg, no podman in tests)."""
    with (
        patch("compute_space.core.memory_guard._open_kmsg", return_value=None),
        patch.object(memory_guard._PodmanOomReader, "_start", lambda self: setattr(self, "_proc", None)),
    ):
        return memory_guard._MemoryGuard()


def _kmsg_record(message: str, seq: int = 1) -> bytes:
    """Build a raw /dev/kmsg record: '<prio>,<seq>,<ts>,<flags>;<message>'."""
    return f"6,{seq},123456,-;{message}".encode()


def _oom_event_line(container_id: str, name: str) -> bytes:
    """Build one `podman events --format json` line for a container OOM kill."""
    return (json.dumps({"ID": container_id, "Name": name, "Status": "oom", "Type": "container"}) + "\n").encode()


def test_pressure_warns_once_then_debounces_until_recovery() -> None:
    guard = _make_guard()
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
    guard = _make_guard()
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
    guard = _make_guard()
    row = _app_row()
    # Crossing the warn threshold notifies once.
    assert _pressure_warn_count(guard, row, 92.0) == 1
    # Dancing around the line (89/92/88/91) and anywhere in [80, 90) must neither
    # re-notify nor re-arm — a single sustained notification.
    for pct in (88.0, 91.0, 85.0, 89.9, 80.0):
        assert _pressure_warn_count(guard, row, pct) == 0
    assert "a1" in guard._pressure_notified
    # A dip below the clear threshold re-arms (no warning on the way down)...
    assert _pressure_warn_count(guard, row, 79.0) == 0
    assert "a1" not in guard._pressure_notified
    # ...so a later climb back over the warn threshold notifies again.
    assert _pressure_warn_count(guard, row, 95.0) == 1


def test_pressure_unknown_usage_is_ignored() -> None:
    guard = _make_guard()
    row = _app_row()
    with (
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(None)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard._check_memory_pressure(row)
    assert warn.call_count == 0


# ─── per-app OOM (podman events) ──────────────────────────────────────────────


def test_report_container_ooms_maps_to_app_and_falls_back() -> None:
    guard = _make_guard()
    rows = [_app_row(app_id="a1", name="myapp")]  # container_id == "cid"
    known = memory_guard._ContainerOomKill(container_id="cid", container_name="myapp-ctr")
    unknown = memory_guard._ContainerOomKill(container_id="gone", container_name="ghost-ctr")
    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        guard._report_container_ooms([known, unknown], rows)
    assert warn.call_count == 2
    joined = " ".join(str(call.args) for call in warn.call_args_list)
    # The mapped kill names the app and its limit; the unmapped one falls back to
    # the container name from the event.
    assert "myapp" in joined
    assert "ghost-ctr" in joined


def test_report_container_ooms_empty_is_noop() -> None:
    guard = _make_guard()
    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        guard._report_container_ooms([], [_app_row()])
    warn.assert_not_called()


def test_parse_podman_event_extracts_container() -> None:
    kill = memory_guard._parse_podman_event(_oom_event_line("abc123", "myapp"))
    assert kill == memory_guard._ContainerOomKill(container_id="abc123", container_name="myapp")


def test_parse_podman_event_tolerates_lowercase_keys() -> None:
    line = (json.dumps({"id": "abc", "name": "myapp", "status": "oom"}) + "\n").encode()
    kill = memory_guard._parse_podman_event(line)
    assert kill is not None
    assert kill.container_id == "abc"
    assert kill.container_name == "myapp"


def test_parse_podman_event_ignores_blank_and_bad_json() -> None:
    assert memory_guard._parse_podman_event(b"   ") is None
    with patch("compute_space.core.memory_guard.logger.warning") as warn:
        assert memory_guard._parse_podman_event(b"not json") is None
    warn.assert_called_once()
    # An event with no container id is not actionable.
    assert memory_guard._parse_podman_event((json.dumps({"Status": "oom"}) + "\n").encode()) is None


def _fake_events_proc(fileno: int = 7) -> MagicMock:
    proc = MagicMock()
    proc.stdout.fileno.return_value = fileno
    proc.poll.return_value = None
    return proc


def _make_events_reader(proc: MagicMock | None = None) -> tuple[memory_guard._PodmanOomReader, MagicMock]:
    """Construct a _PodmanOomReader whose `podman events` process is a controllable mock."""
    proc = proc or _fake_events_proc()
    with (
        patch("compute_space.core.memory_guard.subprocess.Popen", return_value=proc),
        patch("compute_space.core.memory_guard.os.set_blocking"),
    ):
        reader = memory_guard._PodmanOomReader()
    return reader, proc


def test_podman_oom_drain_parses_events_until_blocking() -> None:
    reader, _ = _make_events_reader()
    reads = [_oom_event_line("cid1", "app1"), _oom_event_line("cid2", "app2"), BlockingIOError()]
    with patch("compute_space.core.memory_guard.os.read", side_effect=reads):
        kills = reader.drain()
    assert [(k.container_id, k.container_name) for k in kills] == [("cid1", "app1"), ("cid2", "app2")]


def test_podman_oom_drain_buffers_partial_lines_across_ticks() -> None:
    reader, _ = _make_events_reader()
    full = _oom_event_line("cid1", "app1")
    head, tail = full[:12], full[12:]
    # First tick delivers only half a line: nothing to report, remainder buffered.
    with patch("compute_space.core.memory_guard.os.read", side_effect=[head, BlockingIOError()]):
        assert reader.drain() == []
    # Next tick delivers the rest, completing the line.
    with patch("compute_space.core.memory_guard.os.read", side_effect=[tail, BlockingIOError()]):
        kills = reader.drain()
    assert [k.container_id for k in kills] == ["cid1"]


def test_podman_oom_drain_reconnects_on_stream_end() -> None:
    first = _fake_events_proc()
    reader, _ = _make_events_reader(proc=first)
    with (
        patch("compute_space.core.memory_guard.subprocess.Popen", return_value=_fake_events_proc()) as popen,
        patch("compute_space.core.memory_guard.os.set_blocking"),
        patch("compute_space.core.memory_guard.os.read", side_effect=[b""]),  # EOF
        patch("compute_space.core.memory_guard.logger.warning"),
    ):
        kills = reader.drain()
    assert kills == []
    # The dead stream was reaped and a fresh one started.
    first.wait.assert_called_once()
    popen.assert_called_once()
    assert reader._proc is not None


def test_podman_oom_drain_disabled_when_podman_missing() -> None:
    # podman absent: construction can't start the stream, and drain stays a no-op
    # (retrying the start) rather than raising.
    with (
        patch("compute_space.core.memory_guard.subprocess.Popen", side_effect=FileNotFoundError()),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        reader = memory_guard._PodmanOomReader()
        assert reader.drain() == []
    # Warned once about the missing binary, not once per retry.
    assert warn.call_count == 1


# ─── host OOM (kmsg) ──────────────────────────────────────────────────────────


def test_parse_kmsg_oom_global_kill() -> None:
    record = _kmsg_record("Out of memory: Killed process 4242 (python3) total-vm:1024kB, anon-rss:512kB")
    event = memory_guard._parse_kmsg_oom(record.decode())
    assert event is not None
    assert event.pid == 4242
    assert event.comm == "python3"


def test_parse_kmsg_oom_skips_memcg_kill() -> None:
    # A cgroup-limit kill (an app hitting its --memory) is handled per-app, not here.
    record = _kmsg_record("Memory cgroup out of memory: Killed process 99 (node)")
    assert memory_guard._parse_kmsg_oom(record.decode()) is None


def test_parse_kmsg_oom_ignores_non_oom_lines() -> None:
    assert memory_guard._parse_kmsg_oom(_kmsg_record("usb 1-1: new high-speed USB device").decode()) is None
    assert memory_guard._parse_kmsg_oom("malformed record with no semicolon") is None


def test_drain_kmsg_reads_until_blocking() -> None:
    records = [
        _kmsg_record("Out of memory: Killed process 1 (aaa)", seq=10),
        _kmsg_record("some unrelated kernel message", seq=11),
        _kmsg_record("Out of memory: Killed process 2 (bbb)", seq=12),
    ]
    with patch("compute_space.core.memory_guard.os.read", side_effect=[*records, BlockingIOError()]):
        kills = memory_guard._drain_kmsg_oom_kills(fd=7)
    assert [(k.pid, k.comm) for k in kills] == [(1, "aaa"), (2, "bbb")]


def test_drain_kmsg_reraises_unexpected_errors() -> None:
    # A non-EPIPE read error is not swallowed — it reaches the top-level handler.
    with patch("compute_space.core.memory_guard.os.read", side_effect=OSError(errno.EIO, "I/O error")):
        with pytest.raises(OSError):
            memory_guard._drain_kmsg_oom_kills(fd=7)


def test_host_oom_reader_warns_per_kill_once() -> None:
    record = _kmsg_record("Out of memory: Killed process 4242 (python3)")
    with patch("compute_space.core.memory_guard._open_kmsg", return_value=7):
        reader = memory_guard._HostOomReader()
    with (
        patch("compute_space.core.memory_guard.os.read", side_effect=[record, BlockingIOError()]),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        reader.check()
    assert warn.call_count == 1
    assert warn.call_args.args[1] == 4242
    assert warn.call_args.args[2] == "python3"

    # A subsequent tick with no new records emits nothing (read once, report once).
    with (
        patch("compute_space.core.memory_guard.os.read", side_effect=[BlockingIOError()]),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        reader.check()
    assert warn.call_count == 0


def test_host_oom_reader_checks_capability_once_at_construction() -> None:
    # No /dev/kmsg (or no CAP_SYSLOG): the reader opens once at construction and
    # then every check() is a no-op — it never retries the open or reads.
    with patch("compute_space.core.memory_guard._open_kmsg", return_value=None) as open_kmsg:
        reader = memory_guard._HostOomReader()
    with (
        patch("compute_space.core.memory_guard.os.read") as read,
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        reader.check()
        reader.check()
    open_kmsg.assert_called_once()
    read.assert_not_called()
    warn.assert_not_called()


# ─── check_once fan-out ───────────────────────────────────────────────────────


def _fake_config(db_path: str) -> Config:
    # ensure_memory_guard / check_once only read config.db_path; a stub avoids
    # building a full Config.
    return cast(Config, attr.make_class("_Cfg", ["db_path"], frozen=True)(db_path=db_path))


def test_check_once_reports_container_oom_and_pressure(tmp_path: Path) -> None:
    db_path = str(tmp_path / "apps.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE apps (app_id, name, container_id, cpu_cores, memory_mb)")
    conn.execute("INSERT INTO apps VALUES ('a1', 'myapp', 'cid1', 0.5, 128)")
    conn.commit()
    conn.close()

    guard = _make_guard()
    kill = memory_guard._ContainerOomKill(container_id="cid1", container_name="myapp-ctr")
    with (
        patch.object(guard._host_oom, "check"),
        patch.object(guard._podman_oom, "drain", return_value=[kill]),
        patch("compute_space.core.memory_guard.collect_app_resources", return_value=_usage(95.0)),
        patch("compute_space.core.memory_guard.logger.warning") as warn,
    ):
        guard.check_once(_fake_config(db_path))
    messages = [call.args[0] for call in warn.call_args_list]
    # Both the OOM event (mapped to the app) and the pressure warning fired.
    assert any("OOM killer" in m for m in messages)
    assert any("memory limit" in m and "keeps climbing" in m for m in messages)


def test_ensure_memory_guard_starts_thread() -> None:
    with patch("compute_space.core.memory_guard.threading.Thread") as thread:
        memory_guard.ensure_memory_guard(_fake_config("/tmp/start.db"))
    # Starts the loop thread and records it as the single guard thread. The OOM
    # readers (kmsg capability check + podman events stream) are created inside
    # that thread, so ensure_memory_guard itself never touches them.
    thread.assert_called_once()
    assert thread.call_args.kwargs["target"] is memory_guard._memory_guard_loop
    thread.return_value.start.assert_called_once()
    assert memory_guard._guard_thread is thread.return_value


def test_ensure_memory_guard_starts_only_one_thread() -> None:
    with patch("compute_space.core.memory_guard.threading.Thread") as thread:
        thread.return_value.is_alive.return_value = True
        # Repeated calls — even with a different config — never start a second thread.
        memory_guard.ensure_memory_guard(_fake_config("/tmp/a.db"))
        memory_guard.ensure_memory_guard(_fake_config("/tmp/b.db"))
    assert thread.call_count == 1
    thread.return_value.start.assert_called_once()
