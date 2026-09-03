"""Tests for ``check_app_status`` startup recovery.

The key regression: an app left in 'starting' after an interrupted boot-time
restart sweep must still be recovered. Earlier the sweep only looked at
'running' apps, so anything stranded in 'starting' stayed down forever.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import compute_space.core.startup as startup
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.db.connection import init_db

from .conftest import _make_test_config


def _seed_app(
    cfg: Any,
    *,
    name: str,
    status: str,
    port: int,
    container_id: str | None,
    repo_path: str,
    created_at: str | None = None,
) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        if created_at is None:
            db.execute(
                """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id)
                   VALUES (?, ?, '1.0', ?, ?, ?, ?)""",
                (app_id, name, repo_path, port, status, container_id),
            )
        else:
            db.execute(
                """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id, created_at)
                   VALUES (?, ?, '1.0', ?, ?, ?, ?, ?)""",
                (app_id, name, repo_path, port, status, container_id, created_at),
            )
        db.commit()
    finally:
        db.close()
    return app_id


def _status(cfg: Any, app_id: str) -> str:
    db = sqlite3.connect(cfg.db_path)
    try:
        status: str = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
        return status
    finally:
        db.close()


def _seed_error_app(cfg: Any, *, name: str, port: int, repo_path: str, error_message: str) -> str:
    """Seed an app already in 'error' with a specific error_message."""
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, container_id, error_message)
               VALUES (?, ?, '1.0', ?, ?, 'error', NULL, ?)""",
            (app_id, name, repo_path, port, error_message),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _error_message(cfg: Any, app_id: str) -> str | None:
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _stub_drop_cache(monkeypatch: Any) -> list[bool]:
    """Record calls to drop_docker_build_cache without shelling out to podman."""
    calls: list[bool] = []

    def fake_drop() -> str:
        calls.append(True)
        return "deleted: sha256:abc"

    monkeypatch.setattr(startup, "drop_docker_build_cache", fake_drop)
    return calls


def _capture_restart_sweep(monkeypatch: Any) -> tuple[list[str], threading.Event]:
    """Replace the background restart sweep with a recorder."""
    restarted: list[str] = []
    done = threading.Event()

    def fake_sequential(apps: list[tuple[str, bool]], config: Any) -> None:
        restarted.extend(app_id for app_id, _needs_build in apps)
        done.set()

    monkeypatch.setattr(startup, "image_exists", lambda image_tag: True)
    monkeypatch.setattr(startup, "_restart_apps_sequential", fake_sequential)
    return restarted, done


def test_starting_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20200)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="stuck",
        status="starting",
        port=20210,
        container_id="deadbeef",
        repo_path=str(repo),
        created_at="2019-12-31 23:59:59",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for the stranded 'starting' app"
    assert app_id in restarted


def test_building_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20400)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(
        cfg, name="mid-build", status="building", port=20410, container_id="deadbeef", repo_path=str(repo)
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert app_id in restarted


def test_starting_app_with_live_container_is_recreated(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20300)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="live",
        status="starting",
        port=20310,
        container_id="livecontainer",
        repo_path=str(repo),
        created_at="2019-12-31 23:59:59",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert restarted == [app_id]
    assert _status(cfg, app_id) == "starting"


def test_running_app_with_dead_container_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20500)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="crashed", status="running", port=20510, container_id="deadbeef", repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert app_id in restarted


def test_starting_app_with_no_container_from_previous_process_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    # A no-container 'starting' row created *before* this process started is an
    # abandoned build from a killed previous process — its deploy thread is gone,
    # so the sweep must rebuild it.
    cfg = _make_test_config(tmp_path, port=20700)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="nocontainer",
        status="starting",
        port=20710,
        container_id=None,
        repo_path=str(repo),
        created_at="2019-12-31 23:59:59",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for abandoned 'starting' app with no container"
    assert app_id in restarted
    assert _status(cfg, app_id) == "starting"


