from __future__ import annotations

import socket
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns as dns_mod
from compute_space.config import DefaultConfig
from compute_space.core.dns import DkimCname
from compute_space.core.dns import DnsZone
from compute_space.core.dns import TxtRecord
from compute_space.core.dns import append_txt_records
from compute_space.core.dns import apply_email_records
from compute_space.core.dns import clear_acme_challenge_records
from compute_space.core.dns import public_dns_zones
from compute_space.core.dns import reload_coredns_for_domains
from compute_space.core.dns import render_email_records
from compute_space.core.dns import set_active_coredns
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.core.domains import upsert_record
from compute_space.db import init_db
from compute_space.tests.conftest import open_db


def _seed_dns_cfg(tmp_path: Path, *domains: Domain, **kw: Any) -> DefaultConfig:
    """A config whose DB is seeded with ``domains`` (primary first), for the DB-backed zone builder."""
    primary = domains[0]
    cfg = DefaultConfig(data_root_dir=str(tmp_path), **kw)
    cfg.make_all_dirs()
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, primary, [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
    return cfg


def _write_zonefile(path: Path, serial: int = 100) -> None:
    path.write_text(
        "$ORIGIN app.example.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.app.example.com. admin.app.example.com. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.app.example.com.\n"
        "@   IN A    127.0.0.1\n"
    )


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr

    def getsockname(self) -> tuple[str, int]:
        return ("10.0.0.5", 12345)


def test_coredns_bind_ip_uses_default_route_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(dns_mod.socket, "socket", lambda *args: fake_socket)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "10.0.0.5"
    assert fake_socket.addr == ("8.8.8.8", 80)


def test_coredns_bind_ip_falls_back_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(*args: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", raise_os_error)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "203.0.113.10"


def test_append_txt_records_writes_relative_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)

    # Local DNS-01 path: several values share one relative name, left for CoreDNS
    # to resolve against $ORIGIN.
    append_txt_records(
        zonefile,
        [
            TxtRecord(record_name="_acme-challenge", record_value="base-value"),
            TxtRecord(record_name="_acme-challenge", record_value="wildcard-value"),
        ],
    )

    content = zonefile.read_text()
    assert '_acme-challenge   IN TXT  "base-value"' in content
    assert '_acme-challenge   IN TXT  "wildcard-value"' in content
    # Relative name is not turned into an absolute FQDN.
    assert "_acme-challenge.   IN TXT" not in content
    # Serial bumped so CoreDNS reloads.
    assert "101   ; serial" in content


def test_append_txt_records_writes_absolute_fqdn_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    # Broker path: names arrive as absolute FQDNs (trailing dot) so CoreDNS does
    # not re-append $ORIGIN.
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    content = zonefile.read_text()
    assert '_acme-challenge.app.example.com.   IN TXT  "v"' in content
    # Not doubled up into _acme-challenge.app.example.com.app.example.com.
    assert "app.example.com.app.example.com" not in content


def test_clear_acme_challenge_records_removes_acme_txt(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    clear_acme_challenge_records(zonefile)

    assert "IN TXT" not in zonefile.read_text()


def test_clear_acme_challenge_records_preserves_email_txt(tmp_path: Path) -> None:
    # SPF (apex) and DMARC (_dmarc) TXT records must survive a cert renewal.
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    append_txt_records(
        zonefile,
        [
            TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="challenge"),
            TxtRecord(record_name="@", record_value="v=spf1 include:amazonses.com -all"),
            TxtRecord(record_name="_dmarc", record_value="v=DMARC1; p=quarantine"),
        ],
    )

    clear_acme_challenge_records(zonefile)

    text = zonefile.read_text()
    assert "challenge" not in text  # ACME challenge removed
    assert "v=spf1" in text  # SPF preserved
    assert "v=DMARC1" in text  # DMARC preserved


class _FakeProc:
    pid = 4242
    stdout = None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        # Report already-exited so CoreDnsProcess.restart() skips the terminate path.
        return 0


def _stub_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Don't spawn the log-streaming thread (its target reads proc.stdout).
    monkeypatch.setattr(dns_mod.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())


def test_container_dns_view_rendered_when_gateway_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["9.9.9.9"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns(
        (dns_mod.DnsZone("app.example.com", zonefile),),
        "203.0.113.10",
        corefile,
        container_gateway_ip="10.200.0.1",
    )

    cf = corefile.read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf

    # Public zonefile points at the public IP; container zonefile at the gateway.
    assert "203.0.113.10" in zonefile.read_text()
    container_zone = tmp_path / "zonefile.container"
    assert container_zone.exists()
    cz = container_zone.read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert "203.0.113.10" not in cz


def test_container_dns_view_skipped_when_gateway_not_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns(
        (dns_mod.DnsZone("app.example.com", zonefile),),
        "203.0.113.10",
        corefile,
        container_gateway_ip="10.200.0.1",
    )

    cf = corefile.read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    # No container zonefile written.
    assert not (tmp_path / "zonefile.container").exists()


def test_host_upstream_resolvers_filters_loopback_and_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(
        "nameserver 127.0.0.53\n"
        f"nameserver {dns_mod.CONTAINER_GATEWAY_IP}\n"
        "nameserver 185.12.64.1\n"
        "nameserver 1.1.1.1\n"
        "search example.com\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == ["185.12.64.1", "1.1.1.1"]


def test_host_upstream_resolvers_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*a: object, **k: object) -> object:
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", raise_oserror)
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_host_upstream_resolvers_falls_back_when_only_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host using only the systemd-resolved stub (127.0.0.53) would leave the
    # container view with no forwardable upstream; we must fall back, never emit
    # an empty/loopback forward (which would be unreachable from the container).
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_container_view_forward_uses_discovered_resolvers_and_distinct_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The public view and the container view must bind different addresses (the
    # default-route source vs the gateway), and the container catch-all must
    # forward to the discovered upstreams.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["185.12.64.1", "1.1.1.1"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    dns_mod.start_coredns((dns_mod.DnsZone("app.example.com", tmp_path / "zonefile"),), "203.0.113.10", corefile)
    cf = corefile.read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert "bind 10.200.0.1" in cf  # container view + catch-all
    assert "forward . 185.12.64.1 1.1.1.1" in cf
    # Catch-all is scoped to the container gateway only (never the public bind),
    # so the public IP is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert "bind 10.200.0.1" in catch_all
    assert "bind 10.0.0.5" not in catch_all


def test_public_dns_zones_covers_every_public_domain_and_skips_mdns(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="host.example.org", tls=True),
        Domain(name="myhost.local", tls=False, mdns=True),
    )
    with closing(open_db(config)) as db:
        zones = public_dns_zones(config, db)
    # The mDNS domain is excluded (served by the responder, not CoreDNS).
    assert [z.domain for z in zones] == ["host.example.com", "host.example.org"]
    # Primary keeps the legacy zonefile path; the secondary gets a per-domain file under zones/.
    assert zones[0].zonefile_path == config.coredns_zonefile_path
    assert zones[1].zonefile_path == config.zones_dir / "host.example.org.zone"


def test_public_dns_zones_includes_custom_mail_domain(tmp_path: Path) -> None:
    # The custom mail domain is a mail-only zone (not in the domains table); it must
    # still be served by CoreDNS so its SPF/DKIM/DMARC/MX resolve.
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        email_custom_domain="mail.mydomain.com",
    )
    with closing(open_db(config)) as db:
        zones = public_dns_zones(config, db)
    domains = [z.domain for z in zones]
    assert "mail.mydomain.com" in domains
    custom = next(z for z in zones if z.domain == "mail.mydomain.com")
    # Served from a per-domain zone file (never the primary's legacy path).
    assert custom.zonefile_path == config.zones_dir / "mail.mydomain.com.zone"


def test_public_dns_zones_no_custom_mail_domain_when_unset(tmp_path: Path) -> None:
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        zones = public_dns_zones(config, db)
    assert [z.domain for z in zones] == ["host.example.com"]


def test_public_dns_zones_dedupes_custom_mail_domain(tmp_path: Path) -> None:
    # If the custom mail domain coincides with an existing web domain, it is not
    # duplicated (the web domain's zone already serves it).
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="mail.mydomain.com", tls=True),
        email_custom_domain="mail.mydomain.com",
    )
    with closing(open_db(config)) as db:
        zones = public_dns_zones(config, db)
    assert [z.domain for z in zones].count("mail.mydomain.com") == 1


