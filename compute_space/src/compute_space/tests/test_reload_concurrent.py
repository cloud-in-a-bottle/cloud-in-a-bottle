"""Tests for the concurrent-reload guard on ``/reload_app/<app_id>``.

Spamming the "Reload" button used to spawn several ``reload_app_background``
threads that raced to create the same ``openhost-<name>`` container and failed
with "container name is already in use" (OH-104). ``_reload_app_impl`` now
atomically claims the reload — flipping the row to 'building' only if it isn't
already in a transient state — so a second concurrent reload is refused with a
409 instead of spawning a competing worker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.core.app_id import new_app_id
from compute_space.core.domains import PRIMARY_DOMAIN_APP_RESTART_MARKER
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_app(db_path: str, name: str, status: str = "running") -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, '1.0', '/r', 19500, ?)",
            (app_id, name, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _status(db_path: str, app_id: str) -> str:
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row is not None
    return str(row[0])


def test_plain_reload_claims_building_and_spawns_worker(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """A first reload flips the row to 'building', stops the old container,
    and spawns a background worker."""
    app_id = _seed_app(cfg.db_path, "myapp")

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container") as stop,
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    Thread.assert_called_once()
    Thread.return_value.start.assert_called_once()
    stop.assert_called_once()
    assert _status(cfg.db_path, app_id) == "building"


def test_reload_worker_spawn_failure_marks_app_error(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET container_id = 'old-container' WHERE app_id = ?", (app_id,))
    db.commit()
    db.close()

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container"),
    ):
        Thread.return_value.start.side_effect = RuntimeError("thread unavailable")
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 503
    assert _status(cfg.db_path, app_id) == "error"
    db = sqlite3.connect(cfg.db_path)
    container_id = db.execute("SELECT container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
    db.close()
    assert container_id is None


def test_reload_stop_failure_preserves_container_reference(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET container_id = 'old-container' WHERE app_id = ?", (app_id,))
    db.commit()
    db.close()

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container", side_effect=RuntimeError("remove failed")),
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 503
    Thread.assert_not_called()
    db = sqlite3.connect(cfg.db_path)
    row = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    db.close()
    assert row == ("error", "old-container")


@pytest.mark.parametrize("busy_status", ["building", "starting"])
def test_reload_refused_while_transient(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], busy_status: str
) -> None:
    """A reload is refused with 409 while an operation is already in flight
    (the row is in a transient state), and no second worker is spawned nor
    is the running container stopped."""
    app_id = _seed_app(cfg.db_path, "busyapp", status=busy_status)

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container") as stop,
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "App is already reloading"
    Thread.assert_not_called()
    stop.assert_not_called()
    # The in-flight operation's status is left untouched.
    assert _status(cfg.db_path, app_id) == busy_status


def test_reload_refused_while_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """A removing app is refused earlier (by the dedicated removing guard),
    also with 409 and no worker — belt-and-suspenders with the reload claim."""
    app_id = _seed_app(cfg.db_path, "goingaway", status="removing")

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container") as stop,
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 409
    Thread.assert_not_called()
    stop.assert_not_called()
    assert _status(cfg.db_path, app_id) == "removing"


@pytest.mark.parametrize("reloadable_status", ["running", "stopped", "error"])
def test_reload_allowed_from_settled_states(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], reloadable_status: str
) -> None:
    """A settled app (running/stopped/error) can always be reloaded."""
    app_id = _seed_app(cfg.db_path, "settled", status=reloadable_status)

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container"),
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 200
    Thread.assert_called_once()
    assert _status(cfg.db_path, app_id) == "building"


def test_preclaim_reload_error_does_not_overwrite_domain_restart(cfg: Any) -> None:
    app_id = _seed_app(cfg.db_path, "claimed", status="starting")
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE apps SET error_message = ? WHERE app_id = ?",
            (PRIMARY_DOMAIN_APP_RESTART_MARKER, app_id),
        )
        db.commit()
        recorded = apps_routes._record_reload_error_if_unclaimed(db, app_id, None, "git failed")
        row = db.execute("SELECT status, error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row == ("starting", PRIMARY_DOMAIN_APP_RESTART_MARKER)
    assert recorded is False


def test_preclaim_reload_error_does_not_overwrite_replacement_container(cfg: Any) -> None:
    app_id = _seed_app(cfg.db_path, "replaced")
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute("UPDATE apps SET container_id = 'new-container' WHERE app_id = ?", (app_id,))
        db.commit()
        recorded = apps_routes._record_reload_error_if_unclaimed(db, app_id, "old-container", "git failed")
        row = db.execute(
            "SELECT status, container_id, error_message FROM apps WHERE app_id = ?",
            (app_id,),
        ).fetchone()
    finally:
        db.close()
    assert row == ("running", "new-container", None)
    assert recorded is False


def test_reload_refuses_stale_container_generation(
    cfg: Any,
    client: TestClient[Litestar],
    cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = _seed_app(cfg.db_path, "replaced")
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET container_id = 'old-container' WHERE app_id = ?", (app_id,))
    db.commit()
    db.close()

    def replace_container(_config: Any, _db: sqlite3.Connection) -> bool:
        replacement_db = sqlite3.connect(cfg.db_path)
        replacement_db.execute("UPDATE apps SET container_id = 'new-container' WHERE app_id = ?", (app_id,))
        replacement_db.commit()
        replacement_db.close()
        return True

    monkeypatch.setattr(apps_routes.archive_backend, "is_archive_dir_healthy", replace_container)
    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_container") as stop,
    ):
        client.cookies.update(cookies)
        resp = client.post(f"/reload_app/{app_id}")

    assert resp.status_code == 409
    Thread.assert_not_called()
    stop.assert_not_called()
    db = sqlite3.connect(cfg.db_path)
    row = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    db.close()
    assert row == ("running", "new-container")
