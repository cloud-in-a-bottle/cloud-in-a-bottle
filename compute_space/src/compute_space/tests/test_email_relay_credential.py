"""Tests for core.email.relay_credential.RelayCredentialProvider.

``get(db)`` returns None when email isn't enabled; otherwise it fetches the relay
config from the frontend (bearer via the shared Imbue identity) and returns a
``RelayCredential``, cached in-process with a TTL keyed on the identity/zone/
custom-domain.  A fetch failure while email IS enabled raises
``RelayCredentialError``.

The in-memory ``db`` fixture (conftest) carries the real schema; these tests seed
a primary domain + Imbue identity into it.  The Keycloak token provider is faked
and ``httpx.Client`` is swapped for a MockTransport client, so no network is hit.
The TTL is driven by an injected ``monotonic`` clock.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import httpx
import pytest

from compute_space.config import DefaultConfig
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.core.email.relay_credential import _CACHE_TTL_SECONDS
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialError
from compute_space.core.email.relay_credential import RelayCredentialProvider
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials

_Handler = Callable[[httpx.Request], httpx.Response]
_REAL_HTTPX_CLIENT = httpx.Client
_ZONE = "alice.example.com"
_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.5"


def _cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


class _FakeTokenProvider:
    def __enter__(self) -> _FakeTokenProvider:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def get_token(self) -> str:
        return "fake-token"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    """Fake the token provider and route relay-config HTTP through a mock transport."""
    monkeypatch.setattr(
        "compute_space.core.email.relay_credential.KeycloakTokenProvider.create",
        classmethod(lambda cls, creds: _FakeTokenProvider()),
    )
    monkeypatch.setattr(
        "compute_space.core.email.relay_credential.httpx.Client",
        lambda *a, **k: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )


def _seed_enabled(db: sqlite3.Connection, custom_domain: str | None = None) -> DefaultConfig:
    seed_domains(db, Domain(name=_ZONE, tls=True), [])
    set_instance_identity(db, _cred())
    return DefaultConfig(email_proxy_base_url=_PROXY, public_ip=_IP, email_custom_domain=custom_domain)


def _relay_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "configured": True,
        "smtp_relay_host": "smtp.openhost.imbue.com",
        "smtp_relay_port": 465,
        "smtp_relay_user": _ZONE,
        "smtp_relay_password": "hmac-derived-pw",
        "zone_domain": _ZONE,
    }
    body.update(overrides)
    return body


# --- disabled ----------------------------------------------------------------


def test_get_none_when_email_disabled(db: sqlite3.Connection) -> None:
    # No identity/proxy/public_ip -> email off -> None (not an error).
    seed_domains(db, Domain(name=_ZONE, tls=True), [])
    provider = RelayCredentialProvider(config=DefaultConfig())
    assert provider.get(db) is None


def test_get_none_when_identity_missing(db: sqlite3.Connection) -> None:
    seed_domains(db, Domain(name=_ZONE, tls=True), [])
    provider = RelayCredentialProvider(config=DefaultConfig(email_proxy_base_url=_PROXY, public_ip=_IP))
    assert provider.get(db) is None


def test_get_none_does_not_fetch_when_disabled(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_domains(db, Domain(name=_ZONE, tls=True), [])
    called = {"fetch": False}

    def handler(req: httpx.Request) -> httpx.Response:
        called["fetch"] = True
        return httpx.Response(200, json=_relay_body())

    _install_transport(monkeypatch, handler)
    provider = RelayCredentialProvider(config=DefaultConfig())
    assert provider.get(db) is None
    assert called["fetch"] is False


# --- enabled: successful fetch -----------------------------------------------


def test_get_returns_credential_when_enabled(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json=_relay_body()))
    cred = RelayCredentialProvider(config=cfg).get(db)
    assert cred == RelayCredential(
        smtp_relay_host="smtp.openhost.imbue.com",
        smtp_relay_port=465,
        smtp_relay_user=_ZONE,
        smtp_relay_password="hmac-derived-pw",
    )


def test_get_sends_bearer_to_relay_config_endpoint(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_relay_body())

    _install_transport(monkeypatch, handler)
    RelayCredentialProvider(config=cfg).get(db)
    assert seen["path"] == "/api/email/relay-config"
    assert seen["auth"] == "Bearer fake-token"


def test_get_ignores_extra_response_fields(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential only needs the four SMTP-login fields; any extra keys the
    # frontend returns (e.g. zone_domain) are ignored and don't break parsing.
    cfg = _seed_enabled(db)
    body = _relay_body()
    body["zone_domain"] = "ignored.example.com"
    body["custom_domain"] = "also-ignored.example.com"
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json=body))
    cred = RelayCredentialProvider(config=cfg).get(db)
    assert cred is not None
    assert cred.smtp_relay_host == "smtp.openhost.imbue.com"
    assert not hasattr(cred, "zone_domain")


def test_get_coerces_port_to_int(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json=_relay_body(smtp_relay_port="587")))
    cred = RelayCredentialProvider(config=cfg).get(db)
    assert cred is not None
    assert cred.smtp_relay_port == 587


# --- TTL cache ---------------------------------------------------------------


def test_cache_reused_within_ttl(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_relay_body())

    _install_transport(monkeypatch, handler)
    clock = {"t": 1000.0}
    provider = RelayCredentialProvider(config=cfg, monotonic=lambda: clock["t"])
    first = provider.get(db)
    second = provider.get(db)
    assert first is second
    assert calls["n"] == 1


def test_cache_refetches_after_ttl_expiry(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_relay_body())

    _install_transport(monkeypatch, handler)
    clock = {"t": 1000.0}
    provider = RelayCredentialProvider(config=cfg, monotonic=lambda: clock["t"])
    provider.get(db)
    clock["t"] = 1000.0 + _CACHE_TTL_SECONDS + 1
    provider.get(db)
    assert calls["n"] == 2


def test_cache_refetches_when_identity_rotates(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_relay_body())

    _install_transport(monkeypatch, handler)
    clock = {"t": 1000.0}
    provider = RelayCredentialProvider(config=cfg, monotonic=lambda: clock["t"])
    provider.get(db)
    # Rotate the identity (still within TTL) -> cache key changes -> refetch.
    set_instance_identity(
        db,
        KeycloakClientCredentials(issuer_url="https://kc/realms/x", client_id="rotated", client_secret="new"),
    )
    provider.get(db)
    assert calls["n"] == 2


# --- error behavior (probed against the real implementation) -----------------


def test_http_error_status_raises_relay_credential_error(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(RelayCredentialError, match="HTTP 500"):
        RelayCredentialProvider(config=cfg).get(db)


def test_network_error_raises_relay_credential_error(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, handler)
    with pytest.raises(RelayCredentialError, match="relay-config fetch failed"):
        RelayCredentialProvider(config=cfg).get(db)


def test_not_configured_response_raises(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json={"configured": False}))
    with pytest.raises(RelayCredentialError, match="not configured"):
        RelayCredentialProvider(config=cfg).get(db)


def test_malformed_body_raises(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    # configured True but missing required fields -> malformed error.
    cfg = _seed_enabled(db)
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json={"configured": True}))
    with pytest.raises(RelayCredentialError, match="malformed"):
        RelayCredentialProvider(config=cfg).get(db)
