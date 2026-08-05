"""Tests for core.email.provision.provision_email_records.

Provisioning is a no-op unless email is enabled (proxy URL + Imbue identity in the
DB + public IP).  When enabled it authenticates, asks the proxy to ensure the SES
identity, and appends SPF/DMARC/MX/A/DKIM records into the primary zone file (and
the custom-domain zone file when one is delegated).  It is best-effort: an
EmailProxyError (or any exception) is logged and swallowed, never raised.

The DB is a file-backed test DB with a seeded primary domain + Imbue identity
(``_make_test_config`` + ``set_instance_identity``).  The proxy/token clients are
monkeypatched with in-process fakes so no network is touched; the zone file lives
under ``config.coredns_zonefile_path`` (the primary's legacy path).
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from compute_space.config import Config
from compute_space.core.email.provision import provision_email_records
from compute_space.core.email.proxy_client import DkimRecord
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.email.proxy_client import IdentityResult
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

_ZONE = "alice.example.com"
_IP = "203.0.113.9"


def _cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc.test/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="s3cr3t",
    )


def _write_zonefile(path: Path, origin: str = _ZONE, serial: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"$ORIGIN {origin}.\n"
        "$TTL 60\n"
        f"@   IN SOA  ns.{_ZONE}. admin.{origin}. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        f"@   IN NS   ns.{_ZONE}.\n"
        "@   IN A    127.0.0.1\n"
    )


def _enabled_config(tmp_path: Path, **overrides: Any) -> Config:
    cfg = _make_test_config(tmp_path, zone_domain=_ZONE, **overrides)
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, _cred())
    return cfg


class _FakeTokenProvider:
    def __enter__(self) -> _FakeTokenProvider:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class _FakeClient:
    """Records the domains it was asked for and returns one DKIM record per call."""

    def __init__(self) -> None:
        self.requested: list[str | None] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def ensure_identity(self, domain: str | None = None) -> IdentityResult:
        self.requested.append(domain)
        target = domain or _ZONE
        return IdentityResult(
            verified=False,
            dkim_records=(DkimRecord(name=f"tok._domainkey.{target}", value="tok.dkim.amazonses.com"),),
        )


def _install_fakes(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(
        "compute_space.core.email.provision.KeycloakTokenProvider.create",
        classmethod(lambda cls, creds: _FakeTokenProvider()),
    )
    monkeypatch.setattr(
        "compute_space.core.email.provision.EmailProxyClient.create",
        classmethod(lambda cls, url, tp: client),
    )


# --- no-op when disabled -----------------------------------------------------


def test_noop_when_email_disabled_leaves_zone_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path, zone_domain=_ZONE)  # no identity, no proxy, no public_ip
    zonefile = cfg.coredns_zonefile_path
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    called = {"create": False}

    def _mark_created(cls: object, /, *a: object, **k: object) -> object:
        called["create"] = True
        return object()

    monkeypatch.setattr(
        "compute_space.core.email.provision.EmailProxyClient.create",
        classmethod(_mark_created),
    )
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert zonefile.read_text() == before
    assert called["create"] is False


def test_noop_when_identity_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Proxy + public_ip set, but no Imbue identity -> still disabled -> no-op.
    cfg = _make_test_config(tmp_path, zone_domain=_ZONE, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    zonefile = cfg.coredns_zonefile_path
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert zonefile.read_text() == before
    assert client.requested == []


# --- enabled: writes records into the primary zone ---------------------------


def test_enabled_publishes_records_into_primary_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    zonefile = cfg.coredns_zonefile_path
    _write_zonefile(zonefile)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    content = zonefile.read_text()
    # SPF authorizing SES (outbound).
    assert "v=spf1 include:amazonses.com ~all" in content
    # DMARC policy.
    assert "_dmarc   IN TXT" in content
    assert "v=DMARC1" in content
    # Direct inbound: MX -> mail.<zone>, with an A record for it -> instance IP.
    assert f"@   IN MX   10 mail.{_ZONE}." in content
    assert f"mail.{_ZONE}.   IN A   {_IP}" in content
    # DKIM CNAME.
    assert f"tok._domainkey.{_ZONE}.   IN CNAME  tok.dkim.amazonses.com." in content


def test_enabled_calls_ensure_identity_for_primary_with_no_request_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The built-in zone is requested with request_domain=None (the proxy scopes
    # to the caller's own zone).
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    _write_zonefile(cfg.coredns_zonefile_path)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert client.requested == [None]


def test_enabled_bumps_soa_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    _write_zonefile(cfg.coredns_zonefile_path, serial=100)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert "101   ; serial" in cfg.coredns_zonefile_path.read_text()


def test_enabled_publishes_dmarc_rua_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(
        tmp_path,
        email_proxy_base_url="https://proxy.test",
        public_ip=_IP,
        email_dmarc_rua="reports@example.com",
    )
    _write_zonefile(cfg.coredns_zonefile_path)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert "rua=mailto:reports@example.com" in cfg.coredns_zonefile_path.read_text()


def test_enabled_no_ses_inbound_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Inbound is always direct-to-instance; the SES inbound host must never appear.
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    _write_zonefile(cfg.coredns_zonefile_path)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    assert "inbound-smtp" not in cfg.coredns_zonefile_path.read_text()


# --- custom domain -----------------------------------------------------------


def test_custom_domain_publishes_second_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(
        tmp_path,
        email_proxy_base_url="https://proxy.test",
        public_ip=_IP,
        email_custom_domain="mail.mydomain.com",
    )
    _write_zonefile(cfg.coredns_zonefile_path)
    custom_zonefile = cfg.coredns_zonefile_path_for("mail.mydomain.com", False)
    _write_zonefile(custom_zonefile, origin="mail.mydomain.com")

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    # Built-in zone requested with None; custom zone requested by name.
    assert client.requested == [None, "mail.mydomain.com"]
    custom_content = custom_zonefile.read_text()
    # Custom zone gets its own records. mail.mydomain.com is already a mail host,
    # so it is used as-is (not doubled to mail.mail.mydomain.com).
    assert "@   IN MX   10 mail.mydomain.com." in custom_content
    assert f"mail.mydomain.com.   IN A   {_IP}" in custom_content
    assert "mail.mail." not in custom_content
    assert "tok._domainkey.mail.mydomain.com.   IN CNAME  tok.dkim.amazonses.com." in custom_content


def test_custom_domain_also_updates_primary_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(
        tmp_path,
        email_proxy_base_url="https://proxy.test",
        public_ip=_IP,
        email_custom_domain="mail.mydomain.com",
    )
    _write_zonefile(cfg.coredns_zonefile_path)
    custom_zonefile = cfg.coredns_zonefile_path_for("mail.mydomain.com", False)
    _write_zonefile(custom_zonefile, origin="mail.mydomain.com")

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    # Primary zone still got its own records too.
    assert f"@   IN MX   10 mail.{_ZONE}." in cfg.coredns_zonefile_path.read_text()


def test_no_custom_domain_only_touches_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    _write_zonefile(cfg.coredns_zonefile_path)

    client = _FakeClient()
    _install_fakes(monkeypatch, client)
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)

    # Only the primary zone was requested; no custom-zone file was created.
    assert client.requested == [None]
    assert not cfg.zones_dir.exists() or list(cfg.zones_dir.glob("*.zone")) == []


# --- best-effort error handling ----------------------------------------------


def test_proxy_error_is_swallowed_and_zone_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    zonefile = cfg.coredns_zonefile_path
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    class _FailingClient:
        def __enter__(self) -> _FailingClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def ensure_identity(self, domain: str | None = None) -> IdentityResult:
            raise EmailProxyError("proxy down")

    _install_fakes(monkeypatch, _FailingClient())
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)  # must not raise

    assert zonefile.read_text() == before


def test_unexpected_exception_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _enabled_config(tmp_path, email_proxy_base_url="https://proxy.test", public_ip=_IP)
    zonefile = cfg.coredns_zonefile_path
    _write_zonefile(zonefile)
    before = zonefile.read_text()

    class _BoomClient:
        def __enter__(self) -> _BoomClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def ensure_identity(self, domain: str | None = None) -> IdentityResult:
            raise RuntimeError("unexpected")

    _install_fakes(monkeypatch, _BoomClient())
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)  # must not raise

    assert zonefile.read_text() == before


def test_custom_domain_error_does_not_prevent_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The primary zone is published first; a failure on the custom domain must not
    # roll back the primary's records (best-effort, in order).
    cfg = _enabled_config(
        tmp_path,
        email_proxy_base_url="https://proxy.test",
        public_ip=_IP,
        email_custom_domain="mail.mydomain.com",
    )
    _write_zonefile(cfg.coredns_zonefile_path)
    custom_zonefile = cfg.coredns_zonefile_path_for("mail.mydomain.com", False)
    _write_zonefile(custom_zonefile, origin="mail.mydomain.com")

    class _CustomFailsClient:
        def __enter__(self) -> _CustomFailsClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def ensure_identity(self, domain: str | None = None) -> IdentityResult:
            if domain is not None:
                raise EmailProxyError("custom domain identity failed")
            return IdentityResult(
                verified=False,
                dkim_records=(DkimRecord(name=f"tok._domainkey.{_ZONE}", value="tok.dkim.amazonses.com"),),
            )

    _install_fakes(monkeypatch, _CustomFailsClient())
    with closing(open_db(cfg)) as db:
        provision_email_records(cfg, db)  # must not raise

    # Primary got its records; custom zone did not.
    assert f"@   IN MX   10 mail.{_ZONE}." in cfg.coredns_zonefile_path.read_text()
    assert "IN MX" not in custom_zonefile.read_text()
