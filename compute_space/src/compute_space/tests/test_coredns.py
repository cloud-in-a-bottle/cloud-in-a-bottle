"""The router's own DNS: what gets rendered, what CoreDNS is asked to serve, and the records."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.dns.coredns_provider.coredns as dns_mod
from compute_space.config import DefaultConfig
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider.interface import DnsNotEnabled
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.dns.router_records import publish_router_addresses
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import effective_domains
from compute_space.core.domains import remove_record
from compute_space.core.domains import seed_domains
from compute_space.core.domains import upsert_record
from compute_space.db import init_db
from compute_space.tests.conftest import open_db
from compute_space.tests.conftest import stub_coredns_spawn

PUBLIC_IP = "203.0.113.10"
# The local address CoreDNS binds; the compute space works it out and passes it in.
BIND_IP = "10.0.0.5"
APP_ZONE = "app.example.com"
# Passed explicitly wherever a render is asserted on, so the Corefile doesn't depend on whatever
# nameservers the machine running the tests happens to have.
TEST_UPSTREAM = ("192.0.2.53",)


def _seed_dns_cfg(tmp_path: Path, *domains: Domain, **kw: Any) -> DefaultConfig:
    """A config whose DB is seeded with ``domains`` (primary first), for the DB-backed zone builder."""
    cfg = DefaultConfig(data_root_dir=str(tmp_path), **kw)
    cfg.make_all_dirs()
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
    return cfg


def _zonefile(config: DefaultConfig, domain: str) -> Path:
    return config.zones_dir / f"{domain}.zone"


def _render(tmp_path: Path, container_gateway_ip: str | None = None) -> dict[str, Any]:
    """Rendering kwargs pointing at ``tmp_path``, with no compute space behind them."""
    return {
        "corefile_path": tmp_path / "Corefile",
        "zones_dir": tmp_path / "zones",
        "bind_ip": BIND_IP,
        "container_gateway_ip": container_gateway_ip,
        "upstream_dns": TEST_UPSTREAM,
    }


def _app_zonefile(tmp_path: Path) -> Path:
    return tmp_path / "zones" / f"{APP_ZONE}.zone"


async def _serve(dns: InternalDnsProvider, zones: tuple[str, ...]) -> None:
    """Bring a provider up the way boot does: one zone at a time, no bulk setter."""
    for zone in zones:
        await dns.add_zone(zone)


def _zones(db: sqlite3.Connection) -> tuple[str, ...]:
    """The zone set the app derives from its domains: every public (non-mDNS) domain."""
    return tuple(d.name_no_port for d in effective_domains(db) if not d.mdns)


def _provider(config: DefaultConfig, zones: tuple[str, ...] = ()) -> InternalDnsProvider:
    return InternalDnsProvider(
        corefile_path=config.coredns_corefile_path,
        zones_dir=config.zones_dir,
        bind_ip=BIND_IP,
        zones=zones,
    )


def _serial(zonefile: Path) -> int:
    return int(zonefile.read_text().split("; serial")[0].strip().splitlines()[-1].strip())


# ─── the container-facing view ───


def test_container_dns_view_rendered_when_a_gateway_is_given(tmp_path: Path) -> None:
    render = _render(tmp_path, container_gateway_ip="10.200.0.1")
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)

    cf = render["corefile_path"].read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert f"forward . {' '.join(TEST_UPSTREAM)}" in cf

    # The container zonefile answers the wildcard with the gateway, never the public IP.
    cz = _app_zonefile(tmp_path).with_suffix(".zone.container").read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert PUBLIC_IP not in cz


def test_upstream_resolvers_come_from_the_host(tmp_path: Path) -> None:
    # Hardcoding public resolvers breaks a split-horizon corporate resolver, a VPN's, or a cloud
    # VPC's internal zone, and routes every container query past the resolver the operator set.
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text("search example.com\nnameserver 10.0.0.53\nnameserver 10.0.0.54\n")

    assert dns_mod.host_upstream_resolvers(resolv_conf=resolv_conf) == ("10.0.0.53", "10.0.0.54")


def test_unusable_host_resolvers_are_dropped(tmp_path: Path) -> None:
    # Loopback is the systemd-resolved stub, which the container netns can't reach; the gateway is
    # this CoreDNS, so forwarding there loops.
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text(
        f"nameserver 127.0.0.53\nnameserver ::1\nnameserver {CONTAINER_GATEWAY_IP}\nnameserver 10.0.0.53\n"
    )

    resolvers = dns_mod.host_upstream_resolvers(CONTAINER_GATEWAY_IP, resolv_conf=resolv_conf)

    assert resolvers == ("10.0.0.53",)


@pytest.mark.parametrize("contents", ["", "nameserver 127.0.0.53\n"])
def test_public_resolvers_are_the_fallback_not_the_default(tmp_path: Path, contents: str) -> None:
    # Nothing usable left (or no resolv.conf at all): containers resolving via the wrong servers
    # still beats containers resolving nothing.
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text(contents)

    assert dns_mod.host_upstream_resolvers(resolv_conf=resolv_conf) == dns_mod.FALLBACK_UPSTREAM_DNS
    assert dns_mod.host_upstream_resolvers(resolv_conf=tmp_path / "absent") == dns_mod.FALLBACK_UPSTREAM_DNS


def test_container_dns_view_skipped_without_a_gateway(tmp_path: Path) -> None:
    # None is what boot passes when the openhost0 interface isn't there to bind.
    render = _render(tmp_path, container_gateway_ip=None)
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)

    cf = render["corefile_path"].read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    assert not _app_zonefile(tmp_path).with_suffix(".zone.container").exists()


def test_container_view_forward_and_distinct_bind(tmp_path: Path) -> None:
    # The public view and the container view must bind different addresses (the default-route
    # source vs the gateway), and the container catch-all must forward to the upstreams.
    render = _render(tmp_path, container_gateway_ip=CONTAINER_GATEWAY_IP)
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)
    cf = render["corefile_path"].read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert f"bind {CONTAINER_GATEWAY_IP}" in cf  # container view + catch-all
    assert f"forward . {' '.join(TEST_UPSTREAM)}" in cf
    # Catch-all is scoped to the container gateway only (never the public bind), so the public IP
    # is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert f"bind {CONTAINER_GATEWAY_IP}" in catch_all
    assert "bind 10.0.0.5" not in catch_all


# ─── which zones get served ───


def test_zone_set_covers_every_public_domain_and_skips_mdns(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path,
        Domain(name="host.example.com", tls=True),
        Domain(name="host.example.org", tls=True),
        Domain(name="myhost.local", tls=False, mdns=True),
    )
    with closing(open_db(config)) as db:
        zones = _zones(db)
    # The mDNS domain is excluded (served by the responder, not CoreDNS).
    assert list(zones) == ["host.example.com", "host.example.org"]
    # Each zone renders to its own file under zones/.
    assert [dns_mod._zonefile_path(config.zones_dir, z) for z in zones] == [
        _zonefile(config, "host.example.com"),
        _zonefile(config, "host.example.org"),
    ]


@pytest.mark.asyncio
async def test_start_writes_a_zone_block_and_file_per_public_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_coredns_spawn(monkeypatch)

    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        zones = _zones(db)
    dns = _provider(config)
    await _serve(dns, zones)
    try:
        cf = config.coredns_corefile_path.read_text()
        # Both domains get their own authoritative server block referencing their own zone file.
        assert "host.example.com:53 {" in cf
        assert "host.example.org:53 {" in cf

        # Each zone file is authoritative for its own origin and names this instance as its NS.
        assert "$ORIGIN host.example.com." in _zonefile(config, "host.example.com").read_text()
        secondary = (_zonefile(config, "host.example.org")).read_text()
        assert "$ORIGIN host.example.org." in secondary
        assert "@   IN NS   ns.host.example.org." in secondary
    finally:
        await dns.cleanup()


@pytest.mark.asyncio
async def test_adding_a_zone_regenerates_the_corefile_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_coredns_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config)
        await _serve(dns, _zones(db))
        try:
            assert dns._coredns is not None
            first_proc = dns._coredns.proc

            upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
            await dns.add_zone("host.example.org")

            cf = config.coredns_corefile_path.read_text()
            assert "host.example.org:53 {" in cf
            assert (_zonefile(config, "host.example.org")).exists()
            # restart() replaced the process so the new Corefile (new zone) takes effect.
            assert dns._coredns.proc is not first_proc
        finally:
            await dns.cleanup()


@pytest.mark.asyncio
async def test_concurrent_zone_changes_serialize_their_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two /api/domains requests are two tasks on one event loop.  Overlapping restarts would leave
    # an orphaned CoreDNS holding :53, with the surviving handle pointing at the other process.
    stub_coredns_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        zones = _zones(db)
    dns = _provider(config)
    await _serve(dns, zones)
    try:
        in_flight = 0
        peak = 0
        real_restart = dns_mod.CoreDnsProcess.restart

        async def tracked_restart(self: dns_mod.CoreDnsProcess) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)  # a yield the second restart could slip through
            await real_restart(self)
            in_flight -= 1

        monkeypatch.setattr(dns_mod.CoreDnsProcess, "restart", tracked_restart)
        await asyncio.gather(dns.add_zone("a.example.com"), dns.add_zone("b.example.com"))

        assert peak == 1
        assert set(dns.zones) == {"host.example.com", "a.example.com", "b.example.com"}
    finally:
        await dns.cleanup()


@pytest.mark.asyncio
async def test_re_adding_a_served_zone_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Appending it again would put two server blocks for the same zone in the Corefile, which
    # CoreDNS refuses to load -- and the restart would drop DNS for no reason.
    stub_coredns_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = _provider(config)
    await dns.add_zone("host.example.com")
    try:
        assert dns._coredns is not None
        first_proc = dns._coredns.proc

        await dns.add_zone("host.example.com")

        assert dns.zones == ("host.example.com",)
        assert dns._coredns.proc is first_proc
    finally:
        await dns.cleanup()


@pytest.mark.asyncio
async def test_a_zone_this_instance_cannot_serve_is_refused(tmp_path: Path) -> None:
    # bind_ip=None means nothing would answer for it; boot catches this and carries on.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = InternalDnsProvider(corefile_path=config.coredns_corefile_path, zones_dir=config.zones_dir, bind_ip=None)

    with pytest.raises(DnsNotEnabled):
        await dns.add_zone("host.example.com")
    assert dns.zones == ()


@pytest.mark.asyncio
async def test_the_first_zone_starts_coredns_and_the_last_one_leaving_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # There is no separate start(): a provider with nothing to answer for renders a Corefile with
    # no server blocks, which CoreDNS won't start against, so the process follows the zone set.
    stub_coredns_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = _provider(config)
    assert dns._coredns is None

    await dns.add_zone("host.example.com")
    assert dns._coredns is not None

    await dns.remove_zone("host.example.com")
    assert dns._coredns is None
    assert dns.zones == ()


@pytest.mark.asyncio
async def test_removing_a_zone_discards_its_files_but_keeps_the_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_coredns_spawn(monkeypatch)
    # Records belong to no zone, so dropping one takes its rendered files and nothing else -- and
    # the surviving zone still serves everything that was written.
    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
        dns.set_records("www", RecordType.A, ["198.51.100.7"])
        removed_zone = _zonefile(config, "host.example.org")
        assert removed_zone.exists()

        remove_record(db, "host.example.org")
        await dns.remove_zone("host.example.org")
    await dns.cleanup()

    assert not removed_zone.exists()
    assert [r.data for r in dns.records] == ["198.51.100.7"]
    assert "www   300  IN A  198.51.100.7" in _zonefile(config, "host.example.com").read_text()


@pytest.mark.asyncio
async def test_concurrent_zone_changes_do_not_drop_each_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Concurrent /api/domains requests are tasks on one loop.  Building the new zone set before
    # taking the lock means a change queued behind two others computed its set from the state
    # before the one ahead of it landed, so it drops that zone -- leaving it in the DB with CoreDNS
    # never serving it.  Takes three overlapping changes: with two, the second reads the set the
    # first had already stored.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = _provider(config)

    # The real restart awaits the OS here; yielding inside the lock is what lets a second change
    # interleave at all.
    async def yielding_restart(self: InternalDnsProvider) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(InternalDnsProvider, "_match_process_to_zones", yielding_restart)

    await asyncio.gather(dns.add_zone("a.example.com"), dns.add_zone("b.example.com"), dns.add_zone("c.example.com"))

    assert set(dns.zones) == {"a.example.com", "b.example.com", "c.example.com"}


@pytest.mark.asyncio
async def test_a_new_zone_serves_the_records_written_before_it_existed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_coredns_spawn(monkeypatch)
    # The point of records that carry no zone: one that appears later renders from the same set as
    # every other, with nothing to backfill.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
        dns.set_records("www", RecordType.A, ["198.51.100.7"])

        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        await dns.add_zone("host.example.org")
    await dns.cleanup()

    added = (_zonefile(config, "host.example.org")).read_text()
    assert "$ORIGIN host.example.org." in added
    assert "www   300  IN A  198.51.100.7" in added


# ─── records ───


def test_records_are_rendered_into_every_zone_file(tmp_path: Path) -> None:
    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])

    for path in (_zonefile(config, "host.example.com"), _zonefile(config, "host.example.org")):
        assert "www   300  IN A  198.51.100.7" in path.read_text()


def test_setting_a_record_replaces_the_whole_rrset(tmp_path: Path) -> None:
    # Every publisher re-runs on boot, so a set has to replace what the last run wrote rather than
    # accumulate alongside it.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7", "198.51.100.8"])
    dns.set_records("www", RecordType.A, ["198.51.100.9"])

    content = _zonefile(config, "host.example.com").read_text()
    assert "198.51.100.9" in content
    assert "198.51.100.7" not in content
    assert "198.51.100.8" not in content


def test_deleting_a_record_leaves_the_others_alone_and_is_safe_to_repeat(tmp_path: Path) -> None:
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])
    dns.set_records("_acme-challenge", RecordType.TXT, ["tok"], ttl=60)

    dns.delete_records("_acme-challenge", RecordType.TXT)
    dns.delete_records("_acme-challenge", RecordType.TXT)  # cleanup re-runs; absent is not an error

    content = _zonefile(config, "host.example.com").read_text()
    assert "IN TXT" not in content
    assert "www   300  IN A  198.51.100.7" in content


@pytest.mark.asyncio
async def test_removing_a_zone_is_a_no_op_when_nothing_is_served(tmp_path: Path) -> None:
    # DELETE /api/domains still reaches the provider on an instance with no DNS.  add_zone refused
    # every zone here, so there is nothing to re-render and no address to render it against.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = InternalDnsProvider(corefile_path=config.coredns_corefile_path, zones_dir=config.zones_dir, bind_ip=None)

    await dns.remove_zone("host.example.com")

    assert dns.zones == ()
    assert not config.coredns_corefile_path.exists()


def test_records_are_tracked_but_not_rendered_when_nothing_is_served(tmp_path: Path) -> None:
    # local_http_only sets a public_ip with coredns disabled, so the router still publishes its
    # address records into a provider that has no view to serve them on.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    dns = InternalDnsProvider(corefile_path=config.coredns_corefile_path, zones_dir=config.zones_dir, bind_ip=None)

    publish_router_addresses(dns, PUBLIC_IP)

    assert [r.data for r in dns.records] == [PUBLIC_IP] * 3
    assert not config.coredns_corefile_path.exists()


def test_txt_data_is_quoted_so_the_zone_stays_parseable(tmp_path: Path) -> None:
    # An unquoted TXT token containing a space or a semicolon would be read as several strings or
    # as a comment, and CoreDNS would refuse the whole zone.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    dns.set_records("_acme-challenge", RecordType.TXT, ["tok en; not a comment"], ttl=60)

    assert '_acme-challenge   60  IN TXT  "tok en; not a comment"' in _zonefile(config, "host.example.com").read_text()


def test_router_addresses_are_published_as_ordinary_records(tmp_path: Path) -> None:
    # The apex, the ns glue and the wildcard are the same kind of thing as anything else in the
    # zone -- there is one way to publish a record.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    publish_router_addresses(dns, PUBLIC_IP)

    content = _zonefile(config, "host.example.com").read_text()
    for name in ("@", "ns", "*"):
        assert f"{name}   300  IN A  {PUBLIC_IP}" in content


def test_every_write_advances_the_serial_so_coredns_reloads(tmp_path: Path) -> None:
    # Two writes in the same second must not produce the same serial, or the second change is never
    # picked up.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))

    serials = []
    for i in range(3):
        dns.set_records("www", RecordType.A, [f"198.51.100.{i}"])
        serials.append(_serial(_zonefile(config, "host.example.com")))

    assert serials == sorted(set(serials))


def test_an_unchanged_write_does_not_touch_the_zone(tmp_path: Path) -> None:
    # Boot re-publishes the same router addresses every time; bumping the serial for that would
    # have CoreDNS reload a file that did not change.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    dns.set_records("www", RecordType.A, ["198.51.100.7"])
    before = _serial(_zonefile(config, "host.example.com"))

    dns.set_records("www", RecordType.A, ["198.51.100.7"])

    assert _serial(_zonefile(config, "host.example.com")) == before


def test_zone_caches_addresses_long_but_negative_answers_briefly(tmp_path: Path) -> None:
    # The zone default is what the wildcard A inherits, and that is the only thing keeping a
    # visitor able to reach the instance while CoreDNS is down during an update -- so it is
    # deliberately long. Negative caching stays short (RFC 2308 uses min(SOA MINIMUM, SOA TTL)),
    # which is what lets the NODATA left by a cleared challenge expire before the next renewal.
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **_render(tmp_path))

    content = _app_zonefile(tmp_path).read_text()
    assert "$TTL 300" in content
    assert "60    ; minimum" in content
