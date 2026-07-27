"""Tests for the instance-side Connect-to-Imbue helpers (core/connect.py)."""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path
from typing import Any
from unittest import mock

import bcrypt
import httpx
import pytest
import typed_settings
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import DefaultConfig
from compute_space.config import active_config_path
from compute_space.config import provide_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.connect import ConnectError
from compute_space.core.connect import build_connect_url
from compute_space.core.connect import exchange_code_for_credential
from compute_space.core.connect import persist_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.settings import api_settings_routes

# --- build_connect_url ---------------------------------------------------------


def test_build_connect_url_composes_zone_and_callback() -> None:
    url = build_connect_url(
        "https://openhost.imbue.com",
        "alice.selfhost.imbue.com",
        "https://alice.selfhost.imbue.com",
    )
    assert url.startswith("https://openhost.imbue.com/connect/imbue?")
    assert "zone=alice.selfhost.imbue.com" in url
    # The callback is the instance's own origin + the callback path, URL-encoded.
    assert "callback=https%3A%2F%2Falice.selfhost.imbue.com%2Fapi%2Fsettings%2Fconnect-imbue%2Fcallback" in url


def test_build_connect_url_strips_trailing_slashes() -> None:
    url = build_connect_url("https://openhost.imbue.com/", "z.example.com", "https://z.example.com/")
    assert "https://openhost.imbue.com/connect/imbue?" in url
    assert "callback=https%3A%2F%2Fz.example.com%2Fapi" in url


# --- persist_instance_identity -------------------------------------------------


def _cred(
    issuer: str = "https://kc/realms/openhost-customers",
    client_id: str = "instance-alice",
    secret: str = "sekret",
) -> KeycloakClientCredentials:
    return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=secret)


def test_persist_writes_identity_into_new_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "alice.example.com"\ntls_enabled = true\n')
    persist_instance_identity(str(cfg), _cred())
    data = tomllib.loads(cfg.read_text())["openhost"]
    # New keys added...
    assert data["imbue_identity_issuer_url"] == "https://kc/realms/openhost-customers"
    assert data["imbue_identity_client_id"] == "instance-alice"
    assert data["imbue_identity_client_secret"] == "sekret"
    # ...and existing keys preserved.
    assert data["zone_domain"] == "alice.example.com"
    assert data["tls_enabled"] is True


def test_persist_overwrites_existing_identity(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[openhost]\nzone_domain = "a.example.com"\n'
        'imbue_identity_issuer_url = "old-iss"\n'
        'imbue_identity_client_id = "old-id"\n'
        'imbue_identity_client_secret = "old-secret"\n'
    )
    persist_instance_identity(str(cfg), _cred("new-iss", "new-id", "new-secret"))
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data["imbue_identity_issuer_url"] == "new-iss"
    assert data["imbue_identity_client_id"] == "new-id"
    assert data["imbue_identity_client_secret"] == "new-secret"


def test_persist_creates_section_when_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")  # empty file, no [openhost] section
    persist_instance_identity(str(cfg), _cred("iss", "id", "sec"))
    data = tomllib.loads(cfg.read_text())["openhost"]
    assert data["imbue_identity_client_secret"] == "sec"


def test_persist_result_is_loadable_by_config(tmp_path: Path) -> None:
    # The persisted config must actually load and yield a usable identity, so the
    # round-trip (persist -> load_config) proves the connect flow really enables it.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[openhost]\nzone_domain = "alice.example.com"\n')
    persist_instance_identity(str(cfg), _cred())
    loaded = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(cfg)])
    ident = loaded.instance_identity
    assert ident is not None
    assert ident.client_id == "instance-alice"
    assert ident.client_secret == "sekret"


# --- exchange_code_for_credential ----------------------------------------------


def _mock_httpx_post(monkeypatch: pytest.MonkeyPatch, handler) -> dict[str, object]:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def fake_post(url, json=None, timeout=None):  # type: ignore[no-untyped-def]
        seen["url"] = url
        seen["json"] = json
        return handler()

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_exchange_returns_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_httpx_post(
        monkeypatch,
        lambda: httpx.Response(
            200,
            json={
                "issuer_url": "https://kc/realms/openhost-customers",
                "client_id": "instance-alice",
                "client_secret": "sekret",
                "zone_domain": "alice.example.com",
            },
        ),
    )
    out = exchange_code_for_credential("https://openhost.imbue.com", "one-time-code")
    assert out == KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )
    assert seen["url"] == "https://openhost.imbue.com/connect/imbue/exchange"
    assert seen["json"] == {"code": "one-time-code"}


def test_exchange_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(400, json={"error": "expired"}))
    with pytest.raises(ConnectError, match="connect exchange failed"):
        exchange_code_for_credential("https://openhost.imbue.com", "bad")


def test_exchange_raises_on_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_httpx_post(monkeypatch, lambda: httpx.Response(200, json={"missing": "fields"}))
    with pytest.raises(ConnectError, match="malformed"):
        exchange_code_for_credential("https://openhost.imbue.com", "code")


def test_exchange_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, json=None, timeout=None):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(ConnectError, match="could not reach"):
        exchange_code_for_credential("https://openhost.imbue.com", "code")


