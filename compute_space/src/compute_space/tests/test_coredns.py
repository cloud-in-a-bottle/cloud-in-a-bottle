from __future__ import annotations

import asyncio
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns.coredns_provider.coredns as dns_mod
from compute_space.config import DefaultConfig
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.interface import DnsSettings
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import ManagedZone
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.settings import dns_settings_for
from compute_space.core.dns.settings import zones_for_domains
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import remove_record
from compute_space.core.domains import seed_domains
from compute_space.core.domains import upsert_record
from compute_space.db import init_db
from compute_space.tests.conftest import open_db


def _bare_settings(tmp_path: Path, *, gateway: str | None = None) -> DnsSettings:
    """Settings pointing at ``tmp_path``, for the rendering functions on their own."""
    return DnsSettings(
        corefile_path=tmp_path / "Corefile",
        zonefile_path=tmp_path / "zonefile",
        zones_dir=tmp_path / "zones",
        public_ip="203.0.113.10",
        get_serial=lambda db: None,
        set_serial=lambda db, serial: None,
        container_gateway_ip=gateway,
    )


def _seed_dns_cfg(tmp_path: Path, *domains: Domain, **kw: Any) -> DefaultConfig:
    """A config whose DB is seeded with ``domains``, primary first."""
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
    # Report already-exited so CoreDnsProcess.restart() skips the terminate path.
    returncode = 0

    async def wait(self) -> int:
        return 0


def _settings(config: DefaultConfig) -> DnsSettings:
    """Settings with no container view, as dev/CI has no bindable gateway."""
    return dns_settings_for(config, config.public_ip or "203.0.113.10", container_gateway_ip=None)


