"""Tests for the CORS preflight (OPTIONS) handler on the v2 service-call path."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.db import provide_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.services_v2 import services_v2_routes

APP_NAME = "test-cors-app"
CALL_URL = f"/api/services/v2/call/{APP_NAME}/some-endpoint"
# get_app_from_hostname expects a Host-header style value (hostname[:port]),
# not a full URL — so Origin values here use that format.
APP_ORIGIN = f"{APP_NAME}.testzone.local"


def _make_app() -> Litestar:
    return Litestar(
        route_handlers=[services_v2_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _seed_app(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (new_app_id(), APP_NAME, "1.0.0", f"/tmp/{APP_NAME}", 19600, "running"),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return _make_test_config(tmp_path, port=20600)


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    # _make_test_config already seeded testzone.local as the primary, so the app-origin host
    # (<app>.testzone.local) resolves to a configured domain.
    _seed_app(cfg.db_path)
    with TestClient(app=_make_app()) as c:
        yield c


def test_options_known_app_origin_returns_cors_headers(client: TestClient[Litestar]) -> None:
    """OPTIONS with an Origin matching a known app should return 204 + CORS headers."""
    resp = client.options(CALL_URL, headers={"Origin": APP_ORIGIN})
    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == APP_ORIGIN
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"


def test_options_unknown_origin_returns_403(client: TestClient[Litestar]) -> None:
    """OPTIONS with an Origin that doesn't match any app should be rejected."""
    resp = client.options(CALL_URL, headers={"Origin": "evil.example.com"})
    assert resp.status_code == 403
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_options_no_origin_returns_403(client: TestClient[Litestar]) -> None:
    """OPTIONS without an Origin header should be rejected."""
    resp = client.options(CALL_URL)
    assert resp.status_code == 403
    assert "Access-Control-Allow-Origin" not in resp.headers
