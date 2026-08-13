"""Unit tests for compute_space.core.log_stream.

The build-log ``tail`` command is exercised for real against temp files — including
the fail-loud unreadable case. The container follow (``podman logs``) and the app's
DB state (``_get_state``) are faked so the tests run without a live podman or database
and deterministically.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path

import pytest

from compute_space.core import log_stream
from compute_space.core.log_stream import stream_app_logs
from compute_space.core.process_stream import stream_process_lines

_State = tuple[str, str | None]


async def _collect(gen: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in gen]


async def _collect_bytes(gen: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in gen]


def _fake_state(monkeypatch: pytest.MonkeyPatch, get: Callable[[], _State]) -> None:
    """Make ``_get_state(app_id)`` return whatever ``get()`` yields (ignoring app_id)."""
    monkeypatch.setattr(log_stream, "_get_state", lambda _app_id: get())


def _fake_build_stream(monkeypatch: pytest.MonkeyPatch, lines: list[bytes]) -> None:
    async def fake_stream(
        argv: list[str], *, merge_stderr: bool, raise_on_error: bool = False
    ) -> AsyncIterator[bytes]:
        for line in lines:
            yield line

    monkeypatch.setattr(log_stream, "stream_process_lines", fake_stream)


def _fake_podman(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    async def fake_follow(container_id: str, tail_lines: int) -> AsyncIterator[str]:
        for line in lines:
            yield line

    monkeypatch.setattr(log_stream, "_podman_follow", fake_follow)


# ─── the build-log tail command (real tail) ───────────────────────────────────


def test_tail_reads_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "docker.log"
    p.write_text("build 1\nbuild 2\n")
    argv = log_stream._tail_argv(str(p), follow=False)
    lines = asyncio.run(_collect_bytes(stream_process_lines(argv, merge_stderr=True, raise_on_error=True)))
    assert lines == [b"build 1", b"build 2"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission checks")
def test_tail_unreadable_fails_loud(tmp_path: Path) -> None:
    # Exists but unreadable (EACCES): tail exits non-zero, which raise_on_error
    # surfaces rather than papering over as an empty log.
    p = tmp_path / "docker.log"
    p.write_text("secret\n")
    p.chmod(0o000)
    argv = log_stream._tail_argv(str(p), follow=False)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(_collect_bytes(stream_process_lines(argv, merge_stderr=False, raise_on_error=True)))
    finally:
        p.chmod(0o644)


# ─── container follow: ANSI stripping ─────────────────────────────────────────


def test_podman_follow_strips_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container lines are ANSI-stripped so the browser shows the same text the old
    one-shot podman-logs call produced. The spawn/reap is the shared helper's job
    (tested there); here we fake it to feed raw lines."""
    raw = [b"plain line", b"\x1b[31mred\x1b[0m text", b""]

    async def fake_stream(
        argv: list[str], *, merge_stderr: bool, raise_on_error: bool = False
    ) -> AsyncIterator[bytes]:
        assert "--follow" in argv and "--timestamps" not in argv
        assert merge_stderr is True
        for line in raw:
            yield line

    monkeypatch.setattr(log_stream, "stream_process_lines", fake_stream)

    async def run() -> list[str]:
        return [line async for line in log_stream._podman_follow("cid", 2000)]

    assert asyncio.run(run()) == ["plain line", "red text", ""]


# ─── build→container orchestration ────────────────────────────────────────────


def test_stream_app_logs_running_emits_build_then_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build = tmp_path / "docker.log"
    build.write_text("present")  # must exist for the one-shot existence guard
    _fake_build_stream(monkeypatch, [b"build 1", b"build 2"])
    _fake_podman(monkeypatch, ["container a", "container b"])
    _fake_state(monkeypatch, lambda: ("running", "cid123"))

    chunks = asyncio.run(_collect(stream_app_logs("app-1", str(build))))
    assert chunks == ["build 1", "build 2", "=== Container logs ===", "container a", "container b"]