def _provider(config: DefaultConfig, zones: tuple[ManagedZone, ...]) -> InternalDnsProvider:
    def db_provider() -> Iterator[sqlite3.Connection]:
        with closing(open_db(config)) as db:
            yield db

    return InternalDnsProvider(settings=_settings(config), db_provider=db_provider, zones=zones)


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the spawn wholesale: it starts a log-streaming task that reads proc.stdout."""

    async def fake_spawn(*a: object, **k: object):  # type: ignore[no-untyped-def]
        async def _noop() -> None:
            return None

        task = asyncio.create_task(_noop())
        await task
        return _FakeProc(), task

    monkeypatch.setattr(dns_mod, "_spawn_coredns", fake_spawn)


@pytest.mark.asyncio
async def test_container_dns_view_rendered_when_gateway_bindable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda gw: ["9.9.9.9"])
    _stub_spawn(monkeypatch)

    settings = _bare_settings(tmp_path, gateway="10.200.0.1")
    zonefile = tmp_path / "zonefile"
    await dns_mod.start_coredns((dns_mod.DnsZone("app.example.com", zonefile),), settings)

    cf = settings.corefile_path.read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf

    # The public zone answers only from stored records; the container view answers the wildcard
    # itself, so the gateway IP is baked into its file.
    assert "203.0.113.10" not in zonefile.read_text()
    container_zone = tmp_path / "zonefile.container"
    assert container_zone.exists()
    cz = container_zone.read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert "203.0.113.10" not in cz


@pytest.mark.asyncio
async def test_container_dns_view_skipped_when_gateway_not_bindable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_spawn(monkeypatch)

    settings = _bare_settings(tmp_path, gateway="10.200.0.1")
    await dns_mod.start_coredns((dns_mod.DnsZone("app.example.com", tmp_path / "zonefile"),), settings)

    cf = settings.corefile_path.read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    # No container zonefile written.
    assert not (tmp_path / "zonefile.container").exists()


def test_host_upstream_resolvers_filters_loopback_and_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(
        "nameserver 127.0.0.53\n"
        f"nameserver {CONTAINER_GATEWAY_IP}\n"
        "nameserver 185.12.64.1\n"
        "nameserver 1.1.1.1\n"
        "search example.com\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers(CONTAINER_GATEWAY_IP) == ["185.12.64.1", "1.1.1.1"]


def test_host_upstream_resolvers_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*a: object, **k: object) -> object:
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", raise_oserror)
    assert dns_mod._host_upstream_resolvers(CONTAINER_GATEWAY_IP) == list(dns_mod._FALLBACK_UPSTREAM_DNS)


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
    assert dns_mod._host_upstream_resolvers(CONTAINER_GATEWAY_IP) == list(dns_mod._FALLBACK_UPSTREAM_DNS)


@pytest.mark.asyncio
async def test_container_view_forward_uses_discovered_resolvers_and_distinct_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The public view and the container view must bind different addresses (the
    # default-route source vs the gateway), and the container catch-all must
    # forward to the discovered upstreams.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda gw: ["185.12.64.1", "1.1.1.1"])
    _stub_spawn(monkeypatch)

    settings = _bare_settings(tmp_path, gateway=CONTAINER_GATEWAY_IP)
    await dns_mod.start_coredns((dns_mod.DnsZone("app.example.com", tmp_path / "zonefile"),), settings)
    cf = settings.corefile_path.read_text()

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
        zones = public_dns_zones(_settings(config), zones_for_domains(db))
    # The mDNS domain is excluded (served by the responder, not CoreDNS).
    assert [z.domain for z in zones] == ["host.example.com", "host.example.org"]
    # Primary keeps the legacy zonefile path; the secondary gets a per-domain file under zones/.
    assert zones[0].zonefile_path == config.coredns_zonefile_path
    assert zones[1].zonefile_path == config.zones_dir / "host.example.org.zone"


@pytest.mark.asyncio
async def test_start_coredns_writes_a_zone_block_and_file_per_public_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_spawn(monkeypatch)

    settings = _bare_settings(tmp_path)
    primary_zone = tmp_path / "zonefile"
    secondary_zone = tmp_path / "zones" / "host.example.org.zone"
    await dns_mod.start_coredns(
        (DnsZone("host.example.com", primary_zone), DnsZone("host.example.org", secondary_zone)), settings
    )

    cf = settings.corefile_path.read_text()
    # Both domains get their own authoritative server block referencing their own zone file.
    assert "host.example.com:53 {" in cf
    assert "host.example.org:53 {" in cf
    assert str(primary_zone) in cf
    assert str(secondary_zone) in cf

    # Each zone file is authoritative for its own origin and names this instance as its NS.
    assert "$ORIGIN host.example.com." in primary_zone.read_text()
    secondary_text = secondary_zone.read_text()
    assert "$ORIGIN host.example.org." in secondary_text
    assert "@   IN NS   ns.host.example.org." in secondary_text


@pytest.mark.asyncio
async def test_adding_a_zone_regenerates_the_corefile_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        await dns.start(db)
        try:
            first_proc = dns._coredns.proc

            upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
            assert await dns.add_zone(db, ManagedZone("host.example.org")) is True

            cf = config.coredns_corefile_path.read_text()
            assert "host.example.org:53 {" in cf
            assert (config.zones_dir / "host.example.org.zone").exists()
            # restart() replaced the process so the new Corefile (new zone) takes effect.
            assert dns._coredns.proc is not first_proc
        finally:
            await dns.stop()


@pytest.mark.asyncio
async def test_a_zone_change_before_start_is_stored_but_restarts_nothing(tmp_path: Path) -> None:
    # /api/domains can be served by a router with coredns_enabled off; the zone set still has to
    # be kept honest so a later start picks it up.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        assert await dns.add_zone(db, ManagedZone("host.example.org")) is False
        assert [z.zone for z in dns.zones] == ["host.example.com", "host.example.org"]


def test_zone_caches_addresses_long_but_negative_answers_briefly(tmp_path: Path) -> None:
    # The zone default is what the wildcard A inherits, and that is the only thing keeping a
    # visitor able to reach the instance while CoreDNS is down during an update -- so it is
    # deliberately long. Negative caching stays short (RFC 2308 uses min(SOA MINIMUM, SOA TTL)),
    # which is what lets the NODATA left by a cleared challenge expire before the next renewal.
    zonefile = tmp_path / "zonefile"
    dns_mod.write_coredns_config((dns_mod.DnsZone("app.example.com", zonefile),), _bare_settings(tmp_path))
    content = zonefile.read_text()
    assert "$TTL 300" in content
    assert "60    ; minimum" in content


def test_stored_records_are_rendered_into_the_zone_file(tmp_path: Path) -> None:
    config = _seed_dns_cfg(tmp_path, Domain(name="app.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        store.append_records(db, [DnsRecord("www", "A", 300, "198.51.100.7")])
        zone = public_dns_zones(_settings(config), zones_for_domains(db))[0]

        dns_mod.write_zone_file(zone, _settings(config), db)

        content = zone.zonefile_path.read_text()
        assert "www   300  IN A  198.51.100.7" in content


def test_regenerating_a_zone_keeps_its_stored_records(tmp_path: Path) -> None:
    # A domain change or an IP move rewrites the file wholesale, so anything an app wrote has to
    # come back from the DB rather than surviving in the file.
    config = _seed_dns_cfg(tmp_path, Domain(name="app.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        store.append_records(db, [DnsRecord("www", "A", 300, "198.51.100.7")])
        zones = public_dns_zones(_settings(config), zones_for_domains(db))

        dns_mod.write_coredns_config(zones, _settings(config), db=db)

        content = zones[0].zonefile_path.read_text()
        assert "www   300  IN A  198.51.100.7" in content


def test_every_render_advances_the_serial_so_coredns_reloads(tmp_path: Path) -> None:
    # Two renders in the same second must not produce the same serial, or the second change is
    # never picked up.
    config = _seed_dns_cfg(tmp_path, Domain(name="app.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        zone = public_dns_zones(_settings(config), zones_for_domains(db))[0]
        serials = []
        for _ in range(3):
            dns_mod.write_zone_file(zone, _settings(config), db)
            serials.append(int(zone.zonefile_path.read_text().split("; serial")[0].strip().splitlines()[-1].strip()))
    assert serials == sorted(set(serials))


@pytest.mark.asyncio
async def test_setting_an_unchanged_zone_set_does_not_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # /api/domains pushes the whole zone set on every change, including ones that don't touch it
    # (a cert status flip, an mDNS domain).  Restarting CoreDNS for those would drop DNS for no
    # reason, so an unchanged set has to be a no-op.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        await dns.start(db)
        try:
            first_proc = dns._coredns.proc
            assert await dns.set_zones(db, zones_for_domains(db)) is False
            assert dns._coredns.proc is first_proc
        finally:
            await dns.stop()


@pytest.mark.asyncio
async def test_removing_a_zone_discards_its_files_but_keeps_the_records(tmp_path: Path) -> None:
    # Records belong to no zone, so dropping one takes its rendered files and nothing else -- and
    # the surviving zone still serves everything that was written.
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="host.example.org", tls=True),
        public_ip="203.0.113.10",
    )
    with closing(open_db(config)) as db:
        store.append_records(db, [DnsRecord("www", "A", 300, "198.51.100.7")])
        dns_mod.write_coredns_config(
            public_dns_zones(_settings(config), zones_for_domains(db)), _settings(config), db=db
        )
        removed_zone = config.zones_dir / "host.example.org.zone"
        assert removed_zone.exists()

        dns = _provider(config, zones_for_domains(db))
        remove_record(db, "host.example.org")
        await dns.remove_zone(db, "host.example.org")

        assert not removed_zone.exists()
        assert store.all_records(db) == [DnsRecord("www", "A", 300, "198.51.100.7")]
        assert "www   300  IN A  198.51.100.7" in config.coredns_zonefile_path.read_text()


@pytest.mark.asyncio
async def test_a_new_zone_serves_the_records_written_before_it_existed(tmp_path: Path) -> None:
    # The point of storing records with no zone: a zone that appears later renders from the same
    # rows as every other one, with nothing to backfill.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True), public_ip="203.0.113.10")
    with closing(open_db(config)) as db:
        store.append_records(db, [DnsRecord("www", "A", 300, "198.51.100.7")])

        dns = _provider(config, zones_for_domains(db))
        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        await dns.add_zone(db, ManagedZone("host.example.org"))

        added = (config.zones_dir / "host.example.org.zone").read_text()
        assert "$ORIGIN host.example.org." in added
        assert "www   300  IN A  198.51.100.7" in added