def test_active_config_path_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", "/etc/openhost/config.toml")
    assert active_config_path() == "/etc/openhost/config.toml"
    monkeypatch.delenv("OPENHOST_ROUTER_CONFIG")
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    assert active_config_path() is None


# --- routes: /api/settings/connect-imbue/* ------------------------------------

_IMBUE = "https://openhost.imbue.com"
_IDENT = dict(
    imbue_identity_issuer_url="https://kc/realms/openhost-customers",
    imbue_identity_client_id="instance-alice",
    imbue_identity_client_secret="sekret",
)


@pytest.fixture
def connected_cfg(tmp_path: Path) -> Any:
    # An instance already holding a credential, with an Imbue front door set.
    cfg = _make_test_config(
        tmp_path, port=20600, zone_domain="alice.example.com", email_proxy_base_url=_IMBUE, public_ip="203.0.113.5", **_IDENT
    )
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def unconnected_cfg(tmp_path: Path) -> Any:
    # Imbue front door configured but no credential yet (the connect target case).
    cfg = _make_test_config(tmp_path, port=20601, zone_domain="alice.example.com", email_proxy_base_url=_IMBUE)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def no_imbue_cfg(tmp_path: Path) -> Any:
    # No Imbue front door configured at all -> the feature is unavailable.
    cfg = _make_test_config(tmp_path, port=20602, zone_domain="alice.example.com")
    init_db(cfg.db_path)
    return cfg


def _settings_client() -> TestClient[Litestar]:
    app = Litestar(
        route_handlers=[api_settings_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )
    return TestClient(app=app)


def _auth_cookie(cfg: Any) -> dict[str, str]:
    pw_hash = bcrypt.hashpw(b"secretpass1", bcrypt.gensalt()).decode()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)", ("owner", pw_hash)
        )
        assert cur.lastrowid is not None
        token = create_session(cur.lastrowid, conn)
        conn.commit()
    finally:
        conn.close()
    return {SESSION_COOKIE_NAME: token}


# -- status --


def test_status_requires_auth(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        assert c.get("/api/settings/connect-imbue/status").status_code == 401


def test_status_reports_available_and_unconnected(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.get("/api/settings/connect-imbue/status", cookies=_auth_cookie(unconnected_cfg))
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "connected": False}


def test_status_reports_connected(connected_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.get("/api/settings/connect-imbue/status", cookies=_auth_cookie(connected_cfg))
    assert resp.json() == {"available": True, "connected": True}


def test_status_reports_unavailable_without_imbue(no_imbue_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.get("/api/settings/connect-imbue/status", cookies=_auth_cookie(no_imbue_cfg))
    assert resp.json() == {"available": False, "connected": False}


# -- start --


def test_start_requires_auth(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        assert c.post("/api/settings/connect-imbue/start").status_code == 401


def test_start_returns_front_door_url(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.post("/api/settings/connect-imbue/start", cookies=_auth_cookie(unconnected_cfg))
    assert resp.status_code == 200
    url = resp.json()["redirect_url"]
    assert url.startswith(f"{_IMBUE}/connect/imbue?")
    assert "zone=alice.example.com" in url
    assert "callback=" in url


def test_start_503_without_imbue(no_imbue_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.post("/api/settings/connect-imbue/start", cookies=_auth_cookie(no_imbue_cfg))
    assert resp.status_code == 503


# -- callback --


def test_callback_requires_auth(unconnected_cfg: Any) -> None:
    with _settings_client() as c:
        assert c.get("/api/settings/connect-imbue/callback?code=x").status_code == 401


def test_callback_happy_path(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(Path(unconnected_cfg.data_root_dir) / "config.toml"))
    Path(unconnected_cfg.data_root_dir, "config.toml").write_text('[openhost]\nzone_domain = "alice.example.com"\n')
    restarted: dict[str, bool] = {}
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            return_value=_cred(),
        ),
        mock.patch("compute_space.web.routes.api.settings.persist_instance_identity") as persist,
        mock.patch(
            "compute_space.web.routes.api.settings.trigger_restart",
            side_effect=lambda: restarted.setdefault("v", True),
        ),
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=onetime",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=ok"
    persist.assert_called_once()
    assert restarted.get("v") is True


def test_callback_blank_code_redirects_error(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(Path(unconnected_cfg.data_root_dir) / "config.toml"))
    with _settings_client() as c:
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/settings?connect=error"


def test_callback_502_on_exchange_failure(unconnected_cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(Path(unconnected_cfg.data_root_dir) / "config.toml"))
    Path(unconnected_cfg.data_root_dir, "config.toml").write_text('[openhost]\nzone_domain = "alice.example.com"\n')
    with (
        mock.patch(
            "compute_space.web.routes.api.settings.exchange_code_for_credential",
            side_effect=ConnectError("expired"),
        ),
        _settings_client() as c,
    ):
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=bad",
            cookies=_auth_cookie(unconnected_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 502


def test_callback_503_without_imbue(no_imbue_cfg: Any) -> None:
    with _settings_client() as c:
        resp = c.get(
            "/api/settings/connect-imbue/callback?code=x",
            cookies=_auth_cookie(no_imbue_cfg),
            follow_redirects=False,
        )
    assert resp.status_code == 503
