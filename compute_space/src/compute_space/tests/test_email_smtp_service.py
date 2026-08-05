"""Tests for the router-mediated outbound email SMTP service.

The router runs a local SMTP submission listener that apps relay through. An app
authenticates with SMTP AUTH (username = app name, password = OPENHOST_APP_TOKEN),
must hold the ``email`` v2 service ``send`` grant, and the router then relays the
message to the Imbue smarthost with the per-instance relay credential attached
(the app never sees it).

These tests exercise the auth resolution, the grant check, the Authenticator
callback, and the handler's DATA relay path (smarthost call stubbed).
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.smtp import LoginPassword

from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.email import smtp_service
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialError
from compute_space.core.email.service import EMAIL_GRANT_SEND
from compute_space.core.email.service import EMAIL_SERVICE_URL
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

_ZONE = "alice.example.com"
_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.5"
_TOKEN = "sender-app-token"


def _cred_identity() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20900, zone_domain=_ZONE)
    init_db(cfg.db_path)  # point get_db() at this DB (used by the auth/grant helpers)
    return cfg


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


def _grant_send(app_id: str) -> None:
    grant_permission_v2(app_id, EMAIL_SERVICE_URL, EMAIL_GRANT_SEND)


# --- auth resolution ---------------------------------------------------------


def test_auth_resolves_app_id_for_valid_token(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    assert smtp_service._authenticated_app_id("mailer", _TOKEN) == "MailerApp01"


def test_auth_rejects_unknown_token(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    assert smtp_service._authenticated_app_id("mailer", "wrong-token") is None


def test_auth_rejects_name_token_mismatch(cfg: Any) -> None:
    # Valid token, but the claimed username is a different app.
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    assert smtp_service._authenticated_app_id("someone-else", _TOKEN) is None


# --- grant check -------------------------------------------------------------


def test_app_may_send_requires_grant(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    assert smtp_service._app_may_send("MailerApp01") is False
    _grant_send("MailerApp01")
    assert smtp_service._app_may_send("MailerApp01") is True


def test_app_may_send_ignores_other_service_grant(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    grant_permission_v2("MailerApp01", "github.com/other/service", "send")
    assert smtp_service._app_may_send("MailerApp01") is False


# --- Authenticator callback --------------------------------------------------


def _auth(cfg: Any, login: str, password: str) -> Any:
    # aiosmtpd passes a LoginPassword; only .login/.password are read.
    authnr = smtp_service.EmailServiceAuthenticator()
    data = LoginPassword(login.encode(), password.encode())
    return authnr(None, None, None, "LOGIN", data)  # type: ignore[arg-type]


def test_authenticator_success_binds_app_id(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    _grant_send("MailerApp01")
    result = _auth(cfg, "mailer", _TOKEN)
    assert result.success is True
    assert result.auth_data == "MailerApp01"


def test_authenticator_fails_without_grant(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    result = _auth(cfg, "mailer", _TOKEN)
    assert result.success is False


def test_authenticator_fails_bad_token(cfg: Any) -> None:
    _seed_app(cfg, "mailer", "MailerApp01", _TOKEN)
    _grant_send("MailerApp01")
    result = _auth(cfg, "mailer", "nope")
    assert result.success is False


# --- handler DATA relay ------------------------------------------------------


class _Envelope:
    def __init__(self, mail_from: str, rcpts: list[str], content: bytes) -> None:
        self.mail_from = mail_from
        self.rcpt_tos = rcpts
        self.content = content
        self.original_content = content


class _Session:
    def __init__(self, auth_data: object) -> None:
        self.auth_data = auth_data


def _enable_email(cfg: Any) -> Any:
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, _cred_identity())
    return cfg.evolve(email_proxy_base_url=_PROXY, public_ip=_IP)


async def _run_data(handler: smtp_service.EmailServiceHandler, session: Any, envelope: Any) -> str:
    return await handler.handle_DATA(None, session, envelope)  # type: ignore[arg-type]


def _cred() -> RelayCredential:
    return RelayCredential(
        smtp_relay_host="smtp.openhost.imbue.com",
        smtp_relay_port=465,
        smtp_relay_user=_ZONE,
        smtp_relay_password="hmac-pw",
    )


@pytest.mark.asyncio
async def test_handle_data_relays_with_injected_credential(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_enabled = _enable_email(cfg)
    handler = smtp_service.EmailServiceHandler(cfg_enabled)

    # Stub the credential fetch and capture the smarthost relay call.
    monkeypatch.setattr(handler, "_fetch_credential", lambda: _cred())
    seen: dict[str, Any] = {}

    def _fake_relay(cred: RelayCredential, raw: bytes, frm: str, tos: list[str]) -> None:
        seen["cred"] = cred
        seen["raw"] = raw
        seen["from"] = frm
        seen["tos"] = tos

    monkeypatch.setattr(smtp_service, "_relay_to_smarthost", _fake_relay)

    env = _Envelope("me@alice.example.com", ["dest@example.net"], b"Subject: hi\r\n\r\nbody")
    result = await _run_data(handler, _Session("MailerApp01"), env)
    assert result.startswith("250")
    # The app never provided the credential; the router injected it.
    assert seen["cred"].smtp_relay_password == "hmac-pw"
    assert seen["from"] == "me@alice.example.com"
    assert seen["tos"] == ["dest@example.net"]


@pytest.mark.asyncio
async def test_handle_data_requires_auth(cfg: Any) -> None:
    handler = smtp_service.EmailServiceHandler(_enable_email(cfg))
    env = _Envelope("me@alice.example.com", ["d@example.net"], b"x")
    # No bound app_id on the session -> rejected.
    result = await _run_data(handler, _Session(None), env)
    assert result.startswith("530")


@pytest.mark.asyncio
async def test_handle_data_unconfigured_when_no_credential(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = smtp_service.EmailServiceHandler(_enable_email(cfg))
    monkeypatch.setattr(handler, "_fetch_credential", lambda: None)
    env = _Envelope("me@alice.example.com", ["d@example.net"], b"x")
    result = await _run_data(handler, _Session("MailerApp01"), env)
    assert result.startswith("451")


@pytest.mark.asyncio
async def test_handle_data_no_recipients(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = smtp_service.EmailServiceHandler(_enable_email(cfg))
    monkeypatch.setattr(handler, "_fetch_credential", lambda: _cred())
    env = _Envelope("me@alice.example.com", [], b"x")
    result = await _run_data(handler, _Session("MailerApp01"), env)
    assert result.startswith("554")


def test_fetch_credential_swallows_error(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = smtp_service.EmailServiceHandler(_enable_email(cfg))

    class _ErrProvider:
        def get(self, db: sqlite3.Connection) -> RelayCredential:
            raise RelayCredentialError("frontend down")

    monkeypatch.setattr(smtp_service, "get_relay_credential_provider", lambda config: _ErrProvider())
    assert handler._fetch_credential() is None
