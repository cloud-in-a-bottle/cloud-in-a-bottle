from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns as dns_mod
from compute_space.config import DefaultConfig
from compute_space.core.dns import DnsZone
from compute_space.core.dns import TxtRecord
from compute_space.core.dns import append_txt_records
from compute_space.core.dns import clear_txt
from compute_space.core.dns import dns_zones
from compute_space.core.dns import reload_coredns_for_domains
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
        seed_domains(db, primary, [DomainRecord(d.name, d.tls) for d in domains[1:]])
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


def _stub_bindable(monkeypatch: pytest.MonkeyPatch, *local: str) -> None:
    """Only ``local`` addresses sit on an interface of the (fake) host; CoreDNS binds those and
    drops the rest."""
    monkeypatch.setattr(dns_mod, "is_bindable", lambda ip: ip in local)


def test_coredns_binds_every_local_address(monkeypatch: pytest.MonkeyPatch) -> None:
    # A directly-assigned public address carries the DNS-01 and delegated-NS queries the private one
    # never sees, so a multi-homed box has to answer on both rather than pick one.
    _stub_bindable(monkeypatch, "10.0.0.5", "203.0.113.10", "fd00::5")
    assert dns_mod._coredns_bind_ips("10.0.0.5", "fd00::5", "203.0.113.10") == ("10.0.0.5", "203.0.113.10", "fd00::5")


def test_coredns_bind_skips_addresses_not_on_an_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    # Behind NAT the configured public IP lives on the router, so binding it would crash CoreDNS.
    _stub_bindable(monkeypatch, "10.0.0.5")
    assert dns_mod._coredns_bind_ips("10.0.0.5", None, "203.0.113.10") == ("10.0.0.5",)
    # ...and on a cloud VM the public address is the only one there is.
    _stub_bindable(monkeypatch, "203.0.113.10")
    assert dns_mod._coredns_bind_ips(None, None, "203.0.113.10") == ("203.0.113.10",)
    # One address reachable both ways is still bound once — a repeated bind is an error to CoreDNS.
    _stub_bindable(monkeypatch, "10.0.0.5")
    assert dns_mod._coredns_bind_ips("10.0.0.5", None, "10.0.0.5") == ("10.0.0.5",)


def test_coredns_bind_raises_when_no_address_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty `bind` would make CoreDNS listen on the wildcard and collide with aardvark-dns, so
    # this must fail loudly instead.
    _stub_bindable(monkeypatch)
    with pytest.raises(RuntimeError, match="no local address"):
        dns_mod._coredns_bind_ips("10.0.0.5", None, "203.0.113.10")


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
    assert '_acme-challenge   60  IN TXT  "base-value"' in content
    assert '_acme-challenge   60  IN TXT  "wildcard-value"' in content
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
    assert '_acme-challenge.app.example.com.   60  IN TXT  "v"' in content
    # Not doubled up into _acme-challenge.app.example.com.app.example.com.
    assert "app.example.com.app.example.com" not in content


