"""Tests for the /remove_app/<app_id> route."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
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
    """Insert a row and return its newly minted app_id."""
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


def test_remove_returns_202_and_marks_removing(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """Happy path: row flips to 'removing' synchronously, worker is
    spawned, response is 202."""
    app_id = _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.Thread") as Thread:
        client.cookies.update(cookies)
        resp = client.post(f"/remove_app/{app_id}")

    assert resp.status_code == 202
    assert resp.json() == {"ok": True}
    Thread.assert_called_once()
    # Constructing the Thread isn't enough — the route must call .start().
    Thread.return_value.start.assert_called_once()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row == ("removing",)


def test_remove_404_when_app_missing(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    with patch("compute_space.web.routes.api.apps.Thread") as Thread:
        # Mint a valid-shaped id that won't exist in the DB.
        client.cookies.update(cookies)
        resp = client.post(f"/remove_app/{new_app_id()}")

    assert resp.status_code == 404
    body = resp.json()
    assert "not found" in (body.get("error") or "").lower()
    Thread.assert_not_called()


def test_remove_rolls_back_if_thread_spawn_fails(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """If Thread.start() raises (resource exhaustion), the route must
    flip the row to 'error' so the operator can retry from the
    dashboard. Otherwise the row sits stuck in 'removing' and every
    retry hits the already_removing short-circuit.
    """
    app_id = _seed_app(cfg.db_path, "myapp")

    failing_thread = MagicMock()
    failing_thread.return_value.start.side_effect = RuntimeError("can't start new thread")

    with patch("compute_space.web.routes.api.apps.Thread", failing_thread):
        client.cookies.update(cookies)
        resp = client.post(f"/remove_app/{app_id}")

    assert resp.status_code == 503
    assert "removal worker" in resp.json()["error"].lower()
    db = sqlite3.connect(cfg.db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row[0] == "error"


def test_concurrent_removes_only_spawn_one_worker(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """Two POSTs racing on the same app: only one wins the atomic claim."""
    app_id = _seed_app(cfg.db_path, "myapp")

    with patch("compute_space.web.routes.api.apps.Thread") as Thread:
        client.cookies.update(cookies)
        resp1 = client.post(f"/remove_app/{app_id}")
        resp2 = client.post(f"/remove_app/{app_id}")

    assert resp1.status_code == 202
    assert resp1.json() == {"ok": True}
    assert resp2.status_code == 202
    assert resp2.json().get("already_removing") is True
    assert Thread.call_count == 1


# Guards on sibling routes (stop, reload, rename) while a removal is in flight.


def test_stop_app_refuses_when_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    client.cookies.update(cookies)
    resp = client.post(f"/stop_app/{app_id}")
    assert resp.status_code == 409
    assert "removed" in resp.json()["error"].lower()


def test_reload_app_refuses_when_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    client.cookies.update(cookies)
    resp = client.post(f"/reload_app/{app_id}")
    assert resp.status_code == 409
    assert "removed" in resp.json()["error"].lower()


def test_rename_app_refuses_when_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="removing")
    client.cookies.update(cookies)
    resp = client.post(f"/rename_app/{app_id}", json={"name": "newname"})
    assert resp.status_code == 409
    assert "removed" in resp.json()["error"].lower()
