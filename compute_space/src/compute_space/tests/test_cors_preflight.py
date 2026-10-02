"""Tests for the CORS preflight (OPTIONS) handler on the v2 service-call path."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import upsert_record
from compute_space.db import provide_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.routes.services_v2 import services_v2_routes

APP_NAME = "test-cors-app"
CALL_URL = f"/api/services/v2/call/{APP_NAME}/some-endpoint"
APP_ORIGIN = f"https://{APP_NAME}.testzone.local"


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
                 (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_app_id(), APP_NAME, "1.0.0", f"/tmp/{APP_NAME}", 19600, "running"),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return _make_test_config(tmp_path, port=20600)


@pytest.fixture
def client(cfg: Config) -> Iterator[TestClient[Litestar]]:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("alternate.example", tls=True, mdns=False))
    with TestClient(app=_make_app()) as c:
        yield c


@pytest.mark.parametrize(
    "origin,allowed",
    [
        (APP_ORIGIN, True),
        (f"http://{APP_NAME}.testzone.local:18080", True),
        (f"https://{APP_NAME}.alternate.example:8443", True),
        (f"{APP_NAME}.testzone.local", True),
        (f"{APP_NAME}.testzone.local:18080", True),
        (f"{APP_NAME}.TESTZONE.LOCAL".upper(), True),
        (f"https://nested.{APP_NAME}.testzone.local", False),
        ("https://testzone.local", False),
        ("https://alternate.example:8443", False),
        ("https://evil.example.com", False),
        (f"https://{APP_NAME}.testzone.local.evil.example", False),
        ("https://bad_name.testzone.local", False),
        ("https://-bad.testzone.local", False),
        ("null", False),
        (None, False),
        ("", False),
        ("http://[", False),
        ("\x01" + APP_ORIGIN, False),
        (APP_ORIGIN + ":invalid", False),
        (APP_ORIGIN + ":65536", False),
        (f"{APP_NAME}.testzone.local:invalid", False),
        (APP_ORIGIN + "/path", False),
        (APP_ORIGIN + "?query", False),
        (APP_ORIGIN + "#fragment", False),
        (f"https://user@{APP_NAME}.testzone.local", False),
        (f"ftp://{APP_NAME}.testzone.local", False),
    ],
)
def test_preflight_does_not_disclose_app_existence(
    client: TestClient[Litestar], cfg: Config, origin: str | None, allowed: bool
) -> None:
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type, Authorization",
    }
    if origin is not None:
        headers["Origin"] = origin
    missing = client.options(CALL_URL, headers=headers)
    _seed_app(cfg.db_path)
    for status in ("running", "stopped"):
        with closing(open_db(cfg)) as db:
            db.execute("UPDATE apps SET status = ? WHERE name = ?", (status, APP_NAME))
            db.commit()
        installed = client.options(CALL_URL, headers=headers)
        assert installed.status_code == missing.status_code == (204 if allowed else 403)
        assert installed.content == missing.content
        assert installed.headers == missing.headers
    if allowed:
        assert missing.content == b""
        assert missing.headers["Access-Control-Allow-Origin"] == origin
        assert missing.headers["Access-Control-Allow-Credentials"] == "true"
        assert missing.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        assert missing.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"
    else:
        assert missing.json() == {"status_code": 403, "detail": "Forbidden"}
        assert "Access-Control-Allow-Origin" not in missing.headers


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_preflight_permission_does_not_authorize_actual_calls(
    client: TestClient[Litestar], cfg: Config, method: str, token: str | None
) -> None:
    headers = {"Origin": APP_ORIGIN}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    missing = client.request(method, CALL_URL, headers=headers)
    _seed_app(cfg.db_path)
    installed = client.request(method, CALL_URL, headers=headers)
    assert missing.status_code == installed.status_code == 401
    assert missing.json() == {"status_code": 401, "detail": "app authentication required"}
    assert installed.content == missing.content
    assert installed.headers == missing.headers
    assert "Access-Control-Allow-Origin" not in installed.headers
