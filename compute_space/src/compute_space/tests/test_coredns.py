"""The router's own DNS: what gets rendered, what CoreDNS is asked to serve, and the records."""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns.coredns_provider.coredns as dns_mod
from compute_space.config import DefaultConfig
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider.interface import DnsSettings
from compute_space.core.dns.coredns_provider.interface import DnsZone
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import ManagedZone
from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.dns.coredns_provider.interface import public_dns_zones
from compute_space.core.dns.router_records import publish_router_addresses
from compute_space.core.dns.settings import dns_settings_for
from compute_space.core.dns.settings import zones_for_domains
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import remove_record
from compute_space.core.domains import seed_domains
from compute_space.core.domains import upsert_record
from compute_space.db import init_db
from compute_space.tests.conftest import open_db

PUBLIC_IP = "203.0.113.10"


def _seed_dns_cfg(tmp_path: Path, *domains: Domain, **kw: Any) -> DefaultConfig:
    """A config whose DB is seeded with ``domains`` (primary first), for the DB-backed zone builder."""
    cfg = DefaultConfig(data_root_dir=str(tmp_path), **kw)
    cfg.make_all_dirs()
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
    return cfg


def _bare_settings(tmp_path: Path, container_gateway_ip: str | None = None) -> DnsSettings:
    """Settings pointing at ``tmp_path``, with no compute space behind them."""
    return DnsSettings(
        corefile_path=tmp_path / "Corefile",
        zonefile_path=tmp_path / "zonefile",
        zones_dir=tmp_path / "zones",
        public_ip=PUBLIC_IP,
        container_gateway_ip=container_gateway_ip,
    )


def _provider(config: DefaultConfig, zones: tuple[ManagedZone, ...]) -> InternalDnsProvider:
    return InternalDnsProvider(settings=dns_settings_for(config, PUBLIC_IP, container_gateway_ip=None), zones=zones)


def _serial(zonefile: Path) -> int:
    return int(zonefile.read_text().split("; serial")[0].strip().splitlines()[-1].strip())


# ─── bind address discovery ───


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

    assert dns_mod._coredns_bind_ip(PUBLIC_IP) == "10.0.0.5"
    assert fake_socket.addr == ("8.8.8.8", 80)


