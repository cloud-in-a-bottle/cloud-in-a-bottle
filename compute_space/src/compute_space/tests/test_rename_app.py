"""Tests for the ``/rename_app/<app_id>`` endpoint, focused on the three-tier directory rename (``app_data``, ``app_temp_data``, ``app_archive``) and the partial-failure rollback that keeps on-disk and DB state consistent when one tier fails (e.g. a transient JuiceFS mount drop)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from litestar import Litestar
from litestar.testing import TestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes


@pytest.fixture
def cfg_factory(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Returns a factory because some tests want to vary the port across runs."""

    def _make(port: int) -> Any:
        cfg = _make_test_config(tmp_path_factory.mktemp(f"rename-{port}"), port=port)
        init_db(cfg.db_path)
        return cfg

    return _make


def _client(cfg: Any) -> TestClient[Litestar]:
    return TestClient(app=make_test_app(api_apps_routes))


def _post_rename(
    cfg: Any, app_id: str, new_name: str, *, mock_archive_healthy: bool = True
) -> tuple[int, dict | None]:
    """Drive the rename_app route via a Litestar TestClient.

    Mocks the archive-health gate (these tests exercise the directory rename
    itself) unless ``mock_archive_healthy`` is False — in which case the
    real gate runs and disabled backends return is_archive_dir_healthy=False.
    """
    cookies = auth_cookie(cfg)
    patches: list[Any] = [mock.patch.object(apps_routes, "stop_app_process")]
    if mock_archive_healthy:
        patches.append(mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=True))
    with _client(cfg) as client, _stack(patches):
        resp = client.post(f"/rename_app/{app_id}", json={"name": new_name}, cookies=cookies)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return resp.status_code, payload


class _stack:
    """Tiny ExitStack-equivalent so the with-statement reads cleanly."""

    def __init__(self, ctxs: list[Any]) -> None:
        self._ctxs = ctxs

    def __enter__(self) -> None:
        self._entered = [c.__enter__() for c in self._ctxs]

    def __exit__(self, *exc: Any) -> None:
        for c in reversed(self._ctxs):
            c.__exit__(*exc)


def _seed_app_row(db_path: str, name: str, port: int = 19500, status: str = "stopped") -> str:
    """Insert a minimal apps row.  The archive-health gate that would
    normally 503 on disabled-backend zones is mocked in ``_post_rename``.
    Returns the freshly minted app_id."""
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, '1.0', ?, ?, ?)""",
            (app_id, name, f"/tmp/repo/{name}", port, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _archive_parent(cfg: Any) -> Path:
    """The EFFECTIVE archive parent for the zone's current backend.

    Fresh test DBs default to backend='local', so this is the local archive
    dir; a test that flips the row to 's3' gets the JuiceFS mountpoint.
    """
    db = sqlite3.connect(cfg.db_path)
    try:
        return Path(apps_routes.archive_backend.effective_archive_dir(cfg, db))
    finally:
        db.close()


def _tier_parents(cfg: Any) -> dict[str, Path]:
    """Single source of truth mapping tier name -> host-side parent dir."""
    return {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
        "app_archive": _archive_parent(cfg),
    }


def _make_per_app_dirs(cfg: Any, app_name: str, tiers: list[str]) -> dict[str, Path]:
    """Pre-create per-app subdirs under each named tier with a sentinel file so we can verify the rename actually moved content rather than creating an empty new dir."""
    parents = _tier_parents(cfg)
    out: dict[str, Path] = {}
    for tier in tiers:
        d = parents[tier] / app_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sentinel.txt").write_text(tier)
        out[tier] = d
    return out


def test_rename_renames_all_three_tiers(cfg_factory: Any) -> None:
    """A rename must move app_data, app_temp_data, AND app_archive subdirectories; forgetting the archive tier would orphan its contents under the old name."""
    cfg = cfg_factory(20200)
    app_id = _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    status, payload = _post_rename(cfg, app_id, "new-name")
    assert status == 200, payload

    parents = _tier_parents(cfg)
    for tier, parent in parents.items():
        assert not (parent / "old-name").exists(), tier
        assert (parent / "new-name" / "sentinel.txt").read_text() == tier


def test_rename_skips_missing_tier_without_error(cfg_factory: Any) -> None:
    """An app that never opted into app_archive has no subdir under the archive tier; rename_app must skip it cleanly rather than fail on a missing source."""
    cfg = cfg_factory(20201)
    app_id = _seed_app_row(cfg.db_path, "old-name")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data"])

    status, payload = _post_rename(cfg, app_id, "new-name")
    assert status == 200, payload
    assert not (_archive_parent(cfg) / "new-name").exists()


def test_rename_rollback_on_archive_failure(cfg_factory: Any) -> None:
    """If the archive-tier rename fails partway through (e.g. JuiceFS mount transiently unhealthy), the previously-renamed app_data and app_temp_data dirs must be rolled back so on-disk state matches the unchanged DB rows."""
    cfg = cfg_factory(20202)
    app_id = _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(str(_archive_parent(cfg)))

    def flaky_rename(src: str, dst: str) -> None:
        if os.path.realpath(os.path.dirname(src)) == archive_root:
            raise OSError(28, "simulated transient mount failure")
        real_rename(src, dst)

    cookies = auth_cookie(cfg)
    with (
        mock.patch("os.rename", side_effect=flaky_rename),
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=True),
        _client(cfg) as client,
    ):
        resp = client.post(f"/rename_app/{app_id}", json={"name": "new-name"}, cookies=cookies)
    payload = resp.json()

    assert resp.status_code == 500, payload

    for tier, parent in _tier_parents(cfg).items():
        assert (parent / "old-name").exists(), f"{tier} not rolled back"
        assert not (parent / "new-name").exists(), f"{tier} leaked partial rename"

    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows


def test_rename_refuses_archive_using_app_when_archive_unhealthy(cfg_factory: Any) -> None:
    """An app with app_archive=true cannot be renamed while the JuiceFS
    mount is transiently dead — would orphan the archive subdir under
    the old name."""
    cfg = cfg_factory(20299)

    # Seed an app whose manifest opts into app_archive.
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, '1.0', ?, ?, 'running', ?)""",
            (app_id, "old-name", "/tmp/repo/old-name", 19500, "[data]\napp_archive = true\n"),
        )
        db.commit()
    finally:
        db.close()
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    cookies = auth_cookie(cfg)
    with (
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=False),
        _client(cfg) as client,
    ):
        resp = client.post(f"/rename_app/{app_id}", json={"name": "new-name"}, cookies=cookies)
    payload = resp.json()
    assert resp.status_code == 503, payload
    assert "Archive backend" in (payload or {}).get("error", ""), payload

    parents_present = {
        "app_data": Path(cfg.persistent_data_dir) / "app_data",
        "app_temp_data": Path(cfg.temporary_data_dir) / "app_temp_data",
    }
    for tier, parent in parents_present.items():
        assert (parent / "old-name").exists(), tier
        assert not (parent / "new-name").exists(), tier


