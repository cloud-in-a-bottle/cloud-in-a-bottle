from __future__ import annotations

import os
import sqlite3
from collections import namedtuple
from typing import Any
from typing import cast

import pytest

import compute_space.core.storage as storage
from compute_space.config import DefaultConfig
from compute_space.core.app_id import new_app_id
from compute_space.tests.conftest import _make_test_config


def test_storage_status_includes_disk_totals(tmp_path, monkeypatch):
    # Guard off here so storage_min_free_bytes is None (this test asserts that).
    config = _make_test_config(tmp_path, storage_min_free_mb=0)
    usage = namedtuple("usage", ["total", "used", "free"])

    vm_data = os.path.join(config.persistent_data_dir, "vm_data")
    app_data = os.path.join(config.persistent_data_dir, "app_data")
    os.makedirs(vm_data, exist_ok=True)
    os.makedirs(app_data, exist_ok=True)
    with open(os.path.join(vm_data, "router.db"), "wb") as f:
        f.write(b"x" * (256 * 1024))
    with open(os.path.join(app_data, "blob.bin"), "wb") as f:
        f.write(b"x" * (512 * 1024))

    calls = []

    def fake_disk_usage(path):
        calls.append(path)
        return usage(total=10 * 1024**3, used=4 * 1024**3, free=6 * 1024**3)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: (7 * 1024**3, 3 * 1024**3))
    monkeypatch.setattr(storage, "app_container_writable_layer_bytes", lambda: {})

    status = cast(dict[str, Any], storage.storage_status(config))

    assert config.data_root_dir in calls, "storage_status should query data_root_dir"
    assert status["disk"]["total_bytes"] == 10 * 1024**3
    assert status["disk"]["used_bytes"] == 4 * 1024**3
    assert status["disk"]["free_bytes"] == 6 * 1024**3
    assert "persistent" not in status
    assert "temporary" not in status
    assert status["openhost_data_used_bytes"] > 0
    assert status["app_data_used_bytes"] > 0
    assert status["build_cache_bytes"] == 7 * 1024**3
    assert status["build_cache_reclaimable_bytes"] == 3 * 1024**3
    assert status["storage_min_free_bytes"] is None
    assert status["storage_low"] is False
    assert status["guard_paused"] is False


def test_storage_status_with_min_free(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, storage_min_free_mb=1000)
    usage = namedtuple("usage", ["total", "used", "free"])

    calls = []

    def fake_disk_usage(path):
        calls.append(path)
        return usage(total=10 * 1024**3, used=4 * 1024**3, free=6 * 1024**3)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: (None, None))
    monkeypatch.setattr(storage, "app_container_writable_layer_bytes", lambda: {})

    status = cast(dict[str, Any], storage.storage_status(config))

    assert config.data_root_dir in calls, "storage_status should query data_root_dir"
    assert status["storage_min_free_bytes"] == 1000 * 1024 * 1024
    assert status["storage_low"] is False  # 6 GiB free > 1000 MiB required
    # Podman unavailable → the category degrades to None rather than failing the endpoint.
    assert status["build_cache_bytes"] is None
    assert status["build_cache_reclaimable_bytes"] is None


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


def test_app_data_total_combines_per_app_and_loose_files(tmp_path, monkeypatch):
    """The app_data total is derived from the per-app walk plus loose root
    files, not a second full walk of the tree."""
    config = _make_test_config(tmp_path)
    app_data = os.path.join(config.persistent_data_dir, "app_data")
    _write(os.path.join(app_data, "immich", "photo.bin"), 300 * 1024)
    _write(os.path.join(app_data, "loose.bin"), 200 * 1024)
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: (None, None))
    monkeypatch.setattr(storage, "app_container_writable_layer_bytes", lambda: {})

    status = cast(dict[str, Any], storage.storage_status(config))

    assert status["per_app"] == {"immich": 300 * 1024}
    assert status["app_data_used_bytes"] == 500 * 1024


def test_per_app_includes_temp_data_and_container_layer(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)
    _write(os.path.join(config.persistent_data_dir, "app_data", "notes", "db.bin"), 8192)
    _write(os.path.join(config.temporary_data_dir, "app_temp_data", "notes", "scratch.bin"), 4096)
    _write(os.path.join(config.temporary_data_dir, "app_temp_data", "tempy", "scratch.bin"), 4096)
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: (None, None))
    monkeypatch.setattr(storage, "app_container_writable_layer_bytes", lambda: {"notes": 1000, "layer-only": 2000})

    status = cast(dict[str, Any], storage.storage_status(config))

    assert status["per_app"] == {"notes": 8192 + 4096 + 1000, "tempy": 4096, "layer-only": 2000}


def test_openhost_data_includes_db_dir_and_vm_data(tmp_path):
    config = _make_test_config(tmp_path)
    before = storage.openhost_data_usage_bytes(config)
    _write(os.path.join(config.openhost_data_path, "extra.bin"), 8192)
    _write(os.path.join(config.persistent_data_dir, "vm_data", "keys", "k.pem"), 4096)
    assert storage.openhost_data_usage_bytes(config) == before + 8192 + 4096


