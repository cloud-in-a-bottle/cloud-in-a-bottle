"""Tests for email provisioning: proxy client parsing + zone-record publishing."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import compute_space.core.email.provision as prov
from compute_space.config import DefaultConfig
from compute_space.core.email.provision import provision_email_records
from compute_space.core.email.proxy_client import DkimRecord
from compute_space.core.email.proxy_client import EmailProxyClient
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.email.proxy_client import IdentityResult
from compute_space.core.tls.keycloak import StaticTokenProvider


def _write_zonefile(path: Path, serial: int = 100) -> None:
    path.write_text(
        "$ORIGIN alice.example.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.alice.example.com. admin.alice.example.com. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.alice.example.com.\n"
        "@   IN A    127.0.0.1\n"
    )


def _client_with_handler(handler) -> EmailProxyClient:
    transport = httpx.MockTransport(handler)
    return EmailProxyClient(
        base_url="https://proxy.test",
        token_provider=StaticTokenProvider(token="test-token"),
        http_client=httpx.Client(transport=transport),
    )


def test_proxy_client_parses_identity_and_sends_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "domain": "alice.example.com",
                "verified": False,
                "dkim_records": [
                    {"name": "a._domainkey.alice.example.com", "value": "a.dkim.amazonses.com"},
                    {"name": "b._domainkey.alice.example.com", "value": "b.dkim.amazonses.com"},
                ],
            },
        )

    client = _client_with_handler(handler)
    result = client.ensure_identity()
    assert seen["auth"] == "Bearer test-token"
    assert seen["path"] == "/api/email/identity"
    assert result.domain == "alice.example.com"
    assert len(result.dkim_records) == 2
    assert result.dkim_records[0].name == "a._domainkey.alice.example.com"


def test_proxy_client_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream boom")

    client = _client_with_handler(handler)
    with pytest.raises(EmailProxyError, match="HTTP 502"):
        client.ensure_identity()


def test_provision_email_records_writes_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mode="ses",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    )
    # Point the config's zonefile path at our temp file.
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))

    # Stub the proxy client construction to return a fake identity.

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            return IdentityResult(
                domain="alice.example.com",
                verified=False,
                dkim_records=(DkimRecord(name="tok._domainkey.alice.example.com", value="tok.dkim.amazonses.com"),),
            )

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FakeClient()))

    provision_email_records(cfg)

    content = zonefile.read_text()
    assert "v=spf1 include:amazonses.com" in content
    assert "v=DMARC1" in content
    assert "@   IN MX   10 inbound-smtp.us-west-2.amazonaws.com." in content
    assert "tok._domainkey.alice.example.com.   IN CNAME  tok.dkim.amazonses.com." in content


def test_provision_email_records_direct_inbound_points_mx_at_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Direct inbound: MX -> mail.<zone> + an A record for it -> the instance IP.
    Outbound still authorizes SES (SPF) and publishes SES DKIM."""
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mode="direct",  # the default; explicit for clarity
        public_ip="203.0.113.9",
    )
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            return IdentityResult(
                domain="alice.example.com",
                verified=False,
                dkim_records=(DkimRecord(name="tok._domainkey.alice.example.com", value="tok.dkim.amazonses.com"),),
            )

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FakeClient()))

    provision_email_records(cfg)

    content = zonefile.read_text()
    # Outbound unchanged: SES SPF + SES DKIM.
    assert "v=spf1 include:amazonses.com" in content
    assert "tok._domainkey.alice.example.com.   IN CNAME  tok.dkim.amazonses.com." in content
    # Inbound direct: MX -> mail.<zone>, with an A record for that host -> instance IP.
    assert "@   IN MX   10 mail.alice.example.com." in content
    assert "mail.alice.example.com.   IN A   203.0.113.9" in content
    # And NOT the SES inbound host.
    assert "inbound-smtp" not in content


