"""Unit tests for compute_space.core.log_stream.

The container-log path shells out to ``podman logs --follow`` (via the shared
process-stream helper); here that is faked so the tests run without a live podman.
The file-tailing helpers and the build→container flow are exercised directly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from compute_space.core import log_stream
from compute_space.core.log_stream import _read_build_tail
from compute_space.core.log_stream import _read_from
from compute_space.core.log_stream import stream_app_logs


async def _collect(gen: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in gen]


def test_read_build_tail_returns_whole_small_file(tmp_path: Path) -> None:
    p = tmp_path / "docker.log"
    p.write_text("line1\nline2\n")
    text, offset = _read_build_tail(str(p), max_bytes=1024)
    assert text == "line1\nline2\n"
    assert offset == p.stat().st_size


def test_read_build_tail_drops_partial_leading_line_when_truncated(tmp_path: Path) -> None:
    p = tmp_path / "docker.log"
    p.write_text("aaaa\nbbbb\ncccc\n")
    # max_bytes lands mid-first-line, so the partial "aaaa" line is dropped.
    text, offset = _read_build_tail(str(p), max_bytes=11)
    assert text == "bbbb\ncccc\n"
    assert offset == p.stat().st_size


def test_read_build_tail_missing_file_is_quiet() -> None:
    # Build hasn't written yet: expected, so empty rather than an error.
    text, offset = _read_build_tail("/no/such/file.log", max_bytes=1024)
    assert text == ""
    assert offset == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission checks")
def test_read_build_tail_permission_denied_fails_loud(tmp_path: Path) -> None:
    # A weird error (not "hasn't started yet") must not be papered over as empty.
    p = tmp_path / "docker.log"
    p.write_text("secret\n")
    p.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            _read_build_tail(str(p), max_bytes=1024)
    finally:
        p.chmod(0o644)


def test_read_from_returns_only_appended_bytes(tmp_path: Path) -> None:
    p = tmp_path / "docker.log"
    p.write_text("hello\n")
    text, offset = _read_from(str(p), 0)
    assert text == "hello\n"
    assert offset == 6
    # nothing new since last offset
    text2, offset2 = _read_from(str(p), offset)
    assert text2 == ""
    assert offset2 == 6
    # append and read only the delta
    with open(p, "a") as f:
        f.write("world\n")
    text3, offset3 = _read_from(str(p), offset2)
    assert text3 == "world\n"
    assert offset3 == 12


def test_read_from_resyncs_when_file_shrinks(tmp_path: Path) -> None:
    """A rotated/truncated file (size < offset) resyncs from the start rather
    than seeking past EOF and reading misaligned bytes."""
    p = tmp_path / "docker.log"
    p.write_text("aaaaaaaaaa\n")
    # Pretend we had already consumed more than the file now holds.
    p.write_text("new\n")
    text, offset = _read_from(str(p), 50)
    assert text == "new\n"
    assert offset == p.stat().st_size


def test_read_from_missing_file_is_quiet() -> None:
    text, offset = _read_from("/no/such/file.log", 10)
    assert text == ""
    assert offset == 10


@pytest.fixture
def fake_podman(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace _podman_follow with a generator yielding canned container lines."""

    def _install(lines: list[str]) -> dict[str, object]:
        seen: dict[str, object] = {}

        async def fake_follow(container_id: str, tail_lines: int) -> AsyncIterator[str]:
            seen["container_id"] = container_id
            seen["tail_lines"] = tail_lines
            for line in lines:
                yield line

        monkeypatch.setattr(log_stream, "_podman_follow", fake_follow)
        return seen

    return _install


def test_stream_running_app_emits_build_then_container(tmp_path: Path, fake_podman: Any) -> None:
    build = tmp_path / "docker.log"
    build.write_text("build step 1\nbuild step 2\n")
    seen = fake_podman(["container line a", "container line b"])

    def get_state() -> tuple[str, str | None]:
        return "running", "cid123"

    chunks = asyncio.run(_collect(stream_app_logs("myapp", str(build), get_state)))

    assert chunks == [
        "build step 1\nbuild step 2",
        "=== Container logs ===",
        "container line a",
        "container line b",
    ]
    assert seen["container_id"] == "cid123"


def test_stream_no_container_emits_build_only(tmp_path: Path, fake_podman: Any) -> None:
    build = tmp_path / "docker.log"
    build.write_text("just a build log\n")
    fake_podman(["should not appear"])

    def get_state() -> tuple[str, str | None]:
        return "stopped", None

    chunks = asyncio.run(_collect(stream_app_logs("myapp", str(build), get_state)))
    assert chunks == ["just a build log"]


def test_stream_building_then_container_appears(
    tmp_path: Path, fake_podman: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While building we follow docker.log; once a container id appears we flush
    the remaining build output and switch to the container feed."""
    monkeypatch.setattr(log_stream, "_BUILD_POLL_SECONDS", 0)
    build = tmp_path / "docker.log"
    build.write_text("building...\n")
    fake_podman(["container up"])

    states = [("building", None), ("building", None), ("running", "cid9")]

    def get_state() -> tuple[str, str | None]:
        # Simulate more build output arriving between polls.
        with open(build, "a") as f:
            f.write("more build output\n")
        return states.pop(0)

    chunks = asyncio.run(_collect(stream_app_logs("myapp", str(build), get_state)))

    assert "=== Container logs ===" in chunks
    assert chunks[-1] == "container up"
    # all build output is delivered before the container separator
    sep = chunks.index("=== Container logs ===")
    joined_build = "\n".join(chunks[:sep])
    assert "building..." in joined_build
    assert "more build output" in joined_build
    # and the container feed contains nothing but the separator + follow lines
    assert chunks[sep:] == ["=== Container logs ===", "container up"]


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

    async def fake_stream(argv: list[str], *, merge_stderr: bool) -> AsyncIterator[bytes]:
        assert "--follow" in argv and "--timestamps" in argv
        assert merge_stderr is True
        for line in raw:
            yield line

    monkeypatch.setattr(log_stream, "stream_process_lines", fake_stream)

    async def run() -> list[str]:
        return [line async for line in log_stream._podman_follow("cid", 2000)]

    lines = asyncio.run(run())
    assert lines == ["plain line", "red text", ""]
