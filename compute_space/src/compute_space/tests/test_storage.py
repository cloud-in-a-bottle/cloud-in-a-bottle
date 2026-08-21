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
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: None)

    status = cast(dict[str, Any], storage.storage_status(config))

    assert config.data_root_dir in calls, "storage_status should query data_root_dir"
    assert status["storage_min_free_bytes"] == 1000 * 1024 * 1024
    assert status["storage_low"] is False  # 6 GiB free > 1000 MiB required
    # Podman unavailable → the category degrades to None rather than failing the endpoint.
    assert status["build_cache_bytes"] is None
    assert status["build_cache_reclaimable_bytes"] is None


def test_app_data_total_combines_per_app_and_loose_files(tmp_path, monkeypatch):
    """The app_data total is derived from the per-app walk plus loose root
    files — not a second full walk of the tree."""
    config = _make_test_config(tmp_path)
    app_data = os.path.join(config.persistent_data_dir, "app_data")
    os.makedirs(os.path.join(app_data, "immich"), exist_ok=True)
    with open(os.path.join(app_data, "immich", "photo.bin"), "wb") as f:
        f.write(b"x" * (300 * 1024))
    with open(os.path.join(app_data, "loose.bin"), "wb") as f:
        f.write(b"x" * (200 * 1024))
    monkeypatch.setattr(storage, "container_image_storage_bytes", lambda: None)

    status = cast(dict[str, Any], storage.storage_status(config))

    assert status["per_app"] == {"immich": 300 * 1024}
    assert status["app_data_used_bytes"] == 500 * 1024


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
        f.write(b"x" * 1024)
    with open(os.path.join(docs_dir, "data.bin"), "wb") as f:
        f.write(b"x" * 2048)

    result = storage.per_app_usage(config)
    assert result["notes"] == 1024
    assert result["docs"] == 2048


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


def test_start_storage_guard_noop_without_threshold(tmp_path, monkeypatch):
    config = _make_test_config(tmp_path, storage_min_free_mb=0)
    storage._guard_db_paths.clear()

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            started.append(True)

        def start(self):
            pass

    monkeypatch.setattr(storage.threading, "Thread", FakeThread)

    storage.start_storage_guard(config)
    assert len(started) == 0


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