def test_dir_size_counts_allocated_blocks_not_apparent_size(tmp_path):
    with open(tmp_path / "sparse.bin", "wb") as f:
        f.truncate(64 * 1024**2)
    assert storage._dir_size_bytes(str(tmp_path)) < 1024**2


def test_dir_size_counts_hardlinks_once(tmp_path):
    _write(str(tmp_path / "a" / "file.bin"), 8192)
    os.makedirs(tmp_path / "b")
    os.link(tmp_path / "a" / "file.bin", tmp_path / "b" / "file.bin")
    assert storage._dir_size_bytes(str(tmp_path)) == 8192


def test_swap_file_bytes_counts_files_on_data_disk(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)
    swapfile = tmp_path / "swap file"
    swapfile.write_bytes(b"")
    proc_swaps = tmp_path / "proc_swaps"
    proc_swaps.write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        f"{str(swapfile).replace(' ', chr(92) + '040')}\tfile\t\t16777212\t\t8598400\t\t-2\n"
        "/dev/sda2\tpartition\t1000\t\t0\t\t-3\n"
        "/nonexistent/swapfile\tfile\t\t1000\t\t0\t\t-4\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **k: real_open(proc_swaps if path == "/proc/swaps" else path, *a, **k),
    )
    assert storage.swap_file_bytes(config) == 16777212 * 1024


def test_swap_file_bytes_zero_without_proc_swaps(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)

    def _raise(*a, **k):
        raise FileNotFoundError("/proc/swaps")

    monkeypatch.setattr("builtins.open", _raise)
    assert storage.swap_file_bytes(config) == 0


def test_disk_free_bytes_uses_data_root_dir(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)
    usage = namedtuple("usage", ["total", "used", "free"])

    calls = []

    def fake_disk_usage(path):
        calls.append(path)
        return usage(total=10 * 1024**3, used=4 * 1024**3, free=6 * 1024**3)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_disk_usage)

    result = storage.disk_free_bytes(config)

    assert result == 6 * 1024**3
    assert config.data_root_dir in calls, "disk_free_bytes should query data_root_dir"


def test_check_before_deploy_noop_without_min_free(tmp_path):
    config = _make_test_config(tmp_path)
    # Should not raise when no min-free is configured
    storage.check_before_deploy(config)


def test_check_before_deploy_raises_when_low(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, storage_min_free_mb=10000)
    usage = namedtuple("usage", ["total", "used", "free"])

    def fake_disk_usage(_path):
        # Only 100 MiB free, but 10000 MiB required
        return usage(total=10 * 1024**3, used=10 * 1024**3 - 100 * 1024**2, free=100 * 1024**2)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_disk_usage)
    with pytest.raises(RuntimeError, match="Storage too low"):
        storage.check_before_deploy(config)


def test_storage_low_false_without_threshold(tmp_path):
    config = _make_test_config(tmp_path)
    assert storage.storage_low(config) is False


def test_storage_low_true_when_below_threshold(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, storage_min_free_mb=10000)
    usage = namedtuple("usage", ["total", "used", "free"])

    def fake_disk_usage(_path):
        return usage(total=10 * 1024**3, used=10 * 1024**3 - 100 * 1024**2, free=100 * 1024**2)

    monkeypatch.setattr(storage.shutil, "disk_usage", fake_disk_usage)
    assert storage.storage_low(config) is True


def test_per_app_usage(tmp_path):
    config = _make_test_config(tmp_path)

    notes_dir = os.path.join(config.persistent_data_dir, "app_data", "notes")
    docs_dir = os.path.join(config.persistent_data_dir, "app_data", "docs")
    os.makedirs(notes_dir, exist_ok=True)
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(notes_dir, "data.bin"), "wb") as f:
        f.write(b"x" * 4096)
    with open(os.path.join(docs_dir, "data.bin"), "wb") as f:
        f.write(b"x" * 8192)

    result = storage.per_app_usage(config)
    assert result["notes"] == 4096
    assert result["docs"] == 8192


def test_storage_status_serves_cached_usage_with_live_disk_free(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)
    usage = namedtuple("usage", ["total", "used", "free"])
    measured = []

    def fake_measure(_config):
        measured.append(True)
        return {"per_app": {"notes": len(measured)}}

    free = [6 * 1024**3]
    monkeypatch.setattr(storage, "_measure_usage", fake_measure)
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _p: usage(total=10 * 1024**3, used=4 * 1024**3, free=free[0])
    )

    first = cast(dict[str, Any], storage.storage_status(config))
    free[0] = 5 * 1024**3
    second = cast(dict[str, Any], storage.storage_status(config))

    assert len(measured) == 1
    assert second["per_app"] == {"notes": 1}
    assert second["usage_measured_at"] == first["usage_measured_at"]
    assert second["disk"]["free_bytes"] == 5 * 1024**3

    refreshed = cast(dict[str, Any], storage.storage_status(config, refresh=True))
    assert len(measured) == 2
    assert refreshed["per_app"] == {"notes": 2}
    assert storage.storage_status(config)["per_app"] == {"notes": 2}


