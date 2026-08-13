"""Unit tests for compute_space.core.log_stream.

The build-log tail command (``sh``/``tail``) is exercised for real against temp
files — including the fail-loud unreadable case — while the container follow
(``podman logs``) and the async build→container orchestration are faked so the
tests run without a live podman and deterministically.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from compute_space.core import log_stream
from compute_space.core.log_stream import stream_app_logs
from compute_space.core.process_stream import stream_process_lines


async def _collect(gen: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in gen]


async def _collect_bytes(gen: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in gen]


# ─── the build-log tail shell command (real sh/tail) ──────────────────────────


def test_build_tail_oneshot_reads_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "docker.log"
    p.write_text("build 1\nbuild 2\n")
    argv = log_stream._build_tail_argv(str(p), follow=False)
    lines = asyncio.run(_collect_bytes(stream_process_lines(argv, merge_stderr=True, raise_on_error=True)))
    assert lines == [b"build 1", b"build 2"]


def test_build_tail_oneshot_missing_file_is_quiet() -> None:
    # docker.log not written yet: expected, so empty and a clean (exit-0) end.
    argv = log_stream._build_tail_argv("/no/such/file.log", follow=False)
    lines = asyncio.run(_collect_bytes(stream_process_lines(argv, merge_stderr=True, raise_on_error=True)))
    assert lines == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission checks")
def test_build_tail_unreadable_fails_loud(tmp_path: Path) -> None:
    # Exists but unreadable (EACCES): tail exits non-zero, which raise_on_error
    # surfaces rather than papering over as an empty log.
    p = tmp_path / "docker.log"
    p.write_text("secret\n")
    p.chmod(0o000)
    argv = log_stream._build_tail_argv(str(p), follow=False)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(_collect_bytes(stream_process_lines(argv, merge_stderr=False, raise_on_error=True)))
    finally:
        p.chmod(0o644)


# ─── container follow: timestamp + ANSI stripping ─────────────────────────────


def test_podman_follow_strips_timestamp_and_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    """--timestamps prepends an RFC3339 stamp to each line; that and ANSI colour
    codes are stripped so the browser shows the same text the old podman-logs call
    produced. The spawn/reap is the shared helper's job (tested there); here we fake
    it to feed raw lines."""
    raw = [
        b"2026-07-30T15:27:34.860265157+00:00 plain line",
        b"2026-07-30T15:27:35.000000000+00:00 \x1b[31mred\x1b[0m text",
        b"2026-07-30T15:27:36.000000000+00:00 ",  # empty content
    ]

    async def fake_stream(
        argv: list[str], *, merge_stderr: bool, raise_on_error: bool = False
    ) -> AsyncIterator[bytes]:
        assert "--follow" in argv and "--timestamps" in argv
        assert merge_stderr is True
        for line in raw:
            yield line

    monkeypatch.setattr(log_stream, "stream_process_lines", fake_stream)

    async def run() -> list[str]:
        return [line async for line in log_stream._podman_follow("cid", 2000)]

    assert asyncio.run(run()) == ["plain line", "red text", ""]


# ─── build→container orchestration ────────────────────────────────────────────


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


def test_stream_app_logs_running_emits_build_then_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_build_stream(monkeypatch, [b"build 1", b"build 2"])
    _fake_podman(monkeypatch, ["container a", "container b"])

    def get_state() -> tuple[str, str | None]:
        return "running", "cid123"

    chunks = asyncio.run(_collect(stream_app_logs("app", "/p/docker.log", get_state)))
    assert chunks == ["build 1", "build 2", "=== Container logs ===", "container a", "container b"]


def test_stream_app_logs_no_container_emits_build_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_build_stream(monkeypatch, [b"only a build log"])
    _fake_podman(monkeypatch, ["should not appear"])

    def get_state() -> tuple[str, str | None]:
        return "stopped", None

    chunks = asyncio.run(_collect(stream_app_logs("app", "/p/docker.log", get_state)))
    assert chunks == ["only a build log"]


def test_stream_app_logs_building_then_container_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_follow_build(build_log_path: str, get_state: Any) -> AsyncIterator[str]:
        for line in ("building...", "more build output"):
            yield line

    monkeypatch.setattr(log_stream, "_follow_build_until_container", fake_follow_build)
    _fake_podman(monkeypatch, ["container up"])

    # building at connect; a container has appeared by the time the build follow returns.
    states = iter([("building", None), ("running", "cid9")])

    def get_state() -> tuple[str, str | None]:
        return next(states)

    chunks = asyncio.run(_collect(stream_app_logs("app", "/p/docker.log", get_state)))
    assert chunks == ["building...", "more build output", "=== Container logs ===", "container up"]


def test_follow_build_stops_when_container_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    # The follow yields build lines and returns as soon as get_state reports a
    # container, without draining the rest of the (still-live) build stream.
    _fake_build_stream(monkeypatch, [b"a", b"b", b"c"])
    states = iter([("building", None), ("running", "cid")])

    def get_state() -> tuple[str, str | None]:
        try:
            return next(states)
        except StopIteration:
            return "running", "cid"

    lines = asyncio.run(_collect(log_stream._follow_build_until_container("/p/docker.log", get_state)))
    assert lines == ["a", "b"]