def test_start_coredns_creates_custom_mail_zone_file_for_email_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end of the boot ordering fix: public_dns_zones includes the custom
    # mail domain, so start_coredns creates its zone file from template, and email
    # provisioning can then append records into a zone CoreDNS actually serves.
    # Regression guard: previously the custom zone file was never created here, so
    # apply_email_records had nothing to append to on a real instance.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        email_custom_domain="mail.mydomain.com",
    )
    with closing(open_db(config)) as db:
        zones = public_dns_zones(config, db)
    custom_zonefile = config.coredns_zonefile_path_for("mail.mydomain.com", is_primary=False)
    assert not custom_zonefile.exists()  # not pre-created

    dns_mod.start_coredns(zones, "203.0.113.10", config.coredns_corefile_path)

    # start_coredns created the custom zone file (authoritative for its origin) and
    # the Corefile has a server block for it.
    assert custom_zonefile.exists()
    assert "$ORIGIN mail.mydomain.com." in custom_zonefile.read_text()
    assert "mail.mydomain.com:53 {" in config.coredns_corefile_path.read_text()

    # Email provisioning can now append records into the served zone (no FileNotFound).
    apply_email_records(
        custom_zonefile,
        "mail.mydomain.com",
        inbound_mail_host="mail.mydomain.com",
        inbound_mail_ip="203.0.113.10",
        dkim_cnames=[DkimCname(name="tok._domainkey.mail.mydomain.com", target="tok.dkim.amazonses.com")],
    )
    content = custom_zonefile.read_text()
    assert "@   IN MX   10 mail.mydomain.com." in content
    assert "tok._domainkey.mail.mydomain.com.   IN CNAME  tok.dkim.amazonses.com." in content


