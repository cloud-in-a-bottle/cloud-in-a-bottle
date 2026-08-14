"""Tests for the periodic image pruner.

Covers two behaviours:
- The dangling-only prune (``podman image prune`` without ``--all``).
- The orphaned tagged-image sweep: ``openhost-{name}:latest`` images whose app
  is gone from the DB and which are older than the configured age threshold.

Plus thread wiring (start/idempotency/disable) and that the loop swallows
failures so a transient podman/DB error can't kill it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.image_pruner as image_pruner
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import OpenHostImage
from compute_space.core.containers import list_openhost_images
from compute_space.core.containers import parse_openhost_image_app_name
from compute_space.core.containers import prune_dangling_images
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config

# --------------------------------------------------------------------------
# Dangling-only prune
# --------------------------------------------------------------------------


def test_prune_dangling_images_omits_all_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["cmd"] = cmd
        calls["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 5MB\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = prune_dangling_images()

    assert output == "Total reclaimed space: 5MB"
    # Dangling-only: no --all (which would also remove tagged stopped-app images).
    assert calls["cmd"] == ["podman", "image", "prune", "--force"]
    assert "--all" not in calls["cmd"]
    assert calls["timeout"] == 120


def test_prune_dangling_images_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="podman broke")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="podman broke"):
        prune_dangling_images()


# --------------------------------------------------------------------------
# Tag parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("openhost-notes:latest", "notes"),
        ("localhost/openhost-notes:latest", "notes"),
        ("openhost-my-cool-app:latest", "my-cool-app"),  # interior hyphens
        ("localhost/openhost-a1:latest", "a1"),
        # Non-Cloud in a Bottle repos and shapes must not match.
        ("docker.io/library/python:3.12", None),
        ("openhost-notes:v2", None),  # only :latest is a Cloud in a Bottle app tag
        ("notopenhost-notes:latest", None),
        ("python:latest", None),
        ("<none>:<none>", None),
    ],
)
def test_parse_openhost_image_app_name(tag: str, expected: str | None) -> None:
    assert parse_openhost_image_app_name(tag) == expected


# --------------------------------------------------------------------------
# list_openhost_images
# --------------------------------------------------------------------------


def _fake_images_run(rows: list[dict[str, Any]], returncode: int = 0) -> Any:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=json.dumps(rows), stderr="")

    return fake_run


def test_list_openhost_images_filters_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"Id": "id-notes", "Names": ["localhost/openhost-notes:latest"], "Created": 1000},
        {"Id": "id-base", "Names": ["docker.io/library/python:3.12"], "Created": 900},  # not ours
        {"Id": "id-blog", "Names": ["openhost-blog:latest"], "Created": 2000},
        {"Id": "id-dangling", "Names": [], "Created": 500},  # dangling — skipped
    ]
    monkeypatch.setattr(subprocess, "run", _fake_images_run(rows))

    images = list_openhost_images()

    assert {(i.app_name, i.image_id, i.created_epoch) for i in images} == {
        ("notes", "id-notes", 1000),
        ("blog", "id-blog", 2000),
    }


def test_list_openhost_images_skips_rows_without_int_created(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"Id": "id-x", "Names": ["openhost-x:latest"], "Created": "2024-01-01"},  # str, not int
        {"Id": "id-y", "Names": ["openhost-y:latest"]},  # no Created
        {"Id": "id-z", "Names": ["openhost-z:latest"], "Created": 1234},
    ]
    monkeypatch.setattr(subprocess, "run", _fake_images_run(rows))

    images = list_openhost_images()
    assert [(i.app_name, i.created_epoch) for i in images] == [("z", 1234)]


def test_list_openhost_images_falls_back_to_repotags(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"Id": "id-r", "RepoTags": ["openhost-repo:latest"], "Created": 42}]
    monkeypatch.setattr(subprocess, "run", _fake_images_run(rows))
    images = list_openhost_images()
    assert [(i.app_name, i.image_id) for i in images] == [("repo", "id-r")]


def test_list_openhost_images_empty_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_images_run([], returncode=1))
    assert list_openhost_images() == []


def test_list_openhost_images_empty_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert list_openhost_images() == []


# --------------------------------------------------------------------------
# Orphaned-image sweep
# --------------------------------------------------------------------------


def _insert_app(db_path: str, name: str, status: str = "running") -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, container_id, status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_app_id(), name, "1", f"/tmp/{name}", _free_port(db), None, status, None),
        )
        db.commit()
    finally:
        db.close()


_PORT_COUNTER = [9100]


def _free_port(_db: sqlite3.Connection) -> int:
    _PORT_COUNTER[0] += 1
    return _PORT_COUNTER[0]


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, image_orphan_max_age_seconds=7 * 24 * 3600)
    init_db(cfg.db_path)
    return cfg


def test_sweep_removes_orphan_older_than_threshold(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert_app(cfg.db_path, "liveapp")
    now = 1_000_000.0
    old = int(now - 8 * 24 * 3600)  # 8 days old — past the 7-day threshold
    images = [
        OpenHostImage(image_id="id-live", app_name="liveapp", created_epoch=old),  # kept: app exists
        OpenHostImage(image_id="id-orphan", app_name="goneapp", created_epoch=old),  # removed
    ]
    removed: list[str] = []
    monkeypatch.setattr(image_pruner, "list_openhost_images", lambda: images)
    monkeypatch.setattr(image_pruner, "remove_image_by_id", lambda i: removed.append(i) or True)

    result = image_pruner.sweep_orphaned_images(cfg, now)

    assert result == ["id-orphan"]
    assert removed == ["id-orphan"]


def test_sweep_keeps_orphan_younger_than_threshold(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    recent = int(now - 1 * 24 * 3600)  # 1 day old — within the age guard
    images = [OpenHostImage(image_id="id-recent-orphan", app_name="goneapp", created_epoch=recent)]
    removed: list[str] = []
    monkeypatch.setattr(image_pruner, "list_openhost_images", lambda: images)
    monkeypatch.setattr(image_pruner, "remove_image_by_id", lambda i: removed.append(i) or True)

    # No app in DB, but the image is too new: must NOT be removed (protects
    # mid-deploy images whose DB row hasn't committed yet).
    result = image_pruner.sweep_orphaned_images(cfg, now)

    assert result == []
    assert removed == []


def test_sweep_keeps_images_for_stopped_and_errored_apps(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert_app(cfg.db_path, "stoppedapp", status="stopped")
    _insert_app(cfg.db_path, "erroredapp", status="error")
    now = 1_000_000.0
    old = int(now - 30 * 24 * 3600)  # very old, but apps still exist
    images = [
        OpenHostImage(image_id="id-stopped", app_name="stoppedapp", created_epoch=old),
        OpenHostImage(image_id="id-errored", app_name="erroredapp", created_epoch=old),
    ]
    removed: list[str] = []
    monkeypatch.setattr(image_pruner, "list_openhost_images", lambda: images)
    monkeypatch.setattr(image_pruner, "remove_image_by_id", lambda i: removed.append(i) or True)

    result = image_pruner.sweep_orphaned_images(cfg, now)

    # Images for apps in ANY status are kept, however old.
    assert result == []
    assert removed == []


def test_sweep_disabled_when_max_age_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, image_orphan_max_age_seconds=0)
    init_db(cfg.db_path)
    called: list[bool] = []
    monkeypatch.setattr(image_pruner, "list_openhost_images", lambda: called.append(True) or [])

    result = image_pruner.sweep_orphaned_images(cfg, 1_000_000.0)

    assert result == []
    # Short-circuits before even listing images.
    assert called == []


def test_sweep_survives_db_error(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_config: Any) -> set[str]:
        raise sqlite3.OperationalError("db locked")

    monkeypatch.setattr(image_pruner, "_current_app_names", boom)
    # Should not raise, and should remove nothing when it can't read the DB.
    assert image_pruner.sweep_orphaned_images(cfg, 1_000_000.0) == []


def test_sweep_continues_after_a_failed_removal(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    old = int(now - 10 * 24 * 3600)
    images = [
        OpenHostImage(image_id="id-fail", app_name="gone1", created_epoch=old),
        OpenHostImage(image_id="id-ok", app_name="gone2", created_epoch=old),
    ]
    monkeypatch.setattr(image_pruner, "list_openhost_images", lambda: images)
    # First removal fails, second succeeds.
    monkeypatch.setattr(image_pruner, "remove_image_by_id", lambda i: i == "id-ok")

    result = image_pruner.sweep_orphaned_images(cfg, now)

    # Only the successful removal is reported; the failed one didn't abort the sweep.
    assert result == ["id-ok"]


# --------------------------------------------------------------------------
# Loop / thread wiring
# --------------------------------------------------------------------------


def test_run_prune_once_swallows_prune_and_sweep_errors(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_prune() -> str:
        raise RuntimeError("podman broke")

    def boom_sweep(_config: Any, _now: float) -> list[str]:
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(image_pruner, "prune_dangling_images", boom_prune)
    monkeypatch.setattr(image_pruner, "sweep_orphaned_images", boom_sweep)
    # Must not raise — the loop relies on this so one bad cycle can't kill it.
    image_pruner._run_prune_once(cfg)


def test_run_prune_once_runs_both_prune_and_sweep(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(image_pruner, "prune_dangling_images", lambda: order.append("prune") or "")
    monkeypatch.setattr(image_pruner, "sweep_orphaned_images", lambda c, n: order.append("sweep") or [])

    image_pruner._run_prune_once(cfg)

    assert order == ["prune", "sweep"]


def test_start_image_pruner_noop_when_disabled(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, image_prune_interval_seconds=0)
    image_pruner._pruner_db_paths.clear()

    started: list[bool] = []

    class FakeThread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            started.append(True)

        def start(self) -> None:
            pass

    monkeypatch.setattr(image_pruner.threading, "Thread", FakeThread)

    image_pruner.start_image_pruner(config)
    assert started == []


def test_start_image_pruner_starts_and_is_idempotent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, image_prune_interval_seconds=3600)
    image_pruner._pruner_db_paths.clear()

    started: list[Any] = []

    class FakeThread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            started.append(self)

    monkeypatch.setattr(image_pruner.threading, "Thread", FakeThread)

    image_pruner.start_image_pruner(config)
    image_pruner.start_image_pruner(config)  # idempotent per db_path

    assert len(started) == 1
    assert started[0].daemon is True
    # Thread receives (config, interval).
    assert started[0].args == (config, 3600)


def test_loop_sleeps_before_first_prune(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop must sleep the interval before its first prune (so a mid-build
    # deploy's intermediate layers aren't pruned during the startup rush), then
    # prune each cycle.  Stop it after two iterations via a sleep that raises.
    sleeps: list[int] = []
    prunes: list[Any] = []

    class _Stop(Exception):
        pass

    def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    monkeypatch.setattr(image_pruner.time, "sleep", fake_sleep)
    monkeypatch.setattr(image_pruner, "_run_prune_once", lambda c: prunes.append(c))

    with pytest.raises(_Stop):
        image_pruner._image_pruner_loop(cfg, 1800)

    # Slept before the first prune, and pruned once between the two sleeps.
    assert sleeps == [1800, 1800]
    assert prunes == [cfg]
