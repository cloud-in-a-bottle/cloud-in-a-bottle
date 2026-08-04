"""Config tests for the email fields on the shared-identity branch.

Email has no on/off Config flag any more (enablement is DB-based, see
``core.email.enablement``); Config only carries the static email fields and a few
pure helpers.  These tests pin the field defaults, the domain-normalization and
delegation helpers, ``effective_default_apps(email_on)``, the well-formed
custom-domain validation, and a TOML round trip of the email fields.  Config is
built directly via ``DefaultConfig(**kwargs)`` (no DB needed for any of this).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.config import DelegationRecord

_STALWART = "https://github.com/imbue-openhost/openhost-stalwart-email-server"
_BULWARK = "https://github.com/imbue-openhost/openhost-bulwark-email-client"


# --- field defaults ----------------------------------------------------------


def test_email_fields_default_to_none() -> None:
    cfg = DefaultConfig()
    assert cfg.email_proxy_base_url is None
    assert cfg.email_dmarc_rua is None
    assert cfg.email_custom_domain is None


def test_email_default_apps_default() -> None:
    assert DefaultConfig().email_default_apps == [_STALWART, _BULWARK]


def test_email_default_apps_is_not_shared_between_instances() -> None:
    # attr.Factory builds a fresh list per instance; mutating one must not leak.
    a = DefaultConfig()
    b = DefaultConfig()
    a.email_default_apps.append("mutated")
    assert b.email_default_apps == [_STALWART, _BULWARK]


def test_config_has_no_email_enabled_attribute() -> None:
    # Enablement moved to core.email.enablement (needs the DB), so Config must not
    # expose an email_enabled flag/property any more.
    assert not hasattr(DefaultConfig(), "email_enabled")


# --- email_custom_domain_normalized ------------------------------------------


def test_custom_domain_normalized_none_by_default() -> None:
    assert DefaultConfig().email_custom_domain_normalized is None


def test_custom_domain_normalized_lowercases() -> None:
    cfg = DefaultConfig(email_custom_domain="Mail.MyDomain.Com")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_normalized_strips_whitespace() -> None:
    cfg = DefaultConfig(email_custom_domain="  mail.mydomain.com  ")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_normalized_strips_trailing_dot() -> None:
    cfg = DefaultConfig(email_custom_domain="mail.mydomain.com.")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_normalized_all_at_once() -> None:
    cfg = DefaultConfig(email_custom_domain="  Mail.MyDomain.Com.  ")
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_blank_treated_as_unset() -> None:
    # A blank string (or whitespace-only) normalizes to None so callers treat
    # "unset" and "blank" identically.  A bare "." also validates as a domain
    # but must not sneak past normalization.
    assert DefaultConfig(email_custom_domain="   ").email_custom_domain_normalized is None
    assert DefaultConfig(email_custom_domain=".").email_custom_domain_normalized is None


# --- inbound_mail_host_for ----------------------------------------------------


def test_inbound_mail_host_prefixes_mail() -> None:
    assert DefaultConfig().inbound_mail_host_for("example.com") == "mail.example.com"


def test_inbound_mail_host_leaves_existing_mail_host() -> None:
    # A domain already under mail. is used as-is, not doubled to mail.mail.*.
    assert DefaultConfig().inbound_mail_host_for("mail.mydomain.com") == "mail.mydomain.com"


def test_inbound_mail_host_normalizes_case_and_dot() -> None:
    assert DefaultConfig().inbound_mail_host_for("Example.Com.") == "mail.example.com"


# --- custom_domain_delegation_record -----------------------------------------


def test_delegation_record_none_without_custom_domain() -> None:
    assert DefaultConfig().custom_domain_delegation_record("alice.example.com") is None


def test_delegation_record_shape() -> None:
    cfg = DefaultConfig(email_custom_domain="mail.mydomain.com")
    rec = cfg.custom_domain_delegation_record("alice.selfhost.imbue.com")
    assert rec == DelegationRecord(
        name="mail.mydomain.com",
        record_type="NS",
        value="ns.alice.selfhost.imbue.com",
    )


def test_delegation_record_strips_port_from_zone() -> None:
    cfg = DefaultConfig(email_custom_domain="mail.mydomain.com")
    rec = cfg.custom_domain_delegation_record("alice.selfhost.imbue.com:8443")
    assert rec is not None
    assert rec.value == "ns.alice.selfhost.imbue.com"


def test_delegation_record_display_line() -> None:
    cfg = DefaultConfig(email_custom_domain="mail.mydomain.com")
    rec = cfg.custom_domain_delegation_record("alice.example.com")
    assert rec is not None
    assert rec.as_display_line() == "mail.mydomain.com   NS   ns.alice.example.com"


# --- effective_default_apps ---------------------------------------------------


def test_effective_default_apps_excludes_email_apps_when_off() -> None:
    cfg = DefaultConfig(default_apps=["oauth_provider"])
    assert cfg.effective_default_apps(False) == ["oauth_provider"]


def test_effective_default_apps_appends_email_apps_when_on() -> None:
    cfg = DefaultConfig(default_apps=["oauth_provider"])
    apps = cfg.effective_default_apps(True)
    assert apps == ["oauth_provider", _STALWART, _BULWARK]


def test_effective_default_apps_preserves_default_apps_order() -> None:
    cfg = DefaultConfig(default_apps=["a", "b", "c"])
    assert cfg.effective_default_apps(True)[:3] == ["a", "b", "c"]


def test_effective_default_apps_dedupes_when_already_listed() -> None:
    cfg = DefaultConfig(default_apps=["oauth_provider", _STALWART])
    apps = cfg.effective_default_apps(True)
    # Stalwart already listed -> not appended twice, order preserved.
    assert apps == ["oauth_provider", _STALWART, _BULWARK]
    assert apps.count(_STALWART) == 1


def test_effective_default_apps_dedupes_both_email_apps() -> None:
    cfg = DefaultConfig(default_apps=[_BULWARK, _STALWART])
    apps = cfg.effective_default_apps(True)
    assert apps == [_BULWARK, _STALWART]


def test_effective_default_apps_returns_a_copy() -> None:
    # The returned list must not alias config.default_apps (callers mutate it).
    cfg = DefaultConfig(default_apps=["oauth_provider"])
    result = cfg.effective_default_apps(False)
    result.append("mutated")
    assert cfg.default_apps == ["oauth_provider"]


# --- well-formed custom-domain validation ------------------------------------


@pytest.mark.parametrize(
    "domain",
    [
        "mail.mydomain.com",
        "example.com",
        "a.b.c.d.example.com",
        "MAIL.MyDomain.COM",
        "mail.mydomain.com.",
        "xn--80ak6aa92e.com",  # punycode label
        "mail-server.my-domain.io",
        "a1.b2.example.com",
    ],
)
def test_custom_domain_accepts_well_formed(domain: str) -> None:
    cfg = DefaultConfig(email_custom_domain=domain)
    assert cfg.email_custom_domain_normalized is not None


@pytest.mark.parametrize(
    "domain",
    [
        "not a domain",
        "bad_domain!",
        "singlelabel",
        "-leadinghyphen.com",
        "trailinghyphen-.com",
        "double..dot.com",
        "under_score.com",
        "space in.com",
        "a..b",
        "-.com",
    ],
)
def test_custom_domain_rejects_malformed(domain: str) -> None:
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(email_custom_domain=domain)


def test_custom_domain_rejects_overlong_label() -> None:
    # A DNS label may be at most 63 chars.
    too_long = "a" * 64 + ".com"
    with pytest.raises(ValueError, match="not a well-formed domain"):
        DefaultConfig(email_custom_domain=too_long)


def test_custom_domain_accepts_max_length_label() -> None:
    exactly_63 = "a" * 63 + ".com"
    cfg = DefaultConfig(email_custom_domain=exactly_63)
    assert cfg.email_custom_domain_normalized == exactly_63


# --- TOML round trip ----------------------------------------------------------


def test_email_fields_round_trip_through_toml(tmp_path: Path) -> None:
    cfg = DefaultConfig(
        email_proxy_base_url="https://openhost.imbue.com",
        email_dmarc_rua="dmarc-reports@example.com",
        email_custom_domain="mail.mydomain.com",
        public_ip="203.0.113.5",
    )
    out = tmp_path / "config.toml"
    out.write_text(cfg.to_toml_str())
    reloaded = DefaultConfig.from_toml(str(out))
    assert reloaded.email_proxy_base_url == "https://openhost.imbue.com"
    assert reloaded.email_dmarc_rua == "dmarc-reports@example.com"
    assert reloaded.email_custom_domain == "mail.mydomain.com"
    assert reloaded.email_default_apps == [_STALWART, _BULWARK]


def test_email_toml_omits_unset_none_fields() -> None:
    # _to_toml_dict drops None-valued fields, so a config with no email set must
    # not render the optional email keys at all.
    rendered = DefaultConfig().to_toml_str()
    section = tomllib.loads(rendered)["openhost"]
    assert "email_proxy_base_url" not in section
    assert "email_dmarc_rua" not in section
    assert "email_custom_domain" not in section
    # The list fields always have a (non-None) default, so they DO render.
    assert section["email_default_apps"] == [_STALWART, _BULWARK]


def test_email_toml_renders_set_fields() -> None:
    cfg = DefaultConfig(email_proxy_base_url="https://p.test", email_custom_domain="mail.mydomain.com")
    section = tomllib.loads(cfg.to_toml_str())["openhost"]
    assert section["email_proxy_base_url"] == "https://p.test"
    assert section["email_custom_domain"] == "mail.mydomain.com"