def test_start_coredns_writes_a_zone_block_and_file_per_public_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    primary_zone = tmp_path / "zonefile"
    secondary_zone = tmp_path / "zones" / "host.example.org.zone"
    dns_mod.start_coredns(
        (DnsZone("host.example.com", primary_zone), DnsZone("host.example.org", secondary_zone)),
        "203.0.113.10",
        corefile,
    )

    cf = corefile.read_text()
    # Both domains get their own authoritative server block referencing their own zone file.
    assert "host.example.com:53 {" in cf
    assert "host.example.org:53 {" in cf
    assert str(primary_zone) in cf
    assert str(secondary_zone) in cf

    # Each zone file is authoritative for its own origin and serves the wildcard A at the public IP.
    assert "$ORIGIN host.example.com." in primary_zone.read_text()
    secondary_text = secondary_zone.read_text()
    assert "$ORIGIN host.example.org." in secondary_text
    assert "*   IN A    203.0.113.10" in secondary_text


def test_reload_coredns_for_domains_regenerates_zones_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        coredns = dns_mod.start_coredns(public_dns_zones(config, db), config.public_ip, config.coredns_corefile_path)
        set_active_coredns(coredns)
        try:
            first_proc = coredns.proc

            # Add a second public domain to the DB and reload: CoreDNS must now serve its zone too.
            upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
            assert reload_coredns_for_domains(config, db) is True

            cf = config.coredns_corefile_path.read_text()
            assert "host.example.org:53 {" in cf
            assert (config.zones_dir / "host.example.org.zone").exists()
            # restart() replaced the process so the new Corefile (new zone) takes effect.
            assert coredns.proc is not first_proc
        finally:
            set_active_coredns(None)


