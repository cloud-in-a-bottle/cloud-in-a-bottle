"""Tests for ``/api/app_status/<app_id>`` error-kind logic and the
``/app_status_stream/<app_id>`` WebSocket that pushes the same payload on change.

In particular the legacy ``[CACHE_CORRUPT]`` marker still needs to map to
``error_kind = "build_cache_corrupt"`` so the dashboard's 'drop cache
and rebuild' toast keeps firing against rows whose ``error_message``
column was written before the marker rename.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.config import get_config
from compute_space.core.app_id import new_app_id
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.updates import initialize_shutdown_event
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests._litestar_helpers import ws_cookie_header
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20100)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_app_with_error(cfg: Any, error_message: str, port: int) -> str:
    """Insert one app row with the given error_message and return its app_id."""
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, error_message)
               VALUES (?, 'notes', '1.0', '/repo/notes', ?, 'error', ?)""",
            (app_id, port, error_message),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_current_marker_maps_to_build_cache_corrupt_error_kind(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app_with_error(
        cfg,
        error_message=f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.",
        port=20110,
    )
    client.cookies.update(cookies)
    resp = client.get(f"/api/app_status/{app_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error_kind"] == "build_cache_corrupt"
    assert payload["error"] == "Container build cache is corrupted."


def test_legacy_marker_still_maps_to_build_cache_corrupt_error_kind(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """Rows whose error_message was written before the marker rename
    must still trigger the 'drop cache' remediation toast in the UI."""
    app_id = _seed_app_with_error(
        cfg,
        error_message="[CACHE_CORRUPT] Docker build cache is corrupted.",
        port=20111,
    )
    client.cookies.update(cookies)
    resp = client.get(f"/api/app_status/{app_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error_kind"] == "build_cache_corrupt"
    assert payload["error"] == "Container build cache is corrupted."


def test_unrelated_error_message_has_no_error_kind(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app_with_error(
        cfg,
        error_message="App started but not responding to HTTP",
        port=20112,
    )
    client.cookies.update(cookies)
    resp = client.get(f"/api/app_status/{app_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error_kind"] is None


def _set_app(cfg: Any, app_id: str, **cols: Any) -> None:
    db = sqlite3.connect(cfg.db_path)
    try:
        assignments = ", ".join(f"{col} = ?" for col in cols)
        db.execute(f"UPDATE apps SET {assignments} WHERE app_id = ?", (*cols.values(), app_id))
        db.commit()
    finally:
        db.close()


def test_status_stream_requires_auth(cfg: Any, client: TestClient[Litestar]) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/app_status_stream/{new_app_id()}") as ws:  # no cookie
            ws.receive_json()
    assert exc.value.code == 4401


def test_status_stream_pushes_changes_and_closes_when_row_vanishes(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One connect covers: initial frame, plain-error push, cache-corrupt
    classification, and the 4404 close after the row is deleted."""
    # The handler races its pump against wait_for_shutdown(), which is an instant
    # no-op unless the app-startup event is wired — the stream would die immediately.
    initialize_shutdown_event(asyncio.Event())
    monkeypatch.setattr(apps_routes, "STATUS_STREAM_POLL_SECONDS", 0.01)
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, 'notes', '1.0', '/repo/notes', 20113, 'building')""",
            (app_id,),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/app_status_stream/{app_id}", headers=ws_cookie_header(cookies)) as ws:
            assert ws.receive_json() == {
                "status": "building",
                "error": None,
                "error_kind": None,
                "container_id": None,
            }

            _set_app(cfg, app_id, status="error", error_message="Container build failed (exit code 1):\n...tail")
            frame = ws.receive_json()
            assert frame["status"] == "error"
            assert frame["error"] == "Container build failed (exit code 1):\n...tail"
            assert frame["error_kind"] is None

            _set_app(cfg, app_id, error_message=f"{BUILD_CACHE_CORRUPT_MARKER} boom")
            frame = ws.receive_json()
            assert frame["error"] == "Container build cache is corrupted."
            assert frame["error_kind"] == "build_cache_corrupt"

            db = sqlite3.connect(cfg.db_path)
            try:
                db.execute("DELETE FROM apps WHERE app_id = ?", (app_id,))
                db.commit()
            finally:
                db.close()
            ws.receive_json()  # the watcher notices the missing row and closes
    assert exc.value.code == 4404


def test_import_wiring() -> None:
    """Make sure the module under test is still importing BUILD_CACHE_CORRUPT_MARKER
    the way app_status expects; a rename in core/containers.py that
    forgot the route handler would otherwise pass all other checks."""
    assert apps_routes.BUILD_CACHE_CORRUPT_MARKER == BUILD_CACHE_CORRUPT_MARKER


def test_get_config_is_present_for_router_tests() -> None:
    """Sanity: get_config() shouldn't raise at import time."""
    assert callable(get_config)
