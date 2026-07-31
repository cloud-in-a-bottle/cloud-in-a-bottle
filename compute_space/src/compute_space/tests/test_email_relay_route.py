"""Route tests for the email API endpoints in web/routes/api/system.py.

Covered here:
  * ``GET /api/email/relay-config`` (guard ``require_app_auth``, scoped to
    ``config.email_mailbox_app_names``): an authenticated app NOT in that list is
    rejected; the mailbox app gets ``configured=false`` when email is disabled;
    the mailbox app gets the credential when email is enabled (relay fetch stubbed).
  * ``GET /api/email/custom-domain`` (guard ``require_owner_auth``): owner-only,
    surfaces the delegation record when a custom domain is set, else unconfigured.

The endpoints are exercised through a minimal Litestar app carrying just
``system_routes`` against a file-backed test DB, mirroring test_installer_route /
test_connect_routes.  App auth is a bearer token resolved against a seeded
``apps`` + ``app_tokens`` row; owner auth is a session cookie for a seeded user.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

import compute_space.web.routes.api.system as sys_mod
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialError
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import init_db
from compute_space.db import provide_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.routes.api.system import system_routes

_ZONE = "alice.example.com"
_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.5"
_MAILBOX_TOKEN = "mailbox-app-token"
_OTHER_TOKEN = "other-app-token"


def _cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


@pytest.fixture(autouse=True)
def _reset_relay_providers() -> Iterator[None]:
    # The route memoizes providers in a module-global dict; clear it between tests
    # so a stubbed provider from one test can't leak into another.
    sys_mod._relay_providers.clear()
    yield
    sys_mod._relay_providers.clear()


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20800, zone_domain=_ZONE)
    init_db(cfg.db_path)  # point get_db() (used by require_app_auth) at this DB
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


def _seed_app(cfg: Any, name: str, app_id: str, token: str) -> None:
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (app_id, name, "0.0.0", f"/tmp/{name}", 19700, "running"),
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute("INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)", (app_id, token_hash))
        conn.commit()
    finally:
        conn.close()


def _seed_mailbox_app(cfg: Any) -> None:
    _seed_app(cfg, "stalwart-email-server", "MailboxApp01", _MAILBOX_TOKEN)


def _seed_other_app(cfg: Any) -> None:
    _seed_app(cfg, "some-other-app", "OtherApp001", _OTHER_TOKEN)


def _seed_identity(cfg: Any) -> None:
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, _cred())


def _app_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


# --- relay-config: auth + scoping --------------------------------------------


def test_relay_config_requires_app_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/email/relay-config").status_code == 401


def test_relay_config_rejects_unknown_bearer(client: TestClient[Litestar]) -> None:
    resp = client.get("/api/email/relay-config", headers=_app_headers("not-a-real-token"))
    assert resp.status_code == 401


def test_relay_config_rejects_non_mailbox_app(cfg: Any, client: TestClient[Litestar]) -> None:
    # An authenticated app whose name is NOT in email_mailbox_app_names is refused.
    _seed_other_app(cfg)
    resp = client.get("/api/email/relay-config", headers=_app_headers(_OTHER_TOKEN))
    assert resp.status_code == 401


def test_relay_config_mailbox_app_unconfigured_when_email_off(cfg: Any, client: TestClient[Litestar]) -> None:
    # The mailbox app is allowed through the scope check, but email is off (no
    # identity/proxy/public_ip) -> configured=false with null fields.
    _seed_mailbox_app(cfg)
    resp = client.get("/api/email/relay-config", headers=_app_headers(_MAILBOX_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["smtp_relay_password"] is None
    assert body["smtp_relay_host"] is None


def test_relay_config_returns_credential_to_mailbox_app(
    cfg: Any, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Email enabled + a stubbed provider returning a credential.
    _seed_mailbox_app(cfg)
    _seed_identity(cfg)
    cfg_enabled = cfg.evolve(email_proxy_base_url=_PROXY, public_ip=_IP)

    set_active_config(cfg_enabled)

    cred = RelayCredential(
        smtp_relay_host="smtp.openhost.imbue.com",
        smtp_relay_port=465,
        smtp_relay_user=_ZONE,
        smtp_relay_password="hmac-pw",
        zone_domain=_ZONE,
        custom_domain=None,
    )

    class _StubProvider:
        def get(self, db: sqlite3.Connection) -> RelayCredential:
            return cred

    monkeypatch.setattr(sys_mod, "get_relay_credential_provider", lambda config: _StubProvider())
    resp = client.get("/api/email/relay-config", headers=_app_headers(_MAILBOX_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["smtp_relay_host"] == "smtp.openhost.imbue.com"
    assert body["smtp_relay_port"] == 465
    assert body["smtp_relay_password"] == "hmac-pw"
    assert body["zone_domain"] == _ZONE


def test_relay_config_unconfigured_when_provider_returns_none(
    cfg: Any, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_mailbox_app(cfg)
    _seed_identity(cfg)
    cfg_enabled = cfg.evolve(email_proxy_base_url=_PROXY, public_ip=_IP)

    set_active_config(cfg_enabled)

    class _NoneProvider:
        def get(self, db: sqlite3.Connection) -> RelayCredential | None:
            return None

    monkeypatch.setattr(sys_mod, "get_relay_credential_provider", lambda config: _NoneProvider())
    resp = client.get("/api/email/relay-config", headers=_app_headers(_MAILBOX_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_relay_config_unconfigured_on_credential_error(
    cfg: Any, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A RelayCredentialError while email is enabled degrades to unconfigured
    # (logged, not surfaced as a 500 to the mailbox app).

    _seed_mailbox_app(cfg)
    _seed_identity(cfg)
    cfg_enabled = cfg.evolve(email_proxy_base_url=_PROXY, public_ip=_IP)

    set_active_config(cfg_enabled)

    class _ErrorProvider:
        def get(self, db: sqlite3.Connection) -> RelayCredential:
            raise RelayCredentialError("frontend down")

    monkeypatch.setattr(sys_mod, "get_relay_credential_provider", lambda config: _ErrorProvider())
    resp = client.get("/api/email/relay-config", headers=_app_headers(_MAILBOX_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


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
    assert body["display_line"] == f"mail.mydomain.com   NS   ns.{_ZONE}"