def test_rename_rollback_continues_when_a_rollback_rename_itself_fails(cfg_factory: Any) -> None:
    """If a rollback rename also fails, the endpoint must still return 500 surfacing the original forward-rename failure (not the rollback failure), continue rolling back the other renamed tiers, and restore the DB status field."""
    cfg = cfg_factory(20203)
    app_id = _seed_app_row(cfg.db_path, "old-name", status="running")
    _make_per_app_dirs(cfg, "old-name", ["app_data", "app_temp_data", "app_archive"])

    real_rename = os.rename
    archive_root = os.path.realpath(str(_archive_parent(cfg)))
    app_temp_root = os.path.realpath(str(Path(cfg.temporary_data_dir) / "app_temp_data"))

    def flaky_rename(src: str, dst: str) -> None:
        parent = os.path.realpath(os.path.dirname(src))
        if parent == archive_root and os.path.basename(src) == "old-name":
            raise OSError(28, "simulated transient archive mount failure")
        if parent == app_temp_root and os.path.basename(src) == "new-name":
            raise OSError(5, "simulated rollback rename failure")
        real_rename(src, dst)

    cookies = auth_cookie(cfg)
    with (
        mock.patch("os.rename", side_effect=flaky_rename),
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=True),
        _client(cfg) as client,
    ):
        resp = client.post(f"/rename_app/{app_id}", json={"name": "new-name"}, cookies=cookies)
    payload = resp.json()

    assert resp.status_code == 500, payload
    assert "transient archive mount failure" in (payload or {}).get("error", ""), payload

    parents = _tier_parents(cfg)
    assert (parents["app_data"] / "old-name").exists()
    assert not (parents["app_data"] / "new-name").exists()
    assert (parents["app_archive"] / "old-name").exists()
    assert not (parents["app_archive"] / "new-name").exists()
    assert (parents["app_temp_data"] / "new-name").exists()
    assert not (parents["app_temp_data"] / "old-name").exists()

    db = sqlite3.connect(cfg.db_path)
    try:
        rows = db.execute("SELECT name, status FROM apps").fetchall()
    finally:
        db.close()
    assert [(r[0], r[1]) for r in rows] == [("old-name", "running")], rows


def test_rename_non_archive_app_works_with_disabled_backend(cfg_factory: Any) -> None:
    """An app that doesn't use the archive tier (no app_archive, no
    access_all_archive) must rename successfully on a fresh zone where the
    archive backend is the default 'disabled'.  Pre-fix, the gate
    blocked all renames whenever the backend was unhealthy."""
    cfg = cfg_factory(20305)

    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, '1.0', ?, ?, 'stopped', ?)""",
            (app_id, "plain", "/tmp/repo/plain", 19510, "[data]\napp_data = true\n"),
        )
        db.commit()
    finally:
        db.close()
    _make_per_app_dirs(cfg, "plain", ["app_data", "app_temp_data"])

    # Don't mock is_archive_dir_healthy — let it return False for the
    # default disabled backend so the test exercises the real gate.
    status, payload = _post_rename(cfg, app_id, "renamed", mock_archive_healthy=False)
    assert status == 200, payload
    assert payload is not None
    assert payload["name"] == "renamed"


def test_rename_moves_local_archive_data(cfg_factory: Any) -> None:
    """On the default 'local' archive backend, renaming an archive-using app
    must move its archive subdir under the JuiceFS mountpoint (<old> ->
    <new>), not orphan it.  The archive tier is always the JuiceFS mount, so
    the effective archive parent is ``app_archive_dir`` for every backend."""
    cfg = cfg_factory(20260)
    # Fresh DB defaults to backend='local'; the archive parent is the mount.
    archive_parent = _archive_parent(cfg)
    assert archive_parent == Path(cfg.app_archive_dir)
    app_id = _seed_app_row(cfg.db_path, "old-name")
    # Seed data across all three tiers, archive on the LOCAL backend path.
    for tier, parent in _tier_parents(cfg).items():
        d = parent / "old-name"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sentinel.txt").write_text(tier)

    status, payload = _post_rename(cfg, app_id, "new-name")
    assert status == 200, payload

    assert not (archive_parent / "old-name").exists()
    assert (archive_parent / "new-name" / "sentinel.txt").read_text() == "app_archive"
