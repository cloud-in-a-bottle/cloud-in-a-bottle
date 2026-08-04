"""Route tests for the owner-facing email API in web/routes/api/system.py.

Covers ``GET /api/email/custom-domain`` (guard ``require_owner_auth``): owner
only, surfaces the NS delegation record when a custom mail domain is set, else
unconfigured. (Outbound send is no longer an HTTP route: apps relay through the
router's SMTP submission listener, tested in test_email_smtp_service.py.)

The endpoint is exercised through a minimal Litestar app carrying just
``system_routes`` against a file-backed test DB. Owner auth is a session cookie
for a seeded user.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.db import init_db
from compute_space.db import provide_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.system import system_routes

_ZONE = "alice.example.com"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20800, zone_domain=_ZONE)
    init_db(cfg.db_path)  # point get_db() (used by the owner-auth guard) at this DB
    return cfg


def _make_app() -> Litestar:
    return Litestar(
        route_handlers=[system_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=_make_app()) as c:
        yield c


def _owner_cookie(cfg: Any) -> dict[str, str]:
    pw_hash = bcrypt.hashpw(b"secretpass1", bcrypt.gensalt()).decode()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("owner", pw_hash))
        assert cur.lastrowid is not None
        token = create_session(cur.lastrowid, conn)
        conn.commit()
    finally:
        conn.close()
    return {SESSION_COOKIE_NAME: token}


# --- custom-domain: owner auth -----------------------------------------------


def test_custom_domain_requires_owner_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/email/custom-domain").status_code == 401


def test_custom_domain_unconfigured_when_unset(cfg: Any, client: TestClient[Litestar]) -> None:
    resp = client.get("/api/email/custom-domain", cookies=_owner_cookie(cfg))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["domain"] is None
    assert body["display_line"] is None


def test_custom_domain_returns_delegation_record_when_set(cfg: Any, client: TestClient[Litestar]) -> None:
    cfg_custom = cfg.evolve(email_custom_domain="mail.mydomain.com")

    set_active_config(cfg_custom)
    resp = client.get("/api/email/custom-domain", cookies=_owner_cookie(cfg))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["domain"] == "mail.mydomain.com"
    assert body["record_name"] == "mail.mydomain.com"
    assert body["record_type"] == "NS"
    assert body["record_value"] == f"ns.{_ZONE}"