def test_building_app_with_no_container_from_previous_process_is_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    # Same as above but 'building' — an interrupted build left with no container
    # and a created_at predating this process must be rebuilt.
    cfg = _make_test_config(tmp_path, port=20800)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="nocontainer-build",
        status="building",
        port=20810,
        container_id=None,
        repo_path=str(repo),
        created_at="2019-12-31 23:59:59",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "restart sweep was never scheduled for abandoned 'building' app with no container"
    assert app_id in restarted
    assert _status(cfg, app_id) == "starting"


def test_inflight_build_from_current_process_is_not_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    # The first-boot race guard: deploy_default_apps inserts a 'building' row with
    # no container and spawns a deploy thread, then this same process runs
    # check_app_status while that build is still in flight.  Because the row's
    # created_at is >= this process's start, the sweep must NOT queue a second,
    # concurrent build (which would race podman rm -f, clobber container_id, and
    # regenerate the app token).  It should only reflect the in-flight state by
    # marking the row 'starting'.
    cfg = _make_test_config(tmp_path, port=20900)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="inflight-build",
        status="building",
        port=20910,
        container_id=None,
        repo_path=str(repo),
        created_at="2020-01-01 00:00:01",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set(), "in-flight build from the current process was wrongly queued for restart"
    assert app_id not in restarted
    # Status is advanced to 'starting' so the dashboard shows the transitional
    # state; the owning deploy thread still drives it to 'running'/'error'.
    assert _status(cfg, app_id) == "starting"


def test_inflight_build_at_exact_process_start_is_not_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    # created_at == _PROCESS_START_UTC (1-second resolution collision) must be
    # treated as in-flight, not abandoned — the guard uses >=, so a row stamped
    # in the same second the process started is left for its deploy thread.
    cfg = _make_test_config(tmp_path, port=21000)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="inflight-boundary",
        status="building",
        port=21010,
        container_id=None,
        repo_path=str(repo),
        created_at="2020-01-01 00:00:00",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "starting"


# ---------------------------------------------------------------------------
# Cache-corruption recovery: drop the build cache once and rebuild serially.
# ---------------------------------------------------------------------------


def test_cache_corrupt_app_is_pruned_and_serially_rebuilt(tmp_path: Path, monkeypatch: Any) -> None:
    # The concurrent initial deploy corrupted containers-storage; the boot sweep
    # must drop the build cache and queue the app onto the (serial) rebuild path.
    cfg = _make_test_config(tmp_path, port=21600)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_error_app(
        cfg,
        name="corrupt",
        port=21610,
        repo_path=str(repo),
        error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    drop_calls = _stub_drop_cache(monkeypatch)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5), "cache-corrupt app was never queued for serial rebuild"
    assert drop_calls == [True], "build cache should be dropped exactly once"
    assert app_id in restarted
    # Reset to transitional state so the dashboard reflects the retry.
    assert _status(cfg, app_id) == "starting"
    assert _error_message(cfg, app_id) is None


