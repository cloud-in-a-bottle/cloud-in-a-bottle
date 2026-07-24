"""Edge cases for email DNS record rendering + custom-domain config validation.

Inbound is ALWAYS direct to the instance's own mail server: the zone's MX points
at ``mail.<zone>`` and an A record for that host resolves to the instance's
public IP, so Stalwart receives on port 25. Mail never traverses OpenHost
infrastructure inbound (the platform can never read a tenant's mail). Only
outbound relays through the central SES proxy. There is no "ses" inbound mode.
"""

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
    public_ip="203.0.113.5",  # required whenever email is enabled (direct inbound)
)


def _render(zone: str = "z.example", **kwargs) -> str:
    """render_email_records with sensible direct-inbound defaults for brevity."""
    kwargs.setdefault("inbound_mail_host", "mail.z.example")
    kwargs.setdefault("inbound_mail_ip", "203.0.113.9")
    kwargs.setdefault("dkim_cnames", [])
    return render_email_records(zone, **kwargs)


# ─────────────────────── render_email_records: core ───────────────────────


def test_render_includes_spf_dmarc_mx_and_a():
    out = _render()
    assert '@   IN TXT  "v=spf1 include:amazonses.com ~all"' in out
    assert '_dmarc   IN TXT  "v=DMARC1; p=quarantine"' in out
    assert "@   IN MX   10 mail.z.example." in out
    assert "mail.z.example.   IN A   203.0.113.9" in out


def test_render_mx_always_points_at_instance_not_ses():
    # The SES inbound host must NEVER appear as an MX target.
    out = _render(inbound_mail_host="mail.z.example", inbound_mail_ip="1.2.3.4")
    assert "inbound-smtp" not in out
    assert "amazonaws.com" not in out
    assert "@   IN MX   10 mail.z.example." in out


def test_render_dmarc_rua_appended():
    out = _render(dmarc_rua="dmarc@z.example")
    assert "rua=mailto:dmarc@z.example" in out


def test_render_dmarc_rua_empty_is_omitted():
    out = _render(dmarc_rua="")
    assert 'v=DMARC1; p=quarantine"' in out
    assert "rua=" not in out


def test_render_dmarc_rua_double_mailto_documented():
    # Caller passes a bare address; a mailto: prefix is a caller error the
    # renderer does not dedupe. Document current behavior.
    out = _render(dmarc_rua="mailto:x@y")
    assert "rua=mailto:mailto:x@y" in out


def test_render_no_dkim_still_valid():
    out = _render(dkim_cnames=[])
    assert "IN CNAME" not in out
    assert out.strip().endswith("; --- end openhost email records ---")


def test_render_multiple_dkim_cnames():
    cnames = [DkimCname(name=f"t{i}._domainkey.z.example", target=f"t{i}.dkim.amazonses.com") for i in range(3)]
    out = _render(dkim_cnames=cnames)
    assert out.count("IN CNAME") == 3


def test_render_dkim_names_get_trailing_dot():
    c = [DkimCname(name="tok._domainkey.z.example", target="tok.dkim.amazonses.com")]
    out = _render(dkim_cnames=c)
    assert "tok._domainkey.z.example.   IN CNAME  tok.dkim.amazonses.com." in out


def test_render_dkim_names_already_dotted_not_doubled():
    c = [DkimCname(name="tok._domainkey.z.example.", target="tok.dkim.amazonses.com.")]
    out = _render(dkim_cnames=c)
    assert "tok._domainkey.z.example.   IN CNAME  tok.dkim.amazonses.com." in out
    assert ".." not in out.replace("; ---", "")  # no double dots in records


# ─────────────────────── render: mail host / IP edge cases ───────────────────────


def test_render_mail_host_trailing_dots_stripped():
    out = _render(inbound_mail_host="mail.z.example...", inbound_mail_ip="1.2.3.4")
    assert "@   IN MX   10 mail.z.example." in out
    assert "mail.z.example.   IN A   1.2.3.4" in out
    assert ".." not in out.replace("; ---", "")


def test_render_output_ends_with_newline():
    assert _render().endswith("\n")


def test_render_empty_ip_raises():
    with pytest.raises(ValueError, match="inbound_mail_ip"):
        render_email_records("z", inbound_mail_host="mail.z", inbound_mail_ip="", dkim_cnames=[])


def test_render_none_ip_raises():
    with pytest.raises(ValueError, match="inbound_mail_ip"):
        render_email_records("z", inbound_mail_host="mail.z", inbound_mail_ip=None, dkim_cnames=[])  # type: ignore[arg-type]


