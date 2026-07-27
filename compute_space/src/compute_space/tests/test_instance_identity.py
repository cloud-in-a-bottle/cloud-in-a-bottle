"""Tests for the shared per-instance Imbue credential (imbue_identity_*).

Every Imbue service (cert-api, email, ...) authenticates with one per-instance
credential. It resolves from a service's DEPRECATED per-service override first
(kept for already-deployed configs) then falls back to the shared imbue_identity_*
fields. These tests pin the resolution precedence, the instance_identity accessor,
TOML round-tripping, and backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig


def _shared_identity_kwargs() -> dict[str, str]:
    return dict(
        imbue_identity_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        imbue_identity_client_id="instance-alice",
        imbue_identity_client_secret="shared-s3cr3t",
    )


# --- defaults / backward compat -------------------------------------------------


def test_shared_identity_defaults_to_none() -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com")
    assert cfg.imbue_identity_issuer_url is None
    assert cfg.imbue_identity_client_id is None
    assert cfg.imbue_identity_client_secret is None
    # No identity configured -> accessor returns None (not a partial object).
    assert cfg.instance_identity is None


def test_legacy_cert_api_config_without_shared_identity_still_loads(tmp_path: Path) -> None:
    # A config exactly as ansible wrote it before imbue_identity_* existed.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "tls_enabled = true\n"
        "coredns_enabled = true\n"
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.example.com/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "instance-alice"\n'
        'cert_api_keycloak_client_secret = "legacy-s3cr3t"\n'
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    # cert-api resolves from its own (deprecated) override when the shared field is absent.
    assert cfg.cert_api_keycloak_client_secret_resolved == "legacy-s3cr3t"
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "instance-alice"
    assert ident.client_secret == "legacy-s3cr3t"


# --- resolution precedence ------------------------------------------------------


def test_cert_api_resolves_from_shared_identity() -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **_shared_identity_kwargs())
    assert cfg.cert_api_keycloak_issuer_url_resolved == "https://keycloak.example.com/realms/openhost-customers"
    assert cfg.cert_api_keycloak_client_id_resolved == "instance-alice"
    assert cfg.cert_api_keycloak_client_secret_resolved == "shared-s3cr3t"


def test_cert_api_override_takes_precedence_over_shared() -> None:
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_api_keycloak_issuer_url="https://kc.override.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-override",
        cert_api_keycloak_client_secret="override-s3cr3t",
        **_shared_identity_kwargs(),
    )
    assert cfg.cert_api_keycloak_client_id_resolved == "instance-override"
    assert cfg.cert_api_keycloak_client_secret_resolved == "override-s3cr3t"


def test_email_resolves_from_shared_identity() -> None:
    # Email inherits cert-api which inherits the shared identity: one credential
    # satisfies both services.
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **_shared_identity_kwargs())
    assert cfg.email_keycloak_issuer_url_resolved == "https://keycloak.example.com/realms/openhost-customers"
    assert cfg.email_keycloak_client_id_resolved == "instance-alice"
    assert cfg.email_keycloak_client_secret_resolved == "shared-s3cr3t"


def test_email_override_takes_precedence_over_shared() -> None:
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        email_keycloak_issuer_url="https://kc.email.com/realms/openhost-customers",
        email_keycloak_client_id="instance-email",
        email_keycloak_client_secret="email-s3cr3t",
        **_shared_identity_kwargs(),
    )
    assert cfg.email_keycloak_client_id_resolved == "instance-email"
    assert cfg.email_keycloak_client_secret_resolved == "email-s3cr3t"


def test_email_inherits_cert_api_override_over_shared() -> None:
    # cert-api override is more specific than the shared identity, so email (which
    # inherits cert-api) picks up the cert-api override, not the shared value.
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_api_keycloak_issuer_url="https://kc.cert.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-cert",
        cert_api_keycloak_client_secret="cert-s3cr3t",
        **_shared_identity_kwargs(),
    )
    assert cfg.email_keycloak_client_id_resolved == "instance-cert"
    assert cfg.email_keycloak_client_secret_resolved == "cert-s3cr3t"


# --- instance_identity accessor -------------------------------------------------


def test_instance_identity_builds_credentials() -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **_shared_identity_kwargs())
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.issuer_url == "https://keycloak.example.com/realms/openhost-customers"
    assert ident.client_id == "instance-alice"
    assert ident.client_secret == "shared-s3cr3t"
    # token_endpoint is derived from the issuer.
    assert ident.token_endpoint == (
        "https://keycloak.example.com/realms/openhost-customers/protocol/openid-connect/token"
    )


@pytest.mark.parametrize("drop", ["issuer", "client_id", "client_secret"])
def test_instance_identity_is_none_when_partial(drop: str) -> None:
    kwargs = _shared_identity_kwargs()
    field = {
        "issuer": "imbue_identity_issuer_url",
        "client_id": "imbue_identity_client_id",
        "client_secret": "imbue_identity_client_secret",
    }[drop]
    kwargs[field] = ""  # type: ignore[assignment]
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **kwargs)
    assert cfg.instance_identity is None


# --- cert_api provider validation accepts the shared identity -------------------


def test_cert_api_provider_satisfied_by_shared_identity() -> None:
    # cert_provider=cert_api with NO cert_api_keycloak_* override, only the shared
    # identity, must validate (the credential resolves from imbue_identity_*).
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        **_shared_identity_kwargs(),
    )
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API
    assert cfg.instance_identity is not None


def test_cert_api_provider_missing_all_credentials_still_errors() -> None:
    with pytest.raises(ValueError, match="cert_api_keycloak_issuer_url must be set"):
        DefaultConfig(
            zone_domain="alice.host.example.com",
            cert_provider=CERT_PROVIDER_CERT_API,
            cert_api_base_url="https://cert-api.example.com",
        )


# --- email enablement via shared identity ---------------------------------------


def test_email_enabled_via_shared_identity_only() -> None:
    # A shared-identity instance enables email by setting only email_proxy_base_url
    # (plus public_ip for the direct-inbound records) — no email_keycloak_* needed.
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        email_proxy_base_url="https://openhost.imbue.com",
        public_ip="203.0.113.10",
        **_shared_identity_kwargs(),
    )
    assert cfg.email_enabled is True


def test_email_not_enabled_without_any_identity() -> None:
    # proxy URL set but no credential resolvable anywhere -> config error (email
    # could never authenticate).
    with pytest.raises(ValueError, match="email cannot be enabled"):
        DefaultConfig(
            zone_domain="alice.host.example.com",
            email_proxy_base_url="https://openhost.imbue.com",
            public_ip="203.0.113.10",
        )


# --- TOML round-trip -------------------------------------------------------------


def test_shared_identity_round_trips_through_toml(tmp_path: Path) -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **_shared_identity_kwargs())
    rendered = cfg.to_toml_str()
    assert 'imbue_identity_issuer_url = "https://keycloak.example.com/realms/openhost-customers"' in rendered
    assert 'imbue_identity_client_id = "instance-alice"' in rendered
    assert 'imbue_identity_client_secret = "shared-s3cr3t"' in rendered
    config_path = tmp_path / "config.toml"
    config_path.write_text(rendered)
    reloaded = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity.client_secret == "shared-s3cr3t"


def test_acme_default_unaffected_by_shared_identity() -> None:
    # The default acme path never requires any identity, even with shared fields unset.
    cfg = DefaultConfig(zone_domain="alice.host.example.com")
    assert cfg.cert_provider == CERT_PROVIDER_ACME