def test_clear_txt_removes_records(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    clear_txt(zonefile)

    assert "IN TXT" not in zonefile.read_text()


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
    _stub_bindable(monkeypatch, "203.0.113.10", "10.200.0.1")
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
    # Public view binds the host's own address; container view binds the gateway.
    assert "bind 203.0.113.10" in cf
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
    _stub_bindable(monkeypatch, "203.0.113.10")
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
    # The public view and the container view must bind different addresses (the box's own
    # address vs the gateway), and the container catch-all must forward to the discovered
    # upstreams.
    _stub_bindable(monkeypatch, "203.0.113.10", "10.200.0.1")
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["185.12.64.1", "1.1.1.1"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    dns_mod.start_coredns((dns_mod.DnsZone("app.example.com", tmp_path / "zonefile"),), "203.0.113.10", corefile)
    cf = corefile.read_text()

    assert "bind 203.0.113.10" in cf  # public/authoritative view
    assert "bind 10.200.0.1" in cf  # container view + catch-all
    assert "forward . 185.12.64.1 1.1.1.1" in cf
    # Catch-all is scoped to the container gateway only (never the public bind),
    # so the public IP is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert "bind 10.200.0.1" in catch_all
    assert "bind 203.0.113.10" not in catch_all


def test_dns_zones_covers_every_domain_including_local(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="host.example.org", tls=True),
        Domain(name="myhost.local", tls=False),
    )
    with closing(open_db(config)) as db:
        zones = dns_zones(config, db)
    # CoreDNS now serves the `.local` zone too (for conditional-forwarder clients, e.g. Windows).
    assert [z.domain for z in zones] == ["host.example.com", "host.example.org", "myhost.local"]
    # Primary keeps the legacy zonefile path; the others get a per-domain file under zones/.
    assert zones[0].zonefile_path == config.coredns_zonefile_path
    assert zones[1].zonefile_path == config.zones_dir / "host.example.org.zone"


def test_local_zone_uses_private_ip_public_zone_uses_public_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_bindable(monkeypatch, "192.168.1.50", "fd00::5")
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    public_zone = tmp_path / "public.zone"
    local_zone = tmp_path / "local.zone"
    dns_mod.start_coredns(
        (DnsZone("host.example.com", public_zone), DnsZone("myhost.local", local_zone)),
        "203.0.113.10",
        corefile,
        private_ip="192.168.1.50",
    )
    assert "*   IN A    203.0.113.10" in public_zone.read_text()  # public → public IP
    # CoreDNS stays authoritative for the `.local` zone (so `*.myhost.local` resolves for a
    # conditional-forwarder client, e.g. Windows), answering with the private IP.
    assert "myhost.local:53" in corefile.read_text()
    assert "*   IN A    192.168.1.50" in local_zone.read_text()


def test_local_zone_gets_aaaa_and_v6_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_bindable(monkeypatch, "192.168.1.50", "fd00::5")
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    public_zone = tmp_path / "public.zone"
    local_zone = tmp_path / "local.zone"
    dns_mod.start_coredns(
        (DnsZone("host.example.com", public_zone), DnsZone("myhost.local", local_zone)),
        "203.0.113.10",
        corefile,
        private_ip="192.168.1.50",
        private_ip6="fd00::5",
    )
    assert "*   IN AAAA fd00::5" in local_zone.read_text()
    # Public zones resolve to public_ip, which has no v6 counterpart — no AAAA there.
    assert "AAAA" not in public_zone.read_text()
    # Binding the v6 address too lets a v6-only client use us as a conditional forwarder.
    assert "bind 192.168.1.50 fd00::5" in corefile.read_text()


def test_publishable_private_ip6_requires_a_listening_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    # Publishing an AAAA nothing answers on makes clients (which prefer v6) hang on connect and
    # fall back — the exact stall this whole change set removes.
    monkeypatch.setattr(dns_mod, "private_ip6", lambda: "fd00::5")
    monkeypatch.setattr(dns_mod, "is_reachable", lambda ip, port: False)
    assert dns_mod.publishable_private_ip6() is None

    monkeypatch.setattr(dns_mod, "is_reachable", lambda ip, port: True)
    assert dns_mod.publishable_private_ip6() == "fd00::5"


def test_local_zone_dropped_when_no_private_ip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With no private IP there is no honest answer for a `.local` name, and handing LAN clients the
    # public IP sends their traffic off-box — so the zone is not published at all.
    _stub_bindable(monkeypatch, "203.0.113.10")
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    public_zone = tmp_path / "public.zone"
    local_zone = tmp_path / "local.zone"
    dns_mod.start_coredns(
        (DnsZone("host.example.com", public_zone), DnsZone("myhost.local", local_zone)),
        "203.0.113.10",
        corefile,
        private_ip=None,
    )
    assert "myhost.local:53" not in corefile.read_text() and not local_zone.exists()
    assert "host.example.com:53" in corefile.read_text()  # public zones unaffected


def test_start_coredns_writes_a_zone_block_and_file_per_public_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_bindable(monkeypatch, "203.0.113.10")
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
    _stub_bindable(monkeypatch, "203.0.113.10")
    _stub_popen(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        coredns = dns_mod.start_coredns(dns_zones(config, db), config.public_ip, config.coredns_corefile_path)
        set_active_coredns(coredns)
        try:
            first_proc = coredns.proc

            # Add a second public domain to the DB and reload: CoreDNS must now serve its zone too.
            upsert_record(db, DomainRecord("host.example.org", tls=True))
            assert reload_coredns_for_domains(config, db) is True

            cf = config.coredns_corefile_path.read_text()
            assert "host.example.org:53 {" in cf
            assert (config.zones_dir / "host.example.org.zone").exists()
            # restart() replaced the process so the new Corefile (new zone) takes effect.
            assert coredns.proc is not first_proc
        finally:
            set_active_coredns(None)


def test_reload_preserves_in_flight_challenge_txt_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_bindable(monkeypatch, "203.0.113.10")
    _stub_popen(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        coredns = dns_mod.start_coredns(dns_zones(config, db), config.public_ip, config.coredns_corefile_path)
        set_active_coredns(coredns)
        try:
            zonefile = dns_zones(config, db)[0].zonefile_path
            append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge", record_value="token")])

            assert reload_coredns_for_domains(config, db) is True

            assert '_acme-challenge   60  IN TXT  "token"' in zonefile.read_text()
        finally:
            set_active_coredns(None)


def test_reload_coredns_for_domains_noop_when_not_running(tmp_path: Path) -> None:
    set_active_coredns(None)
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        assert reload_coredns_for_domains(config, db) is False


def test_zone_caches_addresses_long_but_acme_challenges_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wildcard TTL is what a visitor's resolver caches, and it is the only
    # thing keeping them able to reach the instance while CoreDNS is down during an
    # update -- so it is deliberately long. ACME challenge records must NOT inherit
    # it: a renewal would then find the CA (and our own propagation check) served
    # the previous run's token out of a resolver cache.
    _stub_bindable(monkeypatch, "203.0.113.10")
    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod._write_coredns_config(
        (dns_mod.DnsZone("app.example.com", zonefile),), "203.0.113.10", corefile, container_gateway_ip=None
    )
    content = zonefile.read_text()
    assert "$TTL 300" in content
    # Negative caching stays short (RFC 2308 uses min(SOA MINIMUM, SOA TTL)), which
    # is what lets the NODATA left by clear_txt expire before the next renewal.
    assert "60    ; minimum" in content

    dns_mod.append_txt_records(zonefile, [dns_mod.TxtRecord(record_name="_acme-challenge", record_value="tok")])
    assert '_acme-challenge   60  IN TXT  "tok"' in zonefile.read_text()
    # And the explicit TTL column must not stop clear_txt from finding the record.
    dns_mod.clear_txt(zonefile)
    assert "IN TXT" not in zonefile.read_text()
