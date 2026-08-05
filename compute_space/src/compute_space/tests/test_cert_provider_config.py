"""Config tests for the additive, opt-in cert_api provider.

The BYO-ACME path must stay the default and old configs (written before these
fields existed) must keep loading unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig


def _full_cert_api_kwargs() -> dict[str, str]:
    return dict(
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-alice",
        cert_api_keycloak_client_secret="s3cr3t",
    )


def test_default_provider_is_acme() -> None:
    cfg = DefaultConfig()
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    # The broker URL defaults to a host but is only used by the cert_api
    # provider, so the default acme path is unaffected by it.
    # TODO: revert to "https://api.selfhost.imbue.com" once the broker is deployed.
    assert cfg.cert_api_base_url == "https://openhost-cert-api.openhost-qa.selfhost.imbue.com/"
    # Keycloak auth is injected per instance — no defaults.
    assert cfg.cert_api_keycloak_issuer_url is None
    assert cfg.cert_api_keycloak_client_id is None
    assert cfg.cert_api_keycloak_client_secret is None


def test_legacy_config_without_cert_fields_still_loads(tmp_path: Path) -> None:
    # A config as ansible wrote it before the cert fields existed (post-scrub: no zone_domain).
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        "acquire_tls_cert_if_missing = true\n"
        'acme_account_key_path = "/secrets/certbot_private_key.json"\n'
        "coredns_enabled = true\n"
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    assert cfg.acme_account_key_path == "/secrets/certbot_private_key.json"


def test_cert_api_provider_config_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        "acquire_tls_cert_if_missing = true\n"
        "coredns_enabled = true\n"
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://keycloak.example.com/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "instance-alice"\n'
        'cert_api_keycloak_client_secret = "s3cr3t"\n'
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API
    assert cfg.cert_api_base_url == "https://cert-api.example.com"
    assert cfg.cert_api_keycloak_issuer_url == "https://keycloak.example.com/realms/openhost-customers"
    assert cfg.cert_api_keycloak_client_id == "instance-alice"
    assert cfg.cert_api_keycloak_client_secret == "s3cr3t"


def test_cert_provider_round_trips_through_toml() -> None:
    cfg = DefaultConfig(
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-alice",
        cert_api_keycloak_client_secret="s3cr3t",
    )
    rendered = cfg.to_toml_str()
    assert 'cert_provider = "cert_api"' in rendered
    assert 'cert_api_base_url = "https://cert-api.example.com"' in rendered
    assert 'cert_api_keycloak_issuer_url = "https://keycloak.example.com/realms/openhost-customers"' in rendered
    assert 'cert_api_keycloak_client_id = "instance-alice"' in rendered
    assert 'cert_api_keycloak_client_secret = "s3cr3t"' in rendered


def test_unknown_cert_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown cert_provider"):
        DefaultConfig(cert_provider="bogus")


def test_complete_cert_api_config_is_valid() -> None:
    cfg = DefaultConfig(**_full_cert_api_kwargs())
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


def test_cert_api_provider_requires_base_url() -> None:
    # Only the broker URL is validated at construction; the per-instance credential
    # now lives in the DB settings table (the shared Imbue identity), so it can't be
    # checked here and is verified at cert-acquisition time.
    kwargs = _full_cert_api_kwargs()
    kwargs["cert_api_base_url"] = None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="cert_api_base_url must be set"):
        DefaultConfig(**kwargs)


@pytest.mark.parametrize(
    "missing_field",
    [
        "cert_api_keycloak_issuer_url",
        "cert_api_keycloak_client_id",
        "cert_api_keycloak_client_secret",
    ],
)
def test_cert_api_provider_no_longer_requires_keycloak_at_construction(missing_field: str) -> None:
    # The cert_api_keycloak_* fields are a deprecated fallback; their absence is not
    # a construction error (the credential can come from the settings table instead).
    kwargs = _full_cert_api_kwargs()
    kwargs[missing_field] = None  # type: ignore[assignment]
    cfg = DefaultConfig(**kwargs)
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


def test_acme_provider_ignores_cert_api_settings() -> None:
    # The default acme path must not require any cert_api settings, even though
    # cert_api_keycloak_* default to None.
    cfg = DefaultConfig()
    assert cfg.cert_provider == CERT_PROVIDER_ACME
