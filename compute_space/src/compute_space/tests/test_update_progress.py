from __future__ import annotations

import json
from pathlib import Path

import pytest

from compute_space.core import update_progress
from openhost_system_agent.updater import paths as agent_paths


@pytest.fixture
def progress_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(agent_paths._DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_log(lines: list[dict[str, object]]) -> None:
    with open(agent_paths.progress_log_path(), "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")


def test_read_progress_empty_when_missing(progress_dir: Path) -> None:
    view = update_progress.read_progress()
    assert view.entries == []
    assert view.terminal is False


def test_read_progress_returns_entries(progress_dir: Path) -> None:
    _write_log(
        [
            {"ts": "t1", "phase": "fetch", "message": "Fetching"},
            {"ts": "t2", "phase": "migrate", "message": "Migrating"},
        ]
    )
    view = update_progress.read_progress()
    assert [e["phase"] for e in view.entries] == ["fetch", "migrate"]
    assert view.terminal is False


def test_read_progress_terminal_on_done(progress_dir: Path) -> None:
    _write_log([{"ts": "t1", "phase": "fetch", "message": "F"}, {"ts": "t2", "phase": "done", "message": "D"}])
    assert update_progress.read_progress().terminal is True


def test_read_progress_terminal_on_failed(progress_dir: Path) -> None:
    _write_log([{"ts": "t1", "phase": "failed", "message": "boom"}])
    assert update_progress.read_progress().terminal is True


def test_read_progress_skips_partial_final_line(progress_dir: Path) -> None:
    with open(agent_paths.progress_log_path(), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "t1", "phase": "fetch", "message": "F"}) + "\n")
        f.write('{"phase": "migrate"')  # half-written, no newline
    view = update_progress.read_progress()
    assert len(view.entries) == 1
    assert view.entries[0]["phase"] == "fetch"
