"""The "Connect to Imbue" settings routes (``web/routes/api/settings.py``).

Exercised through a minimal Litestar app carrying just ``api_settings_routes``
under the real ``require_owner_auth`` guard, against a file-backed test DB.  The
status/start/callback endpoints read the connect URL + identity live from the
settings table, so the tests seed those via the identity_store setters and assert
the routes' HTTP behavior (auth, availability, redirects, error codes) and the
callback's DB side effect.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs
from urllib.parse import urlparse

import bcrypt
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.identity_store import IMBUE_CONNECT_BASE_URL_KEY
from compute_space.core.identity_store import get_stored_instance_identity
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.settings_store import set_setting
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import init_db
from compute_space.db import provide_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.routes.api.settings import api_settings_routes

_IMBUE = "https://openhost.imbue.com"
_ZONE = "alice.example.com"


def _cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    # seed_primary=True (default) so primary_domain(db) resolves for /start.
    cfg = _make_test_config(tmp_path, port=20700, zone_domain=_ZONE)
    init_db(cfg.db_path)
    return cfg


def _seed_connect_url(cfg: Any, url: str = _IMBUE) -> None:
    with closing(open_db(cfg)) as db:
        set_setting(db, IMBUE_CONNECT_BASE_URL_KEY, url)


def _seed_identity(cfg: Any, cred: KeycloakClientCredentials | None = None) -> None:
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, cred or _cred())


def _make_settings_app() -> Litestar:
    return Litestar(
        route_handlers=[api_settings_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=_make_settings_app()) as c:
        yield c


def _auth_cookie(cfg: Any) -> dict[str, str]:
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


# --- status ------------------------------------------------------------------


def test_status_requires_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/settings/connect-imbue/status").status_code == 401


def test_status_unavailable_and_unconnected_when_empty(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get("/api/settings/connect-imbue/status")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "connected": False}


def test_status_available_when_connect_url_present(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get("/api/settings/connect-imbue/status")
    assert resp.json() == {"available": True, "connected": False}


def test_status_connected_when_identity_present(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_identity(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get("/api/settings/connect-imbue/status")
    # Connected true (identity resolves), available false (no connect URL seeded).
    assert resp.json() == {"available": False, "connected": True}


def test_status_available_and_connected(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    _seed_identity(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get("/api/settings/connect-imbue/status")
    assert resp.json() == {"available": True, "connected": True}


def test_status_not_connected_with_partial_identity(cfg: Any, client: TestClient[Litestar]) -> None:
    # Only two of three identity parts stored -> not connected.
    with closing(open_db(cfg)) as db:
        set_setting(db, "imbue_identity_issuer_url", "iss")
        set_setting(db, "imbue_identity_client_id", "cid")
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get("/api/settings/connect-imbue/status")
    assert resp.json()["connected"] is False


# --- start -------------------------------------------------------------------


def test_start_requires_auth(client: TestClient[Litestar]) -> None:
    assert client.post("/api/settings/connect-imbue/start").status_code == 401


def test_start_503_without_connect_url(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg))
    resp = client.post("/api/settings/connect-imbue/start")
    assert resp.status_code == 503


def test_start_returns_redirect_url_with_zone_and_callback(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.post("/api/settings/connect-imbue/start")
    assert resp.status_code == 200
    url = resp.json()["redirect_url"]
    assert url.startswith(f"{_IMBUE}/connect/imbue?")
    qs = parse_qs(urlparse(url).query)
    assert qs["zone"] == [_ZONE]
    assert qs["callback"][0].endswith("/api/settings/connect-imbue/callback")


def test_start_honors_forwarded_proto_and_host(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.post(
        "/api/settings/connect-imbue/start",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "proxy.example.com"},
    )
    url = resp.json()["redirect_url"]
    qs = parse_qs(urlparse(url).query)
    # The callback origin is derived from the forwarded proxy headers.
    assert qs["callback"] == ["https://proxy.example.com/api/settings/connect-imbue/callback"]
    # Zone still comes from the primary domain, not the proxy host.
    assert qs["zone"] == [_ZONE]


def test_start_callback_host_falls_back_to_host_header(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.post(
        "/api/settings/connect-imbue/start",
        headers={"host": "direct.example.com"},
    )
    qs = parse_qs(urlparse(resp.json()["redirect_url"]).query)
    assert qs["callback"][0].startswith("http://direct.example.com/") or qs["callback"][0].startswith(
        "https://direct.example.com/"
    )


# --- callback ----------------------------------------------------------------


def test_callback_requires_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/settings/connect-imbue/callback?code=x").status_code == 401


def test_callback_503_without_connect_url(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get(
        "/api/settings/connect-imbue/callback?code=x",
        follow_redirects=False,
    )
    assert resp.status_code == 503


def test_callback_blank_code_redirects_error(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get(
        "/api/settings/connect-imbue/callback?code=",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=error"


def test_callback_whitespace_code_redirects_error(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get(
        "/api/settings/connect-imbue/callback?code=%20%20",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=error"


def test_callback_missing_code_param_redirects_error(cfg: Any, client: TestClient[Litestar]) -> None:
    # No ?code= at all defaults to "" -> the blank-code error redirect.
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    resp = client.get(
        "/api/settings/connect-imbue/callback",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=error"


def test_callback_happy_path_stores_identity_and_redirects_ok(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ) as exchange,
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
    ):
        client.cookies.update(_auth_cookie(cfg))
        resp = client.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=ok"
    # The exchange was called with the connect URL + the (stripped) code.
    exchange.assert_called_once_with(_IMBUE, "onetime")
    # No restart is triggered — the DB is read live.
    restart.assert_not_called()
    # The credential was persisted to the settings table.
    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == _cred()


def test_callback_strips_whitespace_around_code_before_exchange(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ) as exchange,
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
    ):
        client.cookies.update(_auth_cookie(cfg))
        resp = client.get(
            "/api/settings/connect-imbue/callback?code=%20onetime%20",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/settings?connect=ok"
    exchange.assert_called_once_with(_IMBUE, "onetime")


def test_callback_connect_error_returns_502(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            side_effect=RuntimeError("code expired"),
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart") as restart,
    ):
        client.cookies.update(_auth_cookie(cfg))
        resp = client.get(
            "/api/settings/connect-imbue/callback?code=bad",
            follow_redirects=False,
        )
    assert resp.status_code == 502
    restart.assert_not_called()
    # Nothing was persisted on failure.
    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) is None


def test_callback_connect_error_does_not_overwrite_existing_identity(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    prior = KeycloakClientCredentials(issuer_url="prior-iss", client_id="prior-cid", client_secret="prior-sec")
    _seed_identity(cfg, prior)
    with mock.patch(
        "compute_space.web.routes.api.settings.exchange_code_for_credential",
        side_effect=RuntimeError("boom"),
    ):
        client.cookies.update(_auth_cookie(cfg))
        resp = client.get(
            "/api/settings/connect-imbue/callback?code=bad",
            follow_redirects=False,
        )
    assert resp.status_code == 502
    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == prior


def test_callback_overwrites_existing_identity_on_success(cfg: Any, client: TestClient[Litestar]) -> None:
    _seed_connect_url(cfg)
    prior = KeycloakClientCredentials(issuer_url="prior-iss", client_id="prior-cid", client_secret="prior-sec")
    _seed_identity(cfg, prior)
    new = KeycloakClientCredentials(issuer_url="new-iss", client_id="new-cid", client_secret="new-sec")
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=new,
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
    ):
        client.cookies.update(_auth_cookie(cfg))
        resp = client.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            follow_redirects=False,
        )
    assert resp.headers["location"] == "/settings?connect=ok"
    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == new


def test_callback_makes_status_report_connected_afterwards(cfg: Any, client: TestClient[Litestar]) -> None:
    # End-to-end: after a successful callback, /status must flip connected -> true
    # (read live from the settings table, no restart in between).
    _seed_connect_url(cfg)
    client.cookies.update(_auth_cookie(cfg))
    before = client.get("/api/settings/connect-imbue/status").json()
    assert before == {"available": True, "connected": False}
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ),
        mock.patch("compute_space.web.routes.api.settings.trigger_restart"),
    ):
        client.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            follow_redirects=False,
        )
    after = client.get("/api/settings/connect-imbue/status").json()
    assert after == {"available": True, "connected": True}