def test_render_empty_host_raises():
    with pytest.raises(ValueError, match="inbound_mail_host"):
        render_email_records("z", inbound_mail_host="", inbound_mail_ip="1.2.3.4", dkim_cnames=[])


def test_render_ipv6_ip_passthrough():
    out = _render(inbound_mail_ip="2001:db8::1")
    assert "mail.z.example.   IN A   2001:db8::1" in out


# ─────────────────────── apply_email_records ───────────────────────


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


def test_apply_email_records_appends_mx_a_and_bumps_serial(tmp_path: Path):
    zone = tmp_path / "zone.db"
    zone.write_text(_zone_file())
    apply_email_records(
        zone,
        "z.example",
        inbound_mail_host="mail.z.example",
        inbound_mail_ip="9.9.9.9",
        dkim_cnames=[DkimCname(name="t._domainkey.z.example", target="t.dkim.amazonses.com")],
    )
    content = zone.read_text()
    assert "v=spf1 include:amazonses.com" in content
    assert "@   IN MX   10 mail.z.example." in content
    assert "mail.z.example.   IN A   9.9.9.9" in content
    assert "IN CNAME" in content
    assert "2020010100   ; serial" not in content  # bumped


def test_apply_email_records_idempotent_serial_progresses(tmp_path: Path):
    zone = tmp_path / "zone.db"
    zone.write_text(_zone_file())
    apply_email_records(
        zone, "z.example", inbound_mail_host="mail.z.example", inbound_mail_ip="1.1.1.1", dkim_cnames=[]
    )
    apply_email_records(
        zone, "z.example", inbound_mail_host="mail.z.example", inbound_mail_ip="1.1.1.1", dkim_cnames=[]
    )
    second = zone.read_text()
    assert second.count("openhost email records (managed)") == 2


def test_apply_error_leaves_file_untouched(tmp_path: Path):
    zone = tmp_path / "z.db"
    zone.write_text(_zone_file())
    before = zone.read_text()
    with pytest.raises(ValueError):
        apply_email_records(
            zone,
            "z.example",
            inbound_mail_host="mail.z.example",
            inbound_mail_ip=None,  # type: ignore[arg-type]
            dkim_cnames=[],
        )
    assert zone.read_text() == before  # no partial write / no serial bump


# ─────────────────────── inbound_mail_host_for ───────────────────────


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("alice.host.imbue.com", "mail.alice.host.imbue.com"),
        ("alice.host.imbue.com.", "mail.alice.host.imbue.com"),
        ("Alice.HOST.Imbue.COM", "mail.alice.host.imbue.com"),
        ("  alice.host.imbue.com  ", "mail.alice.host.imbue.com"),
        ("  alice.host.imbue.com..  ", "mail.alice.host.imbue.com"),
        # already a mail. host -> NOT doubled to mail.mail.…
        ("mail.mydomain.com", "mail.mydomain.com"),
        ("Mail.MyDomain.COM.", "mail.mydomain.com"),
    ],
)
def test_inbound_mail_host_for(domain, expected):
    cfg = DefaultConfig(zone_domain="alice.host.imbue.com")
    assert cfg.inbound_mail_host_for(domain) == expected


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
    assert rec is not None
    assert rec.value == "ns.alice.selfhost.imbue.com"  # no :8443


# ─────────────────────── email_enabled validation ───────────────────────


@pytest.mark.parametrize(
    "missing",
    [
        "email_proxy_base_url",
        "email_keycloak_issuer_url",
        "email_keycloak_client_id",
        "email_keycloak_client_secret",
        "public_ip",
    ],
)
def test_email_enabled_requires_all_fields(missing):
    # Every one of these is required whenever email is enabled; public_ip because
    # inbound is always direct (the MX/A records point at this instance).
    kw = dict(_EMAIL_KW)
    kw[missing] = None
    with pytest.raises(ValueError):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_enabled_empty_public_ip_rejected():
    kw = dict(_EMAIL_KW)
    kw["public_ip"] = ""
    with pytest.raises(ValueError, match="public_ip must be set"):
        DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**kw)


def test_email_disabled_needs_no_fields():
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com")
    assert cfg.email_enabled is False


def test_email_disabled_without_public_ip_ok():
    # public_ip is only required when email is enabled.
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(email_enabled=False)
    assert cfg.email_enabled is False
    assert cfg.public_ip is None


def test_no_ses_inbound_mode_field():
    # The ses inbound mode is gone entirely — the fields no longer exist.
    cfg = DefaultConfig(zone_domain="alice.selfhost.imbue.com").evolve(**_EMAIL_KW)
    assert not hasattr(cfg, "email_inbound_mode")
    assert not hasattr(cfg, "email_inbound_mx_host")
