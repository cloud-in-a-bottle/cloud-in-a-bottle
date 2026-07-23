"""Edge cases for email DNS record rendering + custom-domain config validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.dns import DkimCname
from compute_space.core.dns import apply_email_records
from compute_space.core.dns import render_email_records

_EMAIL_KW = dict(
    email_enabled=True,
    email_proxy_base_url="https://frontend.example",
    email_keycloak_issuer_url="https://kc.example/realms/openhost-customers",
    email_keycloak_client_id="instance-x",
    email_keycloak_client_secret="secret",
    email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    public_ip="203.0.113.5",  # required for direct inbound (default)
)


# ─────────────────────── render_email_records ───────────────────────


def test_render_includes_spf_dmarc_mx():
    out = render_email_records("z.example", mail_from_host="inbound-smtp.us-west-2.amazonaws.com", dkim_cnames=[])
    assert '@   IN TXT  "v=spf1 include:amazonses.com ~all"' in out
    assert '_dmarc   IN TXT  "v=DMARC1; p=quarantine"' in out
    assert "@   IN MX   10 inbound-smtp.us-west-2.amazonaws.com." in out


def test_render_dmarc_rua_appended():
    out = render_email_records("z.example", mail_from_host="mx.aws", dkim_cnames=[], dmarc_rua="dmarc@z.example")
    assert "rua=mailto:dmarc@z.example" in out


def test_render_no_dkim_still_valid():
    out = render_email_records("z.example", mail_from_host="mx.aws", dkim_cnames=[])
    assert "IN CNAME" not in out
    assert out.strip().endswith("; --- end openhost email records ---")


def test_render_multiple_dkim_cnames():
    cnames = [DkimCname(name=f"t{i}._domainkey.z.example", target=f"t{i}.dkim.amazonses.com") for i in range(3)]
    out = render_email_records("z.example", mail_from_host="mx.aws", dkim_cnames=cnames)
    assert out.count("IN CNAME") == 3


def test_render_dkim_names_get_trailing_dot():
    c = [DkimCname(name="tok._domainkey.z.example", target="tok.dkim.amazonses.com")]
    out = render_email_records("z.example", mail_from_host="mx.aws", dkim_cnames=c)
    assert "tok._domainkey.z.example.   IN CNAME  tok.dkim.amazonses.com." in out


def test_render_dkim_names_already_dotted_not_doubled():
    c = [DkimCname(name="tok._domainkey.z.example.", target="tok.dkim.amazonses.com.")]
    out = render_email_records("z.example", mail_from_host="mx.aws", dkim_cnames=c)
    assert "tok._domainkey.z.example.   IN CNAME  tok.dkim.amazonses.com." in out
    assert ".." not in out.replace("; ---", "")  # no double dots in records


def test_render_mx_host_trailing_dot_not_doubled():
    out = render_email_records("z.example", mail_from_host="mx.aws.", dkim_cnames=[])
    assert "@   IN MX   10 mx.aws." in out
    assert "mx.aws.." not in out


def _zone_file(serial: int = 2020010100) -> str:
    return (
        "$ORIGIN z.example.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.z.example. admin.z.example. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.z.example.\n"
        "@   IN A    127.0.0.1\n"
    )


def test_apply_email_records_appends_and_bumps_serial(tmp_path: Path):
    zone = tmp_path / "zone.db"
    zone.write_text(_zone_file())
    apply_email_records(
        zone,
        "z.example",
        mail_from_host="mx.aws",
        dkim_cnames=[DkimCname(name="t._domainkey.z.example", target="t.dkim.amazonses.com")],
    )
    content = zone.read_text()
    assert "v=spf1 include:amazonses.com" in content
    assert "IN CNAME" in content
    # serial bumped from 2020010100
    assert "2020010100   ; serial" not in content


def test_apply_email_records_idempotent_serial_progresses(tmp_path: Path):
    zone = tmp_path / "zone.db"
    zone.write_text(_zone_file())
    apply_email_records(zone, "z.example", mail_from_host="mx", dkim_cnames=[])
    apply_email_records(zone, "z.example", mail_from_host="mx", dkim_cnames=[])
    second = zone.read_text()
    # applied twice -> two record blocks, serial advanced again
    assert second.count("openhost email records (managed)") == 2


# ─────────────────────── custom-domain validation ───────────────────────


def test_custom_domain_normalized_lower_and_strip():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_EMAIL_KW, email_custom_domain="  Mail.MyDomain.COM.  "
    )
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_blank_is_none():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_EMAIL_KW, email_custom_domain="   ")
    assert cfg.email_custom_domain_normalized is None


def test_custom_domain_unset_is_none():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_EMAIL_KW)
    assert cfg.email_custom_domain_normalized is None


@pytest.mark.parametrize("bad", [".mail.mydomain.com", "mail..mydomain.com", "mail.my..domain.com"])
def test_custom_domain_malformed_rejected(bad):
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_EMAIL_KW, email_custom_domain=bad)


def test_custom_domain_trailing_dots_normalized_not_rejected():
    # Trailing dots are stripped by normalization, so a trailing-dot FQDN is fine.
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_EMAIL_KW, email_custom_domain="mail.mydomain.com.."
    )
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


def test_custom_domain_equal_to_zone_rejected():
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
            **_EMAIL_KW, email_custom_domain="alice.selfhost.imbue.com"
        )


def test_custom_domain_subdomain_of_zone_rejected():
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
            **_EMAIL_KW, email_custom_domain="mail.alice.selfhost.imbue.com"
        )


def test_custom_domain_parent_of_zone_rejected():
    # zone is a subdomain of the custom domain -> also overlaps
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
            **_EMAIL_KW, email_custom_domain="selfhost.imbue.com"
        )


def test_custom_domain_distinct_ok():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_EMAIL_KW, email_custom_domain="mail.mydomain.com"
    )
    assert cfg.email_custom_domain_normalized == "mail.mydomain.com"


# ─────────────────────── delegation record ───────────────────────


def test_delegation_record_shape():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        **_EMAIL_KW, email_custom_domain="mail.mydomain.com"
    )
    rec = cfg.custom_domain_delegation_record()
    assert rec is not None
    assert rec.name == "mail.mydomain.com"
    assert rec.record_type == "NS"
    assert rec.value == "ns.alice.selfhost.imbue.com"


def test_delegation_record_none_without_custom_domain():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_EMAIL_KW)
    assert cfg.custom_domain_delegation_record() is None


def test_delegation_record_strips_zone_port():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com:8443").evolve(
        **_EMAIL_KW, email_custom_domain="mail.mydomain.com"
    )
    rec = cfg.custom_domain_delegation_record()
    assert rec.value == "ns.alice.selfhost.imbue.com"  # no :8443


# ─────────────────────── email_enabled validation ───────────────────────


@pytest.mark.parametrize(
    "missing",
    [
        "email_proxy_base_url",
        "email_keycloak_issuer_url",
        "email_keycloak_client_id",
        "email_keycloak_client_secret",
    ],
)
def test_email_enabled_requires_all_fields(missing):
    # These are required whenever email is enabled (outbound relay always via SES).
    # email_inbound_mx_host is required only in ses mode (covered separately).
    kw = dict(_EMAIL_KW)
    kw[missing] = None
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_direct_mode_requires_public_ip():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "direct"
    kw["public_ip"] = None
    with pytest.raises(ValueError, match="public_ip must be set"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_ses_mode_requires_mx_host():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "ses"
    kw["email_inbound_mx_host"] = None
    with pytest.raises(ValueError, match="email_inbound_mx_host must be set"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_invalid_inbound_mode_rejected():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "bogus"
    with pytest.raises(ValueError, match="email_inbound_mode"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_disabled_needs_no_fields():
    # No exception when email is off, even with all email_* unset.
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")
    assert cfg.email_enabled is False


# ─────────────────────── direct-inbound DNS rendering ───────────────────────


def test_render_direct_inbound_mx_and_a_record():
    out = render_email_records(
        "z.example",
        mail_from_host="",
        dkim_cnames=[],
        inbound_mail_host="mail.z.example",
        inbound_mail_ip="203.0.113.9",
    )
    assert "@   IN MX   10 mail.z.example." in out
    assert "mail.z.example.   IN A   203.0.113.9" in out
    assert "inbound-smtp" not in out
    # Outbound (SPF) still authorizes SES.
    assert "v=spf1 include:amazonses.com" in out


def test_render_direct_inbound_requires_ip():
    with pytest.raises(ValueError, match="inbound_mail_ip"):
        render_email_records(
            "z.example", mail_from_host="", dkim_cnames=[], inbound_mail_host="mail.z.example"
        )


def test_render_ses_inbound_unchanged_when_no_mail_host():
    out = render_email_records(
        "z.example", mail_from_host="inbound-smtp.us-west-2.amazonaws.com", dkim_cnames=[]
    )
    assert "@   IN MX   10 inbound-smtp.us-west-2.amazonaws.com." in out
    assert " IN A " not in out


def test_inbound_mail_host_for_uses_mail_subdomain():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")
    assert cfg.inbound_mail_host_for("alice.selfhost.imbue.com") == "mail.alice.selfhost.imbue.com"
    # An already-"mail." domain is used as-is (not doubled to mail.mail.…).
    assert cfg.inbound_mail_host_for("Mail.MyDomain.COM.") == "mail.mydomain.com"


# ─────────────────── direct-inbound: extensive edge cases ───────────────────


def test_render_direct_mx_and_a_exact_format():
    out = render_email_records(
        "z.example", mail_from_host="", dkim_cnames=[],
        inbound_mail_host="mail.z.example", inbound_mail_ip="1.2.3.4",
    )
    assert "@   IN MX   10 mail.z.example." in out
    assert "mail.z.example.   IN A   1.2.3.4" in out
    assert out.endswith("\n")


def test_render_direct_ignores_mail_from_host():
    out = render_email_records(
        "z.example", mail_from_host="inbound-smtp.us-west-2.amazonaws.com",
        dkim_cnames=[], inbound_mail_host="mail.z.example", inbound_mail_ip="1.2.3.4",
    )
    assert "inbound-smtp" not in out  # SES host ignored in direct mode


def test_render_direct_empty_ip_raises():
    with pytest.raises(ValueError, match="inbound_mail_ip"):
        render_email_records("z", mail_from_host="", dkim_cnames=[],
                             inbound_mail_host="mail.z", inbound_mail_ip="")


def test_render_direct_none_ip_raises():
    with pytest.raises(ValueError, match="inbound_mail_ip"):
        render_email_records("z", mail_from_host="", dkim_cnames=[],
                             inbound_mail_host="mail.z", inbound_mail_ip=None)


def test_render_direct_host_trailing_dots_stripped():
    out = render_email_records("z", mail_from_host="", dkim_cnames=[],
                               inbound_mail_host="mail.z.example...", inbound_mail_ip="1.2.3.4")
    assert "@   IN MX   10 mail.z.example." in out
    assert ".." not in out.replace("; ---", "")


def test_render_ses_no_a_record():
    out = render_email_records("z", mail_from_host="inbound-smtp.aws", dkim_cnames=[])
    assert "@   IN MX   10 inbound-smtp.aws." in out
    assert " IN A " not in out


def test_render_dmarc_rua_double_mailto_documented():
    # Caller passes a bare address; passing a mailto: is a caller error that the
    # renderer does not dedupe. Document current behavior.
    out = render_email_records("z", mail_from_host="mx", dkim_cnames=[], dmarc_rua="mailto:x@y")
    assert "rua=mailto:mailto:x@y" in out


def test_render_dmarc_rua_empty_is_none():
    out = render_email_records("z", mail_from_host="mx", dkim_cnames=[], dmarc_rua="")
    assert 'v=DMARC1; p=quarantine"' in out
    assert "rua=" not in out


def test_apply_direct_error_leaves_file_untouched(tmp_path):
    zone = tmp_path / "z.db"
    zone.write_text(_zone_file())
    before = zone.read_text()
    with pytest.raises(ValueError):
        apply_email_records(zone, "z.example", mail_from_host="", dkim_cnames=[],
                            inbound_mail_host="mail.z.example", inbound_mail_ip=None)
    assert zone.read_text() == before  # no partial write / no serial bump


def test_apply_direct_writes_mx_and_a(tmp_path):
    zone = tmp_path / "z.db"
    zone.write_text(_zone_file())
    apply_email_records(zone, "z.example", mail_from_host="", dkim_cnames=[],
                        inbound_mail_host="mail.z.example", inbound_mail_ip="9.9.9.9")
    content = zone.read_text()
    assert "@   IN MX   10 mail.z.example." in content
    assert "mail.z.example.   IN A   9.9.9.9" in content
    assert "2020010100   ; serial" not in content  # bumped


# ─── inbound_mail_host_for normalization + double-mail fix ───


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("alice.host.imbue.com", "mail.alice.host.imbue.com"),
        ("alice.host.imbue.com.", "mail.alice.host.imbue.com"),
        ("Alice.HOST.Imbue.COM", "mail.alice.host.imbue.com"),
        ("  alice.host.imbue.com  ", "mail.alice.host.imbue.com"),
        ("  alice.host.imbue.com..  ", "mail.alice.host.imbue.com"),
        # already a mail. host -> NOT doubled (the fix)
        ("mail.mydomain.com", "mail.mydomain.com"),
        ("Mail.MyDomain.COM.", "mail.mydomain.com"),
    ],
)
def test_inbound_mail_host_for(domain, expected):
    cfg = DefaultConfig(zone_domain="alice.host.imbue.com")
    assert cfg.inbound_mail_host_for(domain) == expected


# ─── mode validation: case/whitespace sensitivity ───


@pytest.mark.parametrize("mode", ["Direct", "DIRECT", " direct", "ses ", "SES", "bogus", ""])
def test_invalid_inbound_mode_rejected(mode):
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = mode
    with pytest.raises(ValueError, match="email_inbound_mode"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_direct_mode_empty_public_ip_rejected():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "direct"
    kw["public_ip"] = ""
    with pytest.raises(ValueError, match="public_ip must be set"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_ses_mode_does_not_require_public_ip():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "ses"
    kw["public_ip"] = None
    kw["email_inbound_mx_host"] = "inbound-smtp.us-west-2.amazonaws.com"
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)
    assert cfg.email_inbound_mode == "ses"


def test_disabled_email_ignores_bad_mode():
    # email off -> mode not validated at all
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(
        email_enabled=False, email_inbound_mode="bogus"
    )
    assert cfg.email_enabled is False


def test_mode_validation_after_proxy_field_validation():
    kw = dict(_EMAIL_KW)
    kw["email_inbound_mode"] = "bogus"
    kw["email_proxy_base_url"] = None
    with pytest.raises(ValueError, match="email_proxy_base_url"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)