def test_reload_coredns_for_domains_noop_when_not_running(tmp_path: Path) -> None:
    set_active_coredns(None)
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        assert reload_coredns_for_domains(config, db) is False


# --- render_email_records / apply_email_records (email DNS block) -------------


def _dkim() -> list[DkimCname]:
    return [DkimCname(name="tok1._domainkey.z.example.com", target="tok1.dkim.amazonses.com")]


def test_render_email_records_includes_spf_dmarc_mx_a_dkim() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=_dkim(),
    )
    assert '@   IN TXT  "v=spf1 include:amazonses.com ~all"' in block
    assert '_dmarc   IN TXT  "v=DMARC1; p=quarantine"' in block
    assert "@   IN MX   10 mail.z.example.com." in block
    assert "mail.z.example.com.   IN A   203.0.113.9" in block
    assert "tok1._domainkey.z.example.com.   IN CNAME  tok1.dkim.amazonses.com." in block


def test_render_email_records_wraps_in_managed_markers() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[],
    )
    assert block.startswith("; --- openhost email records (managed) ---")
    assert block.rstrip().endswith("; --- end openhost email records ---")


def test_render_email_records_dmarc_rua_appended_when_set() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[],
        dmarc_rua="reports@example.com",
    )
    assert '_dmarc   IN TXT  "v=DMARC1; p=quarantine; rua=mailto:reports@example.com"' in block


def test_render_email_records_no_rua_by_default() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[],
    )
    assert "rua=" not in block


def test_render_email_records_strips_trailing_dots_on_mail_host_and_dkim() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com.",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[DkimCname(name="t._domainkey.z.example.com.", target="t.dkim.amazonses.com.")],
    )
    # Exactly one trailing dot each (no doubled dots).
    assert "mail.z.example.com.   IN A" in block
    assert "mail.z.example.com..   IN A" not in block
    assert "t._domainkey.z.example.com.   IN CNAME  t.dkim.amazonses.com." in block


def test_render_email_records_requires_inbound_mail_host() -> None:
    with pytest.raises(ValueError, match="inbound_mail_host is required"):
        render_email_records("z.example.com", inbound_mail_host="", inbound_mail_ip="203.0.113.9", dkim_cnames=[])


def test_render_email_records_requires_inbound_mail_ip() -> None:
    with pytest.raises(ValueError, match="inbound_mail_ip is required"):
        render_email_records(
            "z.example.com", inbound_mail_host="mail.z.example.com", inbound_mail_ip="", dkim_cnames=[]
        )


def test_render_email_records_multiple_dkim_cnames() -> None:
    block = render_email_records(
        "z.example.com",
        inbound_mail_host="mail.z.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[
            DkimCname(name="a._domainkey.z.example.com", target="a.dkim.amazonses.com"),
            DkimCname(name="b._domainkey.z.example.com", target="b.dkim.amazonses.com"),
        ],
    )
    assert "a._domainkey.z.example.com.   IN CNAME  a.dkim.amazonses.com." in block
    assert "b._domainkey.z.example.com.   IN CNAME  b.dkim.amazonses.com." in block


def test_apply_email_records_appends_block_and_bumps_serial(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)
    apply_email_records(
        zonefile,
        "app.example.com",
        inbound_mail_host="mail.app.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[DkimCname(name="t._domainkey.app.example.com", target="t.dkim.amazonses.com")],
    )
    content = zonefile.read_text()
    assert "101   ; serial" in content
    assert "v=spf1 include:amazonses.com" in content
    assert "@   IN MX   10 mail.app.example.com." in content
    # The original zone content is preserved (records appended, not replaced).
    assert "$ORIGIN app.example.com." in content


def test_apply_email_records_preserves_existing_records(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    original_a = "@   IN A    127.0.0.1"
    assert original_a in zonefile.read_text()
    apply_email_records(
        zonefile,
        "app.example.com",
        inbound_mail_host="mail.app.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[],
    )
    assert original_a in zonefile.read_text()
