"""Config tests for the additive, opt-in email feature.

Email is disabled by default; enabling it requires the proxy URL, the
per-instance Keycloak client-credentials, and the instance's public IP (inbound
is always delivered directly to the instance, so the MX/A records point at it).
Old configs (written before these fields existed, or carrying the removed
email_inbound_* keys) must keep loading unchanged.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import typed_settings
from litestar.exceptions import NotAuthorizedException

import compute_space.config as config_mod
import compute_space.web.routes.api.system as sys_mod
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig
from compute_space.config import load_config
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.web.routes.api.system import custom_email_domain


def _full_email_kwargs() -> dict[str, object]:
    return dict(
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


def test_email_enabled_is_derived_from_prereqs() -> None:
    # No on/off flag: all prereqs present -> on.
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    assert cfg.email_enabled is True
    # There is no settable email_enabled field.
    with pytest.raises((TypeError, AttributeError)):
        cfg.evolve(email_enabled=True)


def test_partial_email_keycloak_config_is_rejected() -> None:
    # Setting SOME but not all of the explicit email_keycloak_* override fields is
    # a likely typo (set all three to override cert-api, or none to inherit it).
    cfg = DefaultConfig(zone_domain="x.example.com")
    for missing in ("email_keycloak_issuer_url", "email_keycloak_client_id", "email_keycloak_client_secret"):
        partial = {k: v for k, v in _full_email_kwargs().items() if k != missing}
        with pytest.raises(ValueError, match="partially configured"):
            cfg.evolve(**partial)


def test_email_proxy_without_keycloak_creds_is_rejected() -> None:
    # email_proxy_base_url set (email intended) but no resolvable Keycloak client
    # (no cert-api, no explicit override) -> email could never authenticate.
    cfg = DefaultConfig(zone_domain="x.example.com", public_ip="203.0.113.5")
    with pytest.raises(ValueError, match="email cannot be enabled"):
        cfg.evolve(email_proxy_base_url="https://openhost.imbue.com")


def test_full_email_config_without_public_ip_rejected() -> None:
    # When all email_* fields are set (email will be on), public_ip is required
    # for the direct-inbound MX/A records.
    cfg = DefaultConfig(zone_domain="x.example.com")
    partial = {k: v for k, v in _full_email_kwargs().items() if k != "public_ip"}
    with pytest.raises(ValueError, match="public_ip must be set"):
        cfg.evolve(**partial)


def test_email_off_when_no_prereqs() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_enabled is False


def _cert_api_kwargs() -> dict[str, object]:
    """An instance provisioned with cert-api (holds a per-instance Keycloak client)
    but no email fields — the shape of an existing pre-email instance."""
    return dict(
        coredns_enabled=True,
        public_ip="203.0.113.5",
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
        cert_api_keycloak_issuer_url="https://keycloak.example.com/realms/openhost-customers",
        cert_api_keycloak_client_id="instance-alice",
        cert_api_keycloak_client_secret="cert-secret",
    )


def test_seamless_upgrade_email_inherits_cert_api_keycloak() -> None:
    # THE seamless upgrade: an existing cert-api instance enables email by setting
    # ONLY email_proxy_base_url; the Keycloak client-credentials inherit from
    # cert-api. No new credentials to inject.
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    assert existing.email_enabled is False  # no email URL yet
    upgraded = existing.evolve(email_proxy_base_url="https://openhost.imbue.com")
    assert upgraded.email_enabled is True
    assert upgraded.email_keycloak_issuer_url_resolved == "https://keycloak.example.com/realms/openhost-customers"
    assert upgraded.email_keycloak_client_id_resolved == "instance-alice"
    assert upgraded.email_keycloak_client_secret_resolved == "cert-secret"


def test_explicit_email_keycloak_overrides_cert_api() -> None:
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    cfg = existing.evolve(
        email_proxy_base_url="https://openhost.imbue.com",
        email_keycloak_issuer_url="https://kc2.example/realms/other",
        email_keycloak_client_id="email-client",
        email_keycloak_client_secret="email-secret",
    )
    assert cfg.email_enabled is True
    assert cfg.email_keycloak_issuer_url_resolved == "https://kc2.example/realms/other"
    assert cfg.email_keycloak_client_id_resolved == "email-client"


def test_email_keycloak_resolvers_none_without_any_client() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com")
    assert cfg.email_keycloak_issuer_url_resolved is None
    assert cfg.email_keycloak_client_id_resolved is None
    assert cfg.email_keycloak_client_secret_resolved is None


def test_upgrade_without_public_ip_rejected() -> None:
    # A cert-api instance somehow missing public_ip that turns on email is rejected.
    kw = {k: v for k, v in _cert_api_kwargs().items() if k != "public_ip"}
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)
    with pytest.raises(ValueError, match="public_ip must be set"):
        existing.evolve(email_proxy_base_url="https://openhost.imbue.com")


# ── extensive seamless-upgrade / resolver edge cases ──


def test_partial_kc_override_issuer_only() -> None:
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    with pytest.raises(ValueError, match="partially configured"):
        existing.evolve(email_proxy_base_url="https://f", email_keycloak_issuer_url="https://kc/realms/x")


def test_partial_kc_override_two_of_three() -> None:
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    with pytest.raises(ValueError, match="partially configured"):
        existing.evolve(
            email_proxy_base_url="https://f",
            email_keycloak_issuer_url="https://kc/realms/x",
            email_keycloak_client_id="c",
        )


def test_empty_string_kc_override_is_partial() -> None:
    existing = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    with pytest.raises(ValueError, match="partially configured"):
        existing.evolve(
            email_proxy_base_url="https://f",
            email_keycloak_issuer_url="https://kc/realms/x",
            email_keycloak_client_id="c",
            email_keycloak_client_secret="",
        )


def test_override_partial_rejected_even_without_proxy() -> None:
    # Partial kc override is a typo regardless of whether email is being turned on.
    cfg = DefaultConfig(zone_domain="x.example.com")
    with pytest.raises(ValueError, match="partially configured"):
        cfg.evolve(email_keycloak_issuer_url="https://kc/realms/x", email_keycloak_client_id="c")


def test_cert_api_client_present_but_email_off_without_proxy() -> None:
    # Having the cert-api client is not enough; email stays off until proxy URL set.
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_cert_api_kwargs())
    assert cfg.email_enabled is False
    # ...but the resolvers still expose the cert-api client for when it's turned on.
    assert cfg.email_keycloak_client_id_resolved == "instance-alice"


def test_acme_instance_cannot_enable_email_without_explicit_kc() -> None:
    # A BYO-ACME instance has no cert-api client, so proxy-only can't enable email.
    cfg = DefaultConfig(zone_domain="x.example.com", public_ip="203.0.113.5")
    with pytest.raises(ValueError, match="email cannot be enabled"):
        cfg.evolve(email_proxy_base_url="https://openhost.imbue.com")


def test_acme_instance_enables_email_with_explicit_kc() -> None:
    # A BYO-ACME instance can still enable email by supplying explicit email kc.
    cfg = DefaultConfig(zone_domain="x.example.com", public_ip="203.0.113.5").evolve(
        email_proxy_base_url="https://openhost.imbue.com",
        email_keycloak_issuer_url="https://kc/realms/openhost-customers",
        email_keycloak_client_id="email-only-client",
        email_keycloak_client_secret="es",
    )
    assert cfg.email_enabled is True
    assert cfg.email_keycloak_client_id_resolved == "email-only-client"


def test_upgraded_config_round_trips(tmp_path) -> None:
    # An upgraded config (cert-api + email_proxy_base_url only) survives a TOML
    # round trip and stays email-enabled, with kc still inherited.
    upgraded = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_cert_api_kwargs(), email_proxy_base_url="https://openhost.imbue.com"
    )
    out = tmp_path / "config.toml"
    out.write_text(upgraded.to_toml_str())
    reloaded = DefaultConfig.from_toml(str(out))
    assert reloaded.email_enabled is True
    assert reloaded.email_keycloak_client_id_resolved == "instance-alice"
    # email_keycloak_* were never stored (inherited), so they shouldn't be in TOML.
    rendered = upgraded.to_toml_str()
    assert "email_keycloak_client_id" not in rendered
    assert 'email_proxy_base_url = "https://openhost.imbue.com"' in rendered


def test_explicit_kc_override_is_rendered() -> None:
    cfg = DefaultConfig(zone_domain="x.example.com", public_ip="203.0.113.5").evolve(
        email_proxy_base_url="https://f",
        email_keycloak_issuer_url="https://kc/realms/x",
        email_keycloak_client_id="c",
        email_keycloak_client_secret="s",
    )
    rendered = cfg.to_toml_str()
    assert 'email_keycloak_client_id = "c"' in rendered


def test_coredns_instance_without_email_loads_with_public_ip() -> None:
    # CRITICAL: public_ip is a general CoreDNS field present on essentially every
    # instance. An instance with public_ip set but NO email fields must load fine
    # with email off — public_ip alone must not look like a "partial email config".
    cfg = DefaultConfig(zone_domain="x.example.com", coredns_enabled=True, public_ip="203.0.113.9")
    assert cfg.email_enabled is False
    assert cfg.public_ip == "203.0.113.9"


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
        # ansible renders public_ip into every config; a non-email instance still
        # has it, and that must not look like a partial email config.
        'public_ip = "203.0.113.9"\n'
    )
    cfg = typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(config_path)])
    assert cfg.email_enabled is False


def test_config_with_removed_email_fields_still_loads(tmp_path, monkeypatch) -> None:
    # Upgrade path: an email-enabled config.toml written by a previous template
    # (with the now-removed email_enabled / email_inbound_mode / email_inbound_mx_host)
    # must still load, or a code-only redeploy would break the router (ansible
    # doesn't re-render config.toml unless forced). The obsolete keys are dropped
    # on load; email is then re-derived from the surviving prerequisites.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.selfhost.imbue.com"\n'
        "tls_enabled = true\n"
        "coredns_enabled = true\n"
        'public_ip = "203.0.113.5"\n'
        "email_enabled = true\n"
        'email_proxy_base_url = "https://frontend.example"\n'
        'email_keycloak_issuer_url = "https://kc.example/realms/openhost-customers"\n'
        'email_keycloak_client_id = "instance-x"\n'
        'email_keycloak_client_secret = "secret"\n'
        'email_inbound_mode = "direct"\n'
        'email_inbound_mx_host = "inbound-smtp.us-west-2.amazonaws.com"\n'
    )
    # Exercise the production load path (load_config -> typed_settings). Force
    # the scratch temp file into tmp_path by pointing tempfile at it directly
    # (setting $TMPDIR would not work: tempfile.gettempdir() caches its result).
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    before = set(tmp_path.glob("openhost-config-*.toml"))
    cfg = load_config()
    assert cfg.email_enabled is True
    assert cfg.public_ip == "203.0.113.5"
    assert not hasattr(cfg, "email_inbound_mode")
    # The scrubbed temp copy (which carries secrets) must not linger.
    assert set(tmp_path.glob("openhost-config-*.toml")) == before

    # And the from_toml path used by non-DI callers.
    cfg2 = DefaultConfig.from_toml(str(config_path))
    assert cfg2.email_enabled is True


def test_scrub_obsolete_keys_cleans_up_on_write_failure(tmp_path, monkeypatch) -> None:
    # If writing the scrubbed temp copy fails, it must not leave a (possibly
    # secret-bearing) file behind.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.selfhost.imbue.com"\n'
        'email_inbound_mode = "direct"\n'  # an obsolete key -> triggers the temp write
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def _boom(_data, _f):
        raise RuntimeError("disk full")

    monkeypatch.setattr(config_mod.tomli_w, "dump", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        config_mod._scrub_obsolete_keys_to_temp(str(config_path))
    # No temp file left behind.
    assert list(tmp_path.glob("openhost-config-*.toml")) == []


def test_email_config_round_trips_through_toml(tmp_path: Path) -> None:
    cfg = DefaultConfig(zone_domain="x.example.com").evolve(**_full_email_kwargs())
    rendered = cfg.to_toml_str()
    # email_enabled is a derived property, not a stored field, so it is NOT
    # rendered; the prerequisites that imply it are.
    assert "email_enabled" not in rendered
    assert 'email_proxy_base_url = "https://openhost-email-proxy.fly.dev"' in rendered
    assert 'email_keycloak_client_id = "instance-alice"' in rendered
    # The removed ses-inbound fields must not round-trip.
    assert "email_inbound_mode" not in rendered
    assert "email_inbound_mx_host" not in rendered
    # Re-load the rendered TOML and confirm email is still derived as enabled
    # (the prerequisites survive the round trip even though the flag isn't stored).
    out = tmp_path / "config.toml"
    out.write_text(rendered)
    reloaded = DefaultConfig.from_toml(str(out))
    assert reloaded.email_enabled is True
    assert reloaded.email_proxy_base_url == "https://openhost-email-proxy.fly.dev"


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
