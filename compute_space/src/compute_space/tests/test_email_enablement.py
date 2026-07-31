"""Tests for core.email.enablement (DB-based email enablement + identity).

``email_enabled(config, db)`` is True iff three prerequisites are all present:
the proxy URL (Config), the shared per-instance Imbue identity (DB settings table,
with the deprecated cert_api_keycloak_* config fallback), and the public IP
(Config).  ``resolve_email_identity`` is a thin wrapper over
``get_instance_identity``.

The ``db`` fixture (conftest) is an in-memory DB carrying the real schema, so the
settings + domains tables exist.  These tests seed the identity via
``set_instance_identity`` and toggle the Config prerequisites to assert the
enablement truth table.
"""

from __future__ import annotations

import sqlite3

import pytest

from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig
from compute_space.core.email.enablement import email_enabled
from compute_space.core.email.enablement import resolve_email_identity
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials

_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.5"


def _cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


def _enabled_config() -> DefaultConfig:
    return DefaultConfig(email_proxy_base_url=_PROXY, public_ip=_IP)


# --- all prerequisites present -> enabled ------------------------------------


def test_enabled_when_all_prereqs_present(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    assert email_enabled(_enabled_config(), db) is True


# --- each prerequisite missing -> disabled -----------------------------------


def test_disabled_when_proxy_url_missing(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    cfg = DefaultConfig(email_proxy_base_url=None, public_ip=_IP)
    assert email_enabled(cfg, db) is False


def test_disabled_when_public_ip_missing(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    cfg = DefaultConfig(email_proxy_base_url=_PROXY, public_ip=None)
    assert email_enabled(cfg, db) is False


def test_disabled_when_identity_missing(db: sqlite3.Connection) -> None:
    # No identity seeded and no config fallback.
    assert email_enabled(_enabled_config(), db) is False


def test_disabled_when_only_proxy_present(db: sqlite3.Connection) -> None:
    cfg = DefaultConfig(email_proxy_base_url=_PROXY)
    assert email_enabled(cfg, db) is False


def test_disabled_when_nothing_present(db: sqlite3.Connection) -> None:
    assert email_enabled(DefaultConfig(), db) is False


@pytest.mark.parametrize("drop", ["proxy", "identity", "public_ip"])
def test_disabled_when_any_single_prereq_dropped(db: sqlite3.Connection, drop: str) -> None:
    if drop != "identity":
        set_instance_identity(db, _cred())
    cfg = DefaultConfig(
        email_proxy_base_url=None if drop == "proxy" else _PROXY,
        public_ip=None if drop == "public_ip" else _IP,
    )
    assert email_enabled(cfg, db) is False


def test_empty_string_proxy_is_disabled(db: sqlite3.Connection) -> None:
    # An empty proxy URL is falsy -> email off (never "" truthy).
    set_instance_identity(db, _cred())
    cfg = DefaultConfig(email_proxy_base_url="", public_ip=_IP)
    assert email_enabled(cfg, db) is False


def test_empty_string_public_ip_is_disabled(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    cfg = DefaultConfig(email_proxy_base_url=_PROXY, public_ip="")
    assert email_enabled(cfg, db) is False


def test_email_enabled_returns_bool_not_truthy(db: sqlite3.Connection) -> None:
    # email_enabled wraps its result in bool(); confirm the concrete type.
    set_instance_identity(db, _cred())
    result = email_enabled(_enabled_config(), db)
    assert result is True
    assert isinstance(result, bool)


# --- identity source: settings table vs. cert_api config fallback ------------


def test_identity_from_settings_table_enables(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    cfg = _enabled_config()
    assert email_enabled(cfg, db) is True
    assert resolve_email_identity(cfg, db) == _cred()


def test_identity_from_cert_api_config_fallback_enables(db: sqlite3.Connection) -> None:
    # No settings-table identity, but a pre-shared-identity instance carries the
    # deprecated cert_api_keycloak_* fields in config; those satisfy enablement.
    cfg = DefaultConfig(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://kc/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-fallback",
        cert_api_keycloak_client_secret="fallback-secret",
    )
    assert email_enabled(cfg, db) is True
    cred = resolve_email_identity(cfg, db)
    assert cred is not None
    assert cred.client_id == "instance-fallback"


def test_settings_identity_takes_precedence_over_config_fallback(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    cfg = DefaultConfig(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://other/realms/x",
        cert_api_keycloak_client_id="instance-config",
        cert_api_keycloak_client_secret="config-secret",
    )
    cred = resolve_email_identity(cfg, db)
    assert cred is not None
    # Settings table wins.
    assert cred.client_id == "instance-alice"


def test_partial_cert_api_fallback_does_not_enable(db: sqlite3.Connection) -> None:
    # Only two of three cert_api_keycloak_* fields set -> no resolvable identity.
    cfg = DefaultConfig(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        cert_api_keycloak_issuer_url="https://kc/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-x",
        cert_api_keycloak_client_secret=None,
    )
    assert email_enabled(cfg, db) is False


# --- resolve_email_identity --------------------------------------------------


def test_resolve_identity_returns_credentials(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    assert resolve_email_identity(_enabled_config(), db) == _cred()


def test_resolve_identity_none_when_unset(db: sqlite3.Connection) -> None:
    assert resolve_email_identity(_enabled_config(), db) is None


def test_resolve_identity_ignores_proxy_and_public_ip(db: sqlite3.Connection) -> None:
    # resolve_email_identity is purely about the credential; the proxy/public_ip
    # prerequisites don't affect it (only email_enabled combines all three).
    set_instance_identity(db, _cred())
    cfg = DefaultConfig()  # no proxy, no public_ip
    assert resolve_email_identity(cfg, db) == _cred()


def test_resolve_identity_reflects_updated_credential(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    updated = KeycloakClientCredentials(
        issuer_url="https://kc2/realms/openhost-customers",
        client_id="instance-rotated",
        client_secret="rotated",
    )
    set_instance_identity(db, updated)
    assert resolve_email_identity(_enabled_config(), db) == updated
