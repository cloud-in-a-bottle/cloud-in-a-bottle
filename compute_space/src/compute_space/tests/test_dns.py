from __future__ import annotations

import socket
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns.coredns as dns_mod
import compute_space.core.dns.zonefile as zonefile_mod
from compute_space.config import DefaultConfig
from compute_space.core.dns.coredns import DnsZone
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.coredns import reload_coredns_for_domains
from compute_space.core.dns.coredns import set_active_coredns
from compute_space.core.dns.records import DnsRecord
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


def test_zone_caches_addresses_long_but_acme_challenges_briefly(tmp_path: Path) -> None:
    # The wildcard TTL is what a visitor's resolver caches, and it is the only
    # thing keeping them able to reach the instance while CoreDNS is down during an
    # update -- so it is deliberately long. ACME challenge records must NOT inherit
    # it: a renewal would then find the CA (and our own propagation check) served
    # the previous run's token out of a resolver cache.
    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod._write_coredns_config(
        (dns_mod.DnsZone("app.example.com", zonefile),),
        "203.0.113.10",
        corefile,
        container_gateway_ip=None,
        serve_public=True,
    )
    content = zonefile.read_text()
    assert "$TTL 300" in content
    # Negative caching stays short (RFC 2308 uses min(SOA MINIMUM, SOA TTL)), which
    # is what lets the NODATA left by a challenge cleanup expire before the next renewal.
    assert "60    ; minimum" in content


def test_write_coredns_config_seeds_a_zone_file_but_never_overwrites_one(tmp_path: Path) -> None:
    # The zone file is the source of truth for its records once it exists: apps and the cert path
    # both write into it. Re-rendering the template over it -- which a domain add/remove does --
    # would silently drop everything but the three records the template knows about.
    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    zone = dns_mod.DnsZone("app.example.com", zonefile)
    args = dict(corefile_path=corefile, container_gateway_ip=None, serve_public=True)
    dns_mod._write_coredns_config((zone,), "203.0.113.10", **args)

    zonefile_mod.append_records(
        zonefile, "app.example.com", [DnsRecord(name="www", type="A", ttl=300, data="198.51.100.7")]
    )

    dns_mod._write_coredns_config((zone,), "203.0.113.10", **args)

    records = zonefile_mod.read_records(zonefile, "app.example.com")
    assert ("www", "A", "198.51.100.7") in [(r.name, r.type, r.data) for r in records]


def test_write_coredns_config_repoints_router_records_on_an_ip_change(tmp_path: Path) -> None:
    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    zone = dns_mod.DnsZone("app.example.com", zonefile)
    args = dict(corefile_path=corefile, container_gateway_ip=None, serve_public=True)
    dns_mod._write_coredns_config((zone,), "203.0.113.10", **args)
    zonefile_mod.append_records(
        zonefile, "app.example.com", [DnsRecord(name="www", type="A", ttl=300, data="198.51.100.7")]
    )

    dns_mod._write_coredns_config((zone,), "203.0.113.99", **args)

    by_name = {(r.name, r.type): r.data for r in zonefile_mod.read_records(zonefile, "app.example.com")}
    # Router-owned records follow the new address...
    assert by_name[("@", "A")] == "203.0.113.99"
    assert by_name[("*", "A")] == "203.0.113.99"
    assert by_name[("ns", "A")] == "203.0.113.99"
    # ...and an app's record is left exactly where it was.
    assert by_name[("www", "A")] == "198.51.100.7"


def test_container_view_runs_without_any_public_zones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A space using an external DNS provider still needs the hairpin, so the container view is
    # generated with no public server block and no public IP at all.
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["9.9.9.9"])

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod._write_coredns_config(
        (dns_mod.DnsZone("app.example.com", zonefile),),
        None,
        corefile,
        container_gateway_ip="10.200.0.1",
        serve_public=False,
    )

    cf = corefile.read_text()
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf
    # No authoritative view, and no public zone file written.
    assert cf.count("bind ") == cf.count("bind 10.200.0.1")
    assert not zonefile.exists()
    assert (tmp_path / "zonefile.container").exists()


def test_serving_public_zones_requires_a_public_ip(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="public IP is required"):
        dns_mod._write_coredns_config(
            (dns_mod.DnsZone("app.example.com", tmp_path / "zonefile"),),
            None,
            tmp_path / "Corefile",
            container_gateway_ip=None,
            serve_public=True,
        )


def test_coredns_is_needed_for_either_view(monkeypatch: pytest.MonkeyPatch) -> None:
    zones = (dns_mod.DnsZone("app.example.com", Path("/tmp/zonefile")),)
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    assert dns_mod.coredns_is_needed(zones, serve_public=True, container_gateway_ip=None) is True
    # No public zones, but the hairpin still needs it.
    assert dns_mod.coredns_is_needed((), serve_public=False, container_gateway_ip="10.200.0.1") is True

    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    # Nothing to serve on either side: don't start it at all.
    assert dns_mod.coredns_is_needed((), serve_public=False, container_gateway_ip="10.200.0.1") is False
    assert dns_mod.coredns_is_needed(zones, serve_public=False, container_gateway_ip=None) is False