def test_non_corrupt_error_app_is_left_alone(tmp_path: Path, monkeypatch: Any) -> None:
    # A build that failed for an ordinary reason (bad Dockerfile, etc.) must NOT
    # trigger a cache drop or an automatic rebuild — only the corruption marker does.
    cfg = _make_test_config(tmp_path, port=21700)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_error_app(
        cfg,
        name="badfile",
        port=21710,
        repo_path=str(repo),
        error_message="Container build failed (exit code 1): COPY failed: no such file",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    drop_calls = _stub_drop_cache(monkeypatch)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert drop_calls == []
    assert app_id not in restarted
    assert _status(cfg, app_id) == "error"


def test_recovered_app_is_not_re_pruned_once_marker_cleared(tmp_path: Path, monkeypatch: Any) -> None:
    # No persisted ledger: bounding relies on recovery clearing the marker.
    # After the first sweep sets the app to 'starting' with a null error, a
    # second sweep in the same state must not prune or rebuild it again.
    cfg = _make_test_config(tmp_path, port=21800)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_error_app(
        cfg,
        name="recovered",
        port=21810,
        repo_path=str(repo),
        error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    drop_calls = _stub_drop_cache(monkeypatch)
    _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)  # first sweep: recovers (prunes) the app
    assert drop_calls == [True]
    assert _status(cfg, app_id) == "starting"

    # Second sweep with the app now in 'starting' (marker cleared): the
    # normal sweep handles it (dead container -> rebuild) but the corruption
    # recovery must NOT prune again.
    startup.check_app_status(cfg)
    assert drop_calls == [True], "cache must not be dropped again once the marker is cleared"


def test_multiple_corrupt_apps_prune_once_and_all_rebuild(tmp_path: Path, monkeypatch: Any) -> None:
    # A single global prune clears every corrupt layer, so N corrupt apps must
    # trigger exactly one drop and then all rebuild serially in one sweep.
    cfg = _make_test_config(tmp_path, port=21900)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    ids = {
        _seed_error_app(
            cfg,
            name=f"corrupt{i}",
            port=21910 + i,
            repo_path=str(repo),
            error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
        )
        for i in range(3)
    }

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    drop_calls = _stub_drop_cache(monkeypatch)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert done.wait(5)
    assert drop_calls == [True], "prune must run exactly once for the whole sweep"
    assert ids.issubset(set(restarted))


def test_corrupt_app_with_missing_repo_is_not_recovered(tmp_path: Path, monkeypatch: Any) -> None:
    # No checkout to rebuild from — a prune would be pointless, so skip it.
    cfg = _make_test_config(tmp_path, port=22000)
    init_db(cfg.db_path)
    missing = tmp_path / "gone"  # never created
    app_id = _seed_error_app(
        cfg,
        name="corrupt-norepo",
        port=22010,
        repo_path=str(missing),
        error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    drop_calls = _stub_drop_cache(monkeypatch)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert drop_calls == []
    assert app_id not in restarted
    assert _status(cfg, app_id) == "error"


def test_running_app_with_live_container_is_left_alone(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=20600)
    init_db(cfg.db_path)
    app_id = _seed_app(
        cfg, name="healthy", status="running", port=20610, container_id="livecontainer", repo_path="/nonexistent"
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert restarted == []
    assert _status(cfg, app_id) == "running"


def test_building_app_with_live_container_is_healed_to_running(tmp_path: Path, monkeypatch: Any) -> None:
    # Symmetry with the 'starting' heal: a 'building' row whose container is
    # actually up (a prior sweep started it but the status never advanced) must
    # heal to 'running', not be rebuilt.
    cfg = _make_test_config(tmp_path, port=21300)
    init_db(cfg.db_path)
    app_id = _seed_app(
        cfg, name="live-build", status="building", port=21310, container_id="livecontainer", repo_path="/nonexistent"
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert restarted == []
    assert _status(cfg, app_id) == "running"


def test_dead_container_with_missing_repo_path_is_marked_error(tmp_path: Path, monkeypatch: Any) -> None:
    # A dead container whose repo checkout has vanished cannot be rebuilt, so the
    # sweep must surface the failure as 'error' rather than silently queue a
    # rebuild that would immediately fail.
    cfg = _make_test_config(tmp_path, port=21100)
    init_db(cfg.db_path)
    missing = tmp_path / "gone"  # deliberately never created
    app_id = _seed_app(
        cfg, name="norepo", status="building", port=21110, container_id="deadbeef", repo_path=str(missing)
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "error"


def test_stopped_app_is_left_untouched(tmp_path: Path, monkeypatch: Any) -> None:
    # check_app_status only scans running/starting/building. An app the owner
    # deliberately stopped must not be revived by the boot sweep.
    cfg = _make_test_config(tmp_path, port=21200)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="idle", status="stopped", port=21210, container_id=None, repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "stopped"


def test_removing_app_is_left_untouched(tmp_path: Path, monkeypatch: Any) -> None:
    # 'removing' is a teardown-in-progress state outside the sweep's scan set;
    # reviving it would race the removal thread.
    cfg = _make_test_config(tmp_path, port=21500)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(cfg, name="going", status="removing", port=21510, container_id="deadbeef", repo_path=str(repo))

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "removing"


def test_inflight_starting_from_current_process_is_not_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    # Same guard as the in-flight 'building' case, for a row that reached
    # 'starting' before run_container recorded a container_id. created_at >= this
    # process's start means the owning deploy thread is still live, so the sweep
    # must not queue a competing rebuild.
    cfg = _make_test_config(tmp_path, port=21400)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="inflight-starting",
        status="starting",
        port=21410,
        container_id=None,
        repo_path=str(repo),
        created_at="2020-01-01 00:00:05",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "starting"


def test_inflight_starting_with_live_container_is_not_restarted(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=21420)
    init_db(cfg.db_path)
    repo = tmp_path / "repo-live"
    repo.mkdir()
    monkeypatch.setattr(startup, "_PROCESS_START_UTC", "2020-01-01 00:00:00")
    app_id = _seed_app(
        cfg,
        name="inflight-live",
        status="starting",
        port=21430,
        container_id="live-container",
        repo_path=str(repo),
        created_at="2020-01-01 00:00:05",
    )

    monkeypatch.setattr(startup, "is_container_running", lambda cid: True)
    restarted, done = _capture_restart_sweep(monkeypatch)

    startup.check_app_status(cfg)

    assert not done.is_set()
    assert app_id not in restarted
    assert _status(cfg, app_id) == "starting"


def test_running_app_without_image_falls_back_to_rebuild(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=22100)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    app_id = _seed_app(
        cfg, name="missing-image", status="running", port=22110, container_id="dead", repo_path=str(repo)
    )
    captured: list[tuple[str, bool]] = []
    done = threading.Event()

    monkeypatch.setattr(startup, "is_container_running", lambda cid: False)
    monkeypatch.setattr(startup, "image_exists", lambda image_tag: False)

    def fake_sequential(apps: list[tuple[str, bool]], config: Any) -> None:
        captured.extend(apps)
        done.set()

    monkeypatch.setattr(startup, "_restart_apps_sequential", fake_sequential)
    startup.check_app_status(cfg)

    assert done.wait(5)
    assert captured == [(app_id, True)]


def test_recovery_worker_restarts_existing_images_and_rebuilds_only_fallbacks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = _make_test_config(tmp_path, port=22200)
    init_db(cfg.db_path)
    restarted: list[str] = []
    rebuilt: list[str] = []

    monkeypatch.setattr(startup, "restart_app_process", lambda app_id, db, config: restarted.append(app_id))
    monkeypatch.setattr(startup, "start_app_process", lambda app_id, db, config: rebuilt.append(app_id))

    startup._restart_apps_sequential([("reuse", False), ("fallback", True)], cfg)

    assert restarted == ["reuse"]
    assert rebuilt == ["fallback"]


def test_recovery_worker_persists_restart_failure(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = _make_test_config(tmp_path, port=22300)
    init_db(cfg.db_path)
    repo = tmp_path / "repo-failure"
    repo.mkdir()
    app_id = _seed_app(
        cfg, name="restart-failure", status="starting", port=22310, container_id="old", repo_path=str(repo)
    )

    def fail_restart(app_id: str, db: sqlite3.Connection, config: Any) -> None:
        raise RuntimeError("replacement failed")

    monkeypatch.setattr(startup, "restart_app_process", fail_restart)
    startup._restart_apps_sequential([(app_id, False)], cfg)

    assert _status(cfg, app_id) == "error"
    assert _error_message(cfg, app_id) == "replacement failed"
