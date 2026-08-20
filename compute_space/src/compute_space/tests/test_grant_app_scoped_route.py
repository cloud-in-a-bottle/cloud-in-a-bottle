"""Route tests for POST /api/permissions/v2/grant_app_scoped.

The interesting surface is how the consumer is identified.  Providers name the requesting app on
their own consent screen, so the grant is keyed to ``consumer_app_name``: a provider that displays
one app and grants to another ends up granting to the app it displayed.  ``consumer_app_id`` is
still honoured for providers written before that change.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_routes

SERVICE = "github.com/example/notes"

PROVIDER_NAME = "md-notes"
PROVIDER_ID = "TestProvider1"
PROVIDER_TOKEN = "test-provider-token"  # noqa: S105

CONSUMER_NAME = "notes-reader"
CONSUMER_ID = "TestConsumer1"
# A second installed app, to stand in for the one a misleading consent screen might name.
OTHER_NAME = "photos"
OTHER_ID = "TestOther001"


def _make_app() -> Litestar:
    return Litestar(
        route_handlers=[api_permissions_v2_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _add_app(db: sqlite3.Connection, app_id: str, name: str, port: int) -> None:
    db.execute(
        """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (app_id, name, "0.1.0", f"/tmp/{name}", port, "running"),
    )


def _seed(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        _add_app(db, PROVIDER_ID, PROVIDER_NAME, 19601)
        _add_app(db, CONSUMER_ID, CONSUMER_NAME, 19602)
        _add_app(db, OTHER_ID, OTHER_NAME, 19603)
        db.execute(
            """INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint)
               VALUES (?, ?, ?, ?)""",
            (SERVICE, PROVIDER_ID, "0.1.0", "/api/"),
        )
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (PROVIDER_ID, hashlib.sha256(PROVIDER_TOKEN.encode()).hexdigest()),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return _make_test_config(tmp_path, port=20600)


@pytest.fixture
def db_path(cfg: Any) -> str:
    return str(cfg.db_path)


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    init_db(cfg.db_path)
    _seed(cfg.db_path)
    with TestClient(app=_make_app()) as c:
        yield c


def _grant(client: TestClient[Litestar], **body: Any) -> Any:
    return client.post(
        "/api/permissions/v2/grant_app_scoped",
        json={"service_url": SERVICE, "grant": {"vault": "personal"}, **body},
        headers={"Authorization": f"Bearer {PROVIDER_TOKEN}"},
    )


def _rows(db_path: str, app_id: str | None = None) -> list[dict[str, Any]]:
    """Grants written to the DB — read directly, since listing them over HTTP needs owner auth."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if app_id is None:
            found = conn.execute("SELECT * FROM permissions_v2").fetchall()
        else:
            found = conn.execute("SELECT * FROM permissions_v2 WHERE consumer_app_id = ?", (app_id,)).fetchall()
        return [dict(r) for r in found]
    finally:
        conn.close()


def test_grants_by_consumer_app_name(client: TestClient[Litestar], db_path: str) -> None:
    assert _grant(client, consumer_app_name=CONSUMER_NAME).status_code == 200
    granted = _rows(db_path, CONSUMER_ID)
    assert len(granted) == 1
    assert granted[0]["scope"] == "app"
    assert granted[0]["provider_app_id"] == PROVIDER_ID


def test_naming_another_app_grants_that_app_not_the_provider(client: TestClient[Litestar], db_path: str) -> None:
    """The point of keying on the name: misnaming the consumer can't benefit the misnamer."""
    assert _grant(client, consumer_app_name=OTHER_NAME).status_code == 200
    assert len(_rows(db_path, OTHER_ID)) == 1
    assert _rows(db_path, CONSUMER_ID) == []
    assert _rows(db_path, PROVIDER_ID) == []


def test_unknown_consumer_app_name_is_404(client: TestClient[Litestar], db_path: str) -> None:
    response = _grant(client, consumer_app_name="no-such-app")
    assert response.status_code == 404
    assert "no-such-app" in response.json()["detail"]
    assert _rows(db_path) == []


def test_consumer_app_id_still_works(client: TestClient[Litestar], db_path: str) -> None:
    """Providers written before the name field keep working across a router upgrade."""
    assert _grant(client, consumer_app_id=CONSUMER_ID).status_code == 200
    assert len(_rows(db_path, CONSUMER_ID)) == 1


def test_name_wins_when_both_are_supplied(client: TestClient[Litestar], db_path: str) -> None:
    assert _grant(client, consumer_app_name=OTHER_NAME, consumer_app_id=CONSUMER_ID).status_code == 200
    assert len(_rows(db_path, OTHER_ID)) == 1
    assert _rows(db_path, CONSUMER_ID) == []


def test_missing_consumer_is_400(client: TestClient[Litestar], db_path: str) -> None:
    assert _grant(client).status_code == 400
    assert _rows(db_path) == []


def test_cannot_grant_for_a_service_the_caller_does_not_provide(client: TestClient[Litestar], db_path: str) -> None:
    """A valid app token is not enough — the caller must be a registered provider."""
    response = client.post(
        "/api/permissions/v2/grant_app_scoped",
        json={"service_url": "github.com/example/other", "grant": "x", "consumer_app_name": CONSUMER_NAME},
        headers={"Authorization": f"Bearer {PROVIDER_TOKEN}"},
    )
    assert response.status_code == 403
    assert _rows(db_path) == []