def test_guard_loop_refreshes_snapshot_then_enforces(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path)
    calls = []

    class _Stop(Exception):
        pass

    monkeypatch.setattr(storage, "refresh_usage_snapshot", lambda _c: calls.append("refresh"))
    monkeypatch.setattr(storage, "enforce_storage_guard", lambda _c: calls.append("enforce"))

    def fake_sleep(seconds):
        calls.append(seconds)
        raise _Stop

    monkeypatch.setattr(storage.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        storage._storage_guard_loop(config)
    assert calls == ["refresh", "enforce", 300]


def test_format_bytes():
    assert storage.format_bytes(0) == "0 B"
    assert storage.format_bytes(512) == "512 B"
    assert storage.format_bytes(1024) == "1.0 KiB"
    assert storage.format_bytes(1024**2) == "1.0 MiB"
    assert storage.format_bytes(1024**3) == "1.0 GiB"
    assert storage.format_bytes(1024**4) == "1.0 TiB"
    assert storage.format_bytes(2 * 1024**4) == "2.0 TiB"


# ---------------------------------------------------------------------------
# Storage guard tests
# ---------------------------------------------------------------------------


def _init_apps_db(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """
            CREATE TABLE apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                local_port INTEGER NOT NULL UNIQUE,
                container_id TEXT,
                status TEXT NOT NULL,
                error_message TEXT
            )
            """
        )
        db.commit()
    finally:
        db.close()


def test_enforce_guard_noop_without_threshold(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, seed_primary=False)
    _init_apps_db(config.db_path)
    # Should not do anything when no threshold is configured
    storage.enforce_storage_guard(config)


def test_enforce_guard_stops_apps_when_low(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, seed_primary=False, storage_min_free_mb=1000)
    _init_apps_db(config.db_path)

    db = sqlite3.connect(config.db_path)
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, container_id, status, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_app_id(), "notes", "1", "/tmp/notes", 9100, "cid-1", "running", None),
    )
    db.commit()
    db.close()

    stopped = []
    monkeypatch.setattr(storage, "storage_low", lambda _c: True)
    monkeypatch.setattr(storage, "disk_free_bytes", lambda _c: 50 * 1024 * 1024)
    monkeypatch.setattr(storage, "_stop_app_process_safe", lambda row: stopped.append(row["name"]))

    storage.enforce_storage_guard(config)

    db = sqlite3.connect(config.db_path)
    row = db.execute("SELECT status, error_message, container_id FROM apps WHERE name = 'notes'").fetchone()
    db.close()

    assert stopped == ["notes"]
    assert row[0] == "error"
    assert "too low" in row[1]
    assert row[2] is None


def test_enforce_guard_skips_when_paused(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, seed_primary=False, storage_min_free_mb=1000)
    _init_apps_db(config.db_path)

    db = sqlite3.connect(config.db_path)
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, container_id, status, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_app_id(), "notes", "1", "/tmp/notes", 9100, "cid-1", "running", None),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(storage, "storage_low", lambda _c: True)
    monkeypatch.setattr(storage, "disk_free_bytes", lambda _c: 50 * 1024 * 1024)

    storage.set_guard_paused(True)
    try:
        storage.enforce_storage_guard(config)
    finally:
        storage.set_guard_paused(False)

    db = sqlite3.connect(config.db_path)
    row = db.execute("SELECT status FROM apps WHERE name = 'notes'").fetchone()
    db.close()

    assert row[0] == "running"


def test_start_storage_guard_runs_without_threshold(tmp_path, monkeypatch):
    # The thread also refreshes the usage snapshot, so it runs even with enforcement off.
    config = _make_test_config(tmp_path, storage_min_free_mb=0)
    storage._guard_db_paths.clear()

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(storage.threading, "Thread", FakeThread)

    storage.start_storage_guard(config)
    assert len(started) == 1


def test_start_storage_guard_starts_with_threshold(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, storage_min_free_mb=1000)
    storage._guard_db_paths.clear()

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.daemon = daemon

        def start(self):
            started.append(True)

    monkeypatch.setattr(storage.threading, "Thread", FakeThread)

    storage.start_storage_guard(config)
    storage.start_storage_guard(config)  # idempotent

    assert len(started) == 1


def test_guard_enabled_by_default() -> None:
    # The storage guard ships enabled with a modest threshold (per the decision on
    # PR #236: keep it config-only but on by default). A fresh DefaultConfig must
    # therefore carry a positive storage_min_free_mb so the guard actually runs.
    cfg = DefaultConfig()
    assert cfg.storage_min_free_mb > 0
    # And the guard treats that as an active threshold.
    assert storage.storage_min_free_bytes(cfg) == cfg.storage_min_free_mb * 1024 * 1024


def test_guard_disabled_when_config_zero(tmp_path: Any) -> None:
    # Operators can still turn the guard off via the config (0 = no enforcement).
    cfg = _make_test_config(tmp_path, storage_min_free_mb=0)
    assert storage.storage_min_free_bytes(cfg) is None