def test_coredns_bind_ip_falls_back_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(*args: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", raise_os_error)

    assert dns_mod._coredns_bind_ip(PUBLIC_IP) == PUBLIC_IP


# ─── the container-facing view ───


class _FakeProc:
    pid = 4242
    stdout = None
    # Report already-exited so CoreDnsProcess.restart() skips the terminate path.
    returncode = 0

    async def wait(self) -> int:
        return 0


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the spawn wholesale: it starts a log-streaming task that reads proc.stdout."""

    async def fake_spawn(*a: object, **k: object):  # type: ignore[no-untyped-def]
        async def _noop() -> None:
            return None

        task = asyncio.create_task(_noop())
        await task
        return _FakeProc(), task

    monkeypatch.setattr(dns_mod, "_spawn_coredns", fake_spawn)


def test_container_dns_view_rendered_when_gateway_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda gw: ["9.9.9.9"])

    settings = _bare_settings(tmp_path, container_gateway_ip="10.200.0.1")
    zone = DnsZone("app.example.com", settings.zonefile_path)
    dns_mod.write_coredns_config((zone,), settings, (), serial=1)

    cf = settings.corefile_path.read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf

    # The container zonefile answers the wildcard with the gateway, never the public IP.
    cz = zone.container_zonefile_path.read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert PUBLIC_IP not in cz


def test_container_dns_view_skipped_when_gateway_not_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)

    settings = _bare_settings(tmp_path, container_gateway_ip="10.200.0.1")
    zone = DnsZone("app.example.com", settings.zonefile_path)
    dns_mod.write_coredns_config((zone,), settings, (), serial=1)

    cf = settings.corefile_path.read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    assert not zone.container_zonefile_path.exists()


def test_container_view_forward_uses_discovered_resolvers_and_distinct_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The public view and the container view must bind different addresses (the default-route
    # source vs the gateway), and the container catch-all must forward to the discovered upstreams.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda gw: ["185.12.64.1", "1.1.1.1"])

    settings = _bare_settings(tmp_path, container_gateway_ip=CONTAINER_GATEWAY_IP)
    dns_mod.write_coredns_config((DnsZone("app.example.com", settings.zonefile_path),), settings, (), serial=1)
    cf = settings.corefile_path.read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert f"bind {CONTAINER_GATEWAY_IP}" in cf  # container view + catch-all
    assert "forward . 185.12.64.1 1.1.1.1" in cf
    # Catch-all is scoped to the container gateway only (never the public bind), so the public IP
    # is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert f"bind {CONTAINER_GATEWAY_IP}" in catch_all
    assert "bind 10.0.0.5" not in catch_all


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


def test_host_upstream_resolvers_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*a: object, **k: object) -> object:
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", raise_oserror)
    assert dns_mod._host_upstream_resolvers(CONTAINER_GATEWAY_IP) == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_host_upstream_resolvers_falls_back_when_only_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host using only the systemd-resolved stub (127.0.0.53) would leave the container view with
    # no forwardable upstream; we must fall back, never emit an empty/loopback forward (which would
    # be unreachable from the container).
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers(CONTAINER_GATEWAY_IP) == list(dns_mod._FALLBACK_UPSTREAM_DNS)


# ─── which zones get served ───


def test_zones_for_domains_covers_every_public_domain_and_skips_mdns(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="host.example.org", tls=True),
        Domain(name="myhost.local", tls=False, mdns=True),
    )
    with closing(open_db(config)) as db:
        zones = public_dns_zones(dns_settings_for(config, PUBLIC_IP), zones_for_domains(db))
    # The mDNS domain is excluded (served by the responder, not CoreDNS).
    assert [z.domain for z in zones] == ["host.example.com", "host.example.org"]
    # Primary keeps the legacy zonefile path; the secondary gets a per-domain file under zones/.
    assert zones[0].zonefile_path == config.coredns_zonefile_path
    assert zones[1].zonefile_path == config.zones_dir / "host.example.org.zone"


@pytest.mark.asyncio
async def test_start_writes_a_zone_block_and_file_per_public_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    await dns.start()

    cf = config.coredns_corefile_path.read_text()
    # Both domains get their own authoritative server block referencing their own zone file.
    assert "host.example.com:53 {" in cf
    assert "host.example.org:53 {" in cf

    # Each zone file is authoritative for its own origin and names this instance as its NS.
    assert "$ORIGIN host.example.com." in config.coredns_zonefile_path.read_text()
    secondary = (config.zones_dir / "host.example.org.zone").read_text()
    assert "$ORIGIN host.example.org." in secondary
    assert "@   IN NS   ns.host.example.org." in secondary


@pytest.mark.asyncio
async def test_adding_a_zone_regenerates_the_corefile_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        await dns.start()
        try:
            assert dns._coredns is not None
            first_proc = dns._coredns.proc

            upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
            assert await dns.add_zone(ManagedZone("host.example.org")) is True

            cf = config.coredns_corefile_path.read_text()
            assert "host.example.org:53 {" in cf
            assert (config.zones_dir / "host.example.org.zone").exists()
            # restart() replaced the process so the new Corefile (new zone) takes effect.
            assert dns._coredns.proc is not first_proc
        finally:
            await dns.stop()


@pytest.mark.asyncio
async def test_setting_an_unchanged_zone_set_does_not_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # /api/domains pushes the whole zone set on every change, including ones that don't touch it
    # (a cert status flip, an mDNS domain).  Restarting CoreDNS for those would drop DNS for no
    # reason, so an unchanged set has to be a no-op.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        await dns.start()
        try:
            assert dns._coredns is not None
            first_proc = dns._coredns.proc
            assert await dns.set_zones(zones_for_domains(db)) is False
            assert dns._coredns.proc is first_proc
        finally:
            await dns.stop()


@pytest.mark.asyncio
async def test_a_zone_change_before_start_is_stored_but_restarts_nothing(tmp_path: Path) -> None:
    # /api/domains can be served by a router with coredns_enabled off; the zone set still has to be
    # kept honest so a later start picks it up.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    assert await dns.add_zone(ManagedZone("host.example.org")) is False
    assert [z.zone for z in dns.zones] == ["host.example.com", "host.example.org"]


@pytest.mark.asyncio
async def test_removing_a_zone_discards_its_files_but_keeps_the_records(tmp_path: Path) -> None:
    # Records belong to no zone, so dropping one takes its rendered files and nothing else -- and
    # the surviving zone still serves everything that was written.
    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        dns.set_records("www", RecordType.A, ["198.51.100.7"])
        removed_zone = config.zones_dir / "host.example.org.zone"
        assert removed_zone.exists()

        remove_record(db, "host.example.org")
        await dns.remove_zone("host.example.org")

    assert not removed_zone.exists()
    assert [r.data for r in dns.records] == ["198.51.100.7"]
    assert "www   300  IN A  198.51.100.7" in config.coredns_zonefile_path.read_text()


@pytest.mark.asyncio
async def test_a_new_zone_serves_the_records_written_before_it_existed(tmp_path: Path) -> None:
    # The point of records that carry no zone: one that appears later renders from the same set as
    # every other, with nothing to backfill.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
        dns.set_records("www", RecordType.A, ["198.51.100.7"])

        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        await dns.add_zone(ManagedZone("host.example.org"))

    added = (config.zones_dir / "host.example.org.zone").read_text()
    assert "$ORIGIN host.example.org." in added
    assert "www   300  IN A  198.51.100.7" in added


# ─── records ───


def test_records_are_rendered_into_every_zone_file(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])

    for path in (config.coredns_zonefile_path, config.zones_dir / "host.example.org.zone"):
        assert "www   300  IN A  198.51.100.7" in path.read_text()


def test_setting_a_record_replaces_the_whole_rrset(tmp_path: Path) -> None:
    # Every publisher re-runs on boot, so a set has to replace what the last run wrote rather than
    # accumulate alongside it.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7", "198.51.100.8"])
    dns.set_records("www", RecordType.A, ["198.51.100.9"])

    content = config.coredns_zonefile_path.read_text()
    assert "198.51.100.9" in content
    assert "198.51.100.7" not in content
    assert "198.51.100.8" not in content


def test_deleting_a_record_leaves_the_others_alone_and_is_safe_to_repeat(tmp_path: Path) -> None:
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])
    dns.set_records("_acme-challenge", RecordType.TXT, ["tok"], ttl=60)

    dns.delete_records("_acme-challenge", RecordType.TXT)
    dns.delete_records("_acme-challenge", RecordType.TXT)  # cleanup re-runs; absent is not an error

    content = config.coredns_zonefile_path.read_text()
    assert "IN TXT" not in content
    assert "www   300  IN A  198.51.100.7" in content


def test_txt_data_is_quoted_so_the_zone_stays_parseable(tmp_path: Path) -> None:
    # An unquoted TXT token containing a space or a semicolon would be read as several strings or
    # as a comment, and CoreDNS would refuse the whole zone.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    dns.set_records("_acme-challenge", RecordType.TXT, ["tok en; not a comment"], ttl=60)

    assert '_acme-challenge   60  IN TXT  "tok en; not a comment"' in config.coredns_zonefile_path.read_text()


def test_router_addresses_are_published_as_ordinary_records(tmp_path: Path) -> None:
    # The apex, the ns glue and the wildcard are the same kind of thing as anything else in the
    # zone -- there is one way to publish a record.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    publish_router_addresses(dns, PUBLIC_IP)

    content = config.coredns_zonefile_path.read_text()
    for name in ("@", "ns", "*"):
        assert f"{name}   300  IN A  {PUBLIC_IP}" in content


def test_every_write_advances_the_serial_so_coredns_reloads(tmp_path: Path) -> None:
    # Two writes in the same second must not produce the same serial, or the second change is never
    # picked up.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))

    serials = []
    for i in range(3):
        dns.set_records("www", RecordType.A, [f"198.51.100.{i}"])
        serials.append(_serial(config.coredns_zonefile_path))

    assert serials == sorted(set(serials))


def test_an_unchanged_write_does_not_touch_the_zone(tmp_path: Path) -> None:
    # Boot re-publishes the same router addresses every time; bumping the serial for that would
    # have CoreDNS reload a file that did not change.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, zones_for_domains(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])
    before = _serial(config.coredns_zonefile_path)

    dns.set_records("www", RecordType.A, ["198.51.100.7"])

    assert _serial(config.coredns_zonefile_path) == before


def test_zone_caches_addresses_long_but_negative_answers_briefly(tmp_path: Path) -> None:
    # The zone default is what the wildcard A inherits, and that is the only thing keeping a
    # visitor able to reach the instance while CoreDNS is down during an update -- so it is
    # deliberately long. Negative caching stays short (RFC 2308 uses min(SOA MINIMUM, SOA TTL)),
    # which is what lets the NODATA left by a cleared challenge expire before the next renewal.
    settings = _bare_settings(tmp_path)
    dns_mod.write_coredns_config((DnsZone("app.example.com", settings.zonefile_path),), settings, (), serial=1)

    content = settings.zonefile_path.read_text()
    assert "$TTL 300" in content
    assert "60    ; minimum" in content