def test_provision_email_records_provisions_custom_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # With a delegated custom mail domain configured, both the built-in zone and
    # the custom zone get an SES identity + published records, and the proxy is
    # asked specifically for the custom domain (request_domain != None).
    zonefile = tmp_path / "zonefile"
    custom_zonefile = tmp_path / "zonefile.custom"
    _write_zonefile(zonefile)
    custom_zonefile.write_text(
        "$ORIGIN mail.mydomain.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.alice.example.com. admin.mail.mydomain.com. (\n"
        "    100   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.alice.example.com.\n"
        "@   IN A    203.0.113.10\n"
    )

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mode="ses",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
        email_custom_domain="mail.mydomain.com",
    )
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))
    monkeypatch.setattr(type(cfg), "coredns_custom_zonefile_path", property(lambda self: custom_zonefile))

    requested: list[str | None] = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            requested.append(domain)
            target = domain or "alice.example.com"
            return IdentityResult(
                domain=target,
                verified=False,
                dkim_records=(DkimRecord(name=f"tok._domainkey.{target}", value="tok.dkim.amazonses.com"),),
            )

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FakeClient()))

    provision_email_records(cfg)

    # Built-in zone asked for with no explicit domain; custom zone asked for by name.
    assert requested == [None, "mail.mydomain.com"]

    # Built-in zone got its records.
    assert "tok._domainkey.alice.example.com.   IN CNAME  tok.dkim.amazonses.com." in zonefile.read_text()
    # Custom zone got its own records (SPF/DMARC/MX/DKIM under the custom origin).
    custom_content = custom_zonefile.read_text()
    assert "v=spf1 include:amazonses.com" in custom_content
    assert "@   IN MX   10 inbound-smtp.us-west-2.amazonaws.com." in custom_content
    assert "tok._domainkey.mail.mydomain.com.   IN CNAME  tok.dkim.amazonses.com." in custom_content


def test_provision_email_records_noop_when_disabled(tmp_path: Path):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    before = zonefile.read_text()
    cfg = DefaultConfig(zone_domain="alice.example.com")  # email disabled
    provision_email_records(cfg)
    assert zonefile.read_text() == before


def test_provision_email_records_survives_proxy_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        public_ip="203.0.113.9",
    )
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))

    class _FakeTokenProvider:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def ensure_identity(self, domain=None):
            raise EmailProxyError("proxy down")

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FailingClient()))

    # Must not raise, and must not modify the zone (fail-open).
    provision_email_records(cfg)
    assert zonefile.read_text() == before


def test_provision_custom_domain_direct_no_double_mail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A delegated custom domain 'mail.mydomain.com' in direct mode must NOT
    produce 'mail.mail.mydomain.com' — the mail host is used as-is."""
    zonefile = tmp_path / "zonefile"
    custom_zonefile = tmp_path / "custom_zonefile"
    _write_zonefile(zonefile)
    custom_zonefile.write_text(
        "$ORIGIN mail.mydomain.com.\n$TTL 60\n"
        "@   IN SOA  ns.alice.example.com. admin.alice.example.com. (\n"
        "    2020010100   ; serial\n    3600 ; refresh\n    600 ; retry\n"
        "    86400 ; expire\n    60 ; minimum\n)\n"
        "@   IN NS   ns.alice.example.com.\n@   IN A    203.0.113.10\n"
    )
    cfg = DefaultConfig(zone_domain="alice.example.com").evolve(
        email_enabled=True,
        email_proxy_base_url="https://proxy.test",
        email_keycloak_issuer_url="https://kc.test/realms/openhost-customers",
        email_keycloak_client_id="instance-alice",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mode="direct",
        public_ip="203.0.113.9",
        email_custom_domain="mail.mydomain.com",
    )
    monkeypatch.setattr(type(cfg), "coredns_zonefile_path", property(lambda self: zonefile))
    monkeypatch.setattr(type(cfg), "coredns_custom_zonefile_path", property(lambda self: custom_zonefile))

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def ensure_identity(self, domain=None):
            target = domain or "alice.example.com"
            return IdentityResult(domain=target, verified=False,
                                  dkim_records=(DkimRecord(name=f"tok._domainkey.{target}", value="t.dkim.amazonses.com"),))

    class _FakeTokenProvider:
        def __enter__(self): return self
        def __exit__(self, *a): return None

    monkeypatch.setattr(prov.KeycloakTokenProvider, "create", classmethod(lambda cls, creds: _FakeTokenProvider()))
    monkeypatch.setattr(prov.EmailProxyClient, "create", classmethod(lambda cls, url, tp: _FakeClient()))

    provision_email_records(cfg)

    primary = zonefile.read_text()
    custom = custom_zonefile.read_text()
    # Primary zone (alice.example.com) -> mail.alice.example.com
    assert "@   IN MX   10 mail.alice.example.com." in primary
    assert "mail.alice.example.com.   IN A   203.0.113.9" in primary
    # Custom zone: mail.mydomain.com is already a mail host -> used as-is, NOT doubled.
    assert "@   IN MX   10 mail.mydomain.com." in custom
    assert "mail.mydomain.com.   IN A   203.0.113.9" in custom
    assert "mail.mail." not in custom