def test_stream_app_logs_running_missing_build_log_shows_container_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # No docker.log on disk: the build tail is skipped entirely (no subprocess), and
    # only the container logs are streamed.
    _fake_podman(monkeypatch, ["container a"])
    _fake_state(monkeypatch, lambda: ("running", "cid123"))

    chunks = asyncio.run(_collect(stream_app_logs("app-1", "/no/such/docker.log")))
    assert chunks == ["=== Container logs ===", "container a"]


def test_stream_app_logs_building_then_container_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_follow_build(app_id: str, build_log_path: str) -> AsyncIterator[str]:
        for line in ("building...", "more build output"):
            yield line

    monkeypatch.setattr(log_stream, "_follow_build_until_container", fake_follow_build)
    _fake_podman(monkeypatch, ["container up"])

    # building at connect; a container has appeared by the time the build follow returns.
    states = iter([("building", None), ("running", "cid9")])
    _fake_state(monkeypatch, lambda: next(states))

    chunks = asyncio.run(_collect(stream_app_logs("app-1", "/p/docker.log")))
    assert chunks == ["building...", "more build output", "=== Container logs ===", "container up"]


def test_follow_build_streams_until_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # While the build keeps going, every build line is yielded until the stream ends.
    build = tmp_path / "docker.log"
    build.write_text("present")  # exists, so the follow skips the wait-for-appear
    _fake_build_stream(monkeypatch, [b"a", b"b"])
    _fake_state(monkeypatch, lambda: ("building", None))

    lines = asyncio.run(_collect(log_stream._follow_build_until_container("app-1", str(build))))
    assert lines == ["a", "b"]


def test_follow_build_hands_off_after_a_quiet_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # State is re-checked only when a read times out — a promptly-arriving line is
    # itself evidence the build is live. So a line already waiting is still forwarded
    # even though the container is up; the follow hands off only once the stream goes
    # quiet and the next read times out.
    monkeypatch.setattr(log_stream, "_BUILD_POLL_SECONDS", 0.01)
    build = tmp_path / "docker.log"
    build.write_text("present")  # exists, so the follow skips the wait-for-appear

    async def one_then_quiet(
        argv: list[str], *, merge_stderr: bool, raise_on_error: bool = False
    ) -> AsyncIterator[bytes]:
        yield b"a"
        await asyncio.sleep(30)  # go quiet so the next read times out (cancelled on aclose)
        yield b"b"

    monkeypatch.setattr(log_stream, "stream_process_lines", one_then_quiet)
    _fake_state(monkeypatch, lambda: ("running", "cid"))  # container already up

    lines = asyncio.run(_collect(log_stream._follow_build_until_container("app-1", str(build))))
    assert lines == ["a"]


def test_follow_build_read_survives_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A line that takes longer than the poll interval to arrive is still delivered:
    # the timed-out read is shielded (kept alive), not cancelled/lost.
    monkeypatch.setattr(log_stream, "_BUILD_POLL_SECONDS", 0.01)
    build = tmp_path / "docker.log"
    build.write_text("present")

    async def slow_stream(
        argv: list[str], *, merge_stderr: bool, raise_on_error: bool = False
    ) -> AsyncIterator[bytes]:
        await asyncio.sleep(0.05)  # several poll intervals with no line
        yield b"late line"

    monkeypatch.setattr(log_stream, "stream_process_lines", slow_stream)
    _fake_state(monkeypatch, lambda: ("building", None))

    lines = asyncio.run(_collect(log_stream._follow_build_until_container("app-1", str(build))))
    assert lines == ["late line"]


def test_follow_build_gives_up_when_build_ends_before_log_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker.log never appears and the build ends: return cleanly with no lines and
    # without ever spawning tail.
    monkeypatch.setattr(log_stream, "_BUILD_POLL_SECONDS", 0)
    states = iter([("building", None), ("error", None)])
    _fake_state(monkeypatch, lambda: next(states))

    lines = asyncio.run(_collect(log_stream._follow_build_until_container("app-1", "/no/such/docker.log")))
    assert lines == []
