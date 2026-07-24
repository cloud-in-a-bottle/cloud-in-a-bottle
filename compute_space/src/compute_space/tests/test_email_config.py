"""Config tests for the additive, opt-in email feature.

Email is disabled by default; enabling it requires the proxy URL, per-instance
Keycloak client-credentials, and the inbound MX host. Old configs (written
before these fields existed) must keep loading unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings
from litestar.exceptions import NotAuthorizedException

import compute_space.web.routes.api.system as sys_mod
from compute_space.config import DefaultConfig
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.web.routes.api.system import custom_email_domain


def _full_email_kwargs() -> dict[str, object]:
    return dict(
        email_enabled=True,
        email_proxy_base_url="https://openhost-email-proxy.fly.dev",
        email_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        # Inbound is always direct-to-instance; the MX/A records need the instance IP.
        public_ip="203.0.113.5",
    )


def test_email_disabled_by_default() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_enabled is False
    assert cfg.email_proxy_base_url is None
    assert cfg.email_keycloak_client_secret is None


def test_email_enabled_requires_all_fields() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    with pytest.raises(ValueError, match="email_proxy_base_url must be set"):
        cfg.evolve(email_enabled=True)


def test_email_enabled_requires_public_ip() -> None:
    # Inbound is always direct-to-instance, so the public IP (the MX/A target)
    # must be known when email is enabled.
    cfg = DefaultConfig(zone_domain="x.example.com")
    partial = {k: v for k, v in _full_email_kwargs().items() if k != "public_ip"}
    with pytest.raises(ValueError, match="public_ip must be set"):
        cfg.evolve(**partial)


def test_email_config_has_no_inbound_mode_fields() -> None:
    # The ses inbound mode has been removed entirely: inbound is always direct.
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert not hasattr(cfg, "email_inbound_mode")
    assert not hasattr(cfg, "email_inbound_mx_host")


def test_effective_default_apps_excludes_email_apps_when_off() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com", default_apps=["oauth_provider"])
    assert cfg.email_enabled is False
    assert cfg.effective_default_apps == ["oauth_provider"]


def test_effective_default_apps_appends_email_apps_when_on() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com", default_apps=["oauth_provider"]).evolve(**_full_email_kwargs())
    apps = cfg.effective_default_apps
    assert apps[0] == "oauth_provider"
    assert "https://github.com/imbue-openhost/openhost-stalwart-email-server" in apps
    assert "https://github.com/imbue-openhost/openhost-bulwark-email-client" in apps


def test_effective_default_apps_dedupes_when_already_listed() -> None:
    stalwart = "https://github.com/imbue-openhost/openhost-stalwart-email-server"
    cfg = DefaultConfig(zone_domain="x.example.com", default_apps=[stalwart]).evolve(**_full_email_kwargs())
    apps = cfg.effective_default_apps
    assert apps.count(stalwart) == 1


def test_email_enabled_with_all_fields_ok() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    assert cfg.email_enabled is True
    assert cfg.public_ip == "203.0.113.5"


def test_legacy_config_without_email_fields_still_loads(tmp_path: Path) -> None:
    # A config exactly as ansible wrote it before the email feature existed.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "legacy.example.com"\n'
        "tls_enabled = true\n"
        "acquire_tls_cert_if_missing = true\n"
        'acme_account_key_path = "/secrets/certbot_private_key.json"\n'
        "coredns_enabled = true\n"
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.email_enabled is False


def test_email_config_round_trips_through_toml() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    rendered = cfg.to_toml_str()
    assert "email_enabled = true" in rendered
    assert 'email_proxy_base_url = "https://openhost-email-proxy.fly.dev"' in rendered
    assert 'email_keycloak_client_id = "instance-alice"' in rendered
    # The removed ses-inbound fields must not round-trip.
    assert "email_inbound_mode" not in rendered
    assert "email_inbound_mx_host" not in rendered


def test_email_config_has_no_baked_in_relay_secret() -> None:
    # The relay host/port/user/password are fetched at runtime from the frontend,
    # never rendered into the instance config.
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    rendered = cfg.to_toml_str()
    assert "email_smtp_relay_password" not in rendered
    assert "email_smtp_relay_host" not in rendered


def test_custom_domain_none_by_default() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_custom_domain is None
    assert cfg.email_custom_domain_normalized is None
    assert cfg.custom_domain_delegation_record() is None


def test_custom_domain_normalized_lowercases_and_strips_dot() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="Mail.MyDomain.Com.")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_blank_treated_as_unset() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="   ")
    assert cfg.email_custom_domain_normalized is None


def test_custom_domain_delegation_record() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com:8443").evolve(email_custom_domain="mail.mydomain.com")
    rec = cfg.custom_domain_delegation_record()
    assert rec is not None
    assert rec.name == "mail.mydomain.com"
    assert rec.record_type == "NS"
    assert rec.value == "ns.alice.selfhost.imbue.com"
    assert rec.as_display_line() == "mail.mydomain.com   NS   ns.alice.selfhost.imbue.com"


def test_custom_domain_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="not a domain")


def test_custom_domain_rejects_overlap_with_zone() -> None:
    # The built-in zone already handles its own name and subdomains; a custom
    # domain that overlaps would double-declare records.
    with pytest.raises(ValueError, match="overlaps the instance zone"):
        DefaultConfig(zone_domain="alice.example.com").evolve(email_custom_domain="mail.alice.example.com")
    with pytest.raises(ValueError, match="overlaps the instance zone"):
        DefaultConfig(zone_domain="alice.example.com").evolve(email_custom_domain="alice.example.com")


def test_custom_domain_validated_even_when_email_disabled() -> None:
    # A typo should surface at config load, not silently wait until email is on.
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(zone_domain="x.example.com").evolve(email_custom_domain="bad_domain!")


def test_custom_domain_round_trips_through_toml() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(
        **_full_email_kwargs(),
        email_custom_domain="mail.mydomain.com",
    )
    assert 'email_custom_domain = "mail.mydomain.com"' in cfg.to_toml_str()


def test_custom_email_domain_route_returns_record_when_set() -> None:
    # The owner-facing route surfaces the exact NS record to paste at the registrar.

    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(email_custom_domain="mail.mydomain.com")
    resp = custom_email_domain.fn(cfg)  # type: ignore[attr-defined]
    assert resp.configured is True
    assert resp.domain == "mail.mydomain.com"
    assert resp.record_name == "mail.mydomain.com"
    assert resp.record_type == "NS"
    assert resp.record_value == "ns.alice.selfhost.imbue.com"
    assert resp.display_line == "mail.mydomain.com   NS   ns.alice.selfhost.imbue.com"


def test_custom_email_domain_route_reports_unconfigured() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")  # no custom domain
    resp = custom_email_domain.fn(cfg)  # type: ignore[attr-defined]
    assert resp.configured is False
    assert resp.domain is None
    assert resp.display_line is None


def test_mailbox_app_names_default() -> None:
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")
    assert cfg.email_mailbox_app_names == ["stalwart-email-server"]


class _FakeDB:
    def __init__(self, app_name: str | None) -> None:
        self._app_name = app_name

    def execute(self, *_args: object) -> _FakeDB:
        return self

    def fetchone(self) -> dict[str, str] | None:
        return {"name": self._app_name} if self._app_name is not None else None


def test_relay_config_rejects_non_mailbox_app(monkeypatch) -> None:

    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-123")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_full_email_kwargs())
    # A different app (not in email_mailbox_app_names) must be refused.
    with pytest.raises(NotAuthorizedException):
        sys_mod.email_relay_config.fn(object(), _FakeDB("some-other-app"), cfg)  # type: ignore[attr-defined]


def test_relay_config_returns_creds_to_mailbox_app(monkeypatch) -> None:
    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-mail")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_full_email_kwargs(),
        email_custom_domain="mail.mydomain.com",
    )
    # The router fetches the credential at runtime from the frontend; stub the
    # provider so we don't need a live frontend.
    cred = RelayCredential(
        smtp_relay_host="openhost-email-proxy.fly.dev",
        smtp_relay_port=465,
        smtp_relay_user="alice.selfhost.imbue.com",
        smtp_relay_password="hmac-pw",
        zone_domain="alice.selfhost.imbue.com",
        custom_domain="mail.mydomain.com",
    )

    class _StubProvider:
        def get(self) -> RelayCredential:
            return cred

    monkeypatch.setattr(sys_mod, "get_relay_credential_provider", lambda config: _StubProvider())
    resp = sys_mod.email_relay_config.fn(object(), _FakeDB("stalwart-email-server"), cfg)  # type: ignore[attr-defined]
    body = resp.content
    assert body.configured is True
    assert body.smtp_relay_host == "openhost-email-proxy.fly.dev"
    assert body.smtp_relay_password == "hmac-pw"
    assert body.zone_domain == "alice.selfhost.imbue.com"
    assert body.custom_domain == "mail.mydomain.com"


def test_relay_config_reports_unconfigured_when_email_off(monkeypatch) -> None:
    monkeypatch.setattr(sys_mod, "verify_app_auth", lambda request: "app-mail")
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")  # email disabled
    resp = sys_mod.email_relay_config.fn(object(), _FakeDB("stalwart-email-server"), cfg)  # type: ignore[attr-defined]
    assert resp.content.configured is False
    assert resp.content.smtp_relay_password is None


def test_relay_provider_cache_reused_across_equivalent_configs(monkeypatch) -> None:
    # provide_config() re-wraps the active config into a fresh Config per request,
    # so two distinct-but-equivalent Config objects must still share one provider
    # (otherwise the TTL cache never hits and the dict leaks a provider per call).
    monkeypatch.setattr(sys_mod, "_relay_providers", {})
    cfg1 = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_full_email_kwargs())
    cfg2 = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_full_email_kwargs())
    assert cfg1 is not cfg2
    p1 = sys_mod.get_relay_credential_provider(cfg1)
    p2 = sys_mod.get_relay_credential_provider(cfg2)
    assert p1 is p2
    assert len(sys_mod._relay_providers) == 1


def test_relay_provider_cache_separates_distinct_configs(monkeypatch) -> None:
    # Different zones (or email identities) must not share a cached credential.
    monkeypatch.setattr(sys_mod, "_relay_providers", {})
    cfg_a = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_full_email_kwargs())
    cfg_b = DefaultConfig(zone_domain="bob.selfhost.imbue.com").evolve(**_full_email_kwargs())
    p_a = sys_mod.get_relay_credential_provider(cfg_a)
    p_b = sys_mod.get_relay_credential_provider(cfg_b)
    assert p_a is not p_b
    assert len(sys_mod._relay_providers) == 2
