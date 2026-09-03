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
from compute_space.core.dns.coredns_provider.interface import DnsZoneError
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

PUBLIC_IP = "203.0.113.10"
# The local address CoreDNS binds; the compute space works it out and passes it in.
BIND_IP = "10.0.0.5"
APP_ZONE = "app.example.com"


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
    }


def _app_zonefile(tmp_path: Path) -> Path:
    return tmp_path / "zones" / f"{APP_ZONE}.zone"


def _zones(db: sqlite3.Connection) -> tuple[str, ...]:
    """The zone set the app derives from its domains: every public (non-mDNS) domain."""
    return tuple(d.name_no_port for d in effective_domains(db) if not d.mdns)


def _provider(config: DefaultConfig, zones: tuple[str, ...]) -> InternalDnsProvider:
    return InternalDnsProvider(
        corefile_path=config.coredns_corefile_path,
        zones_dir=config.zones_dir,
        bind_ip=BIND_IP,
        zones=zones,
    )


def _serial(zonefile: Path) -> int:
    return int(zonefile.read_text().split("; serial")[0].strip().splitlines()[-1].strip())


# ─── the container-facing view ───


class _FakeStdout:
    """An empty stream that never ends, so the log task stays alive like it would over a real
    process and is wound down by stop() rather than falling out of its own loop."""

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FakeProc:
    pid = 4242
    # Report already-exited so CoreDnsProcess.stop() skips the terminate path.
    returncode = 0

    def __init__(self) -> None:
        self.stdout = _FakeStdout()

    async def wait(self) -> int:
        return 0


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub at the OS boundary, so start() and restart() themselves run for real."""

    async def fake_exec(*a: object, **k: object) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_container_dns_view_rendered_when_gateway_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)

    render = _render(tmp_path, container_gateway_ip="10.200.0.1")
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)

    cf = render["corefile_path"].read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert f"forward . {' '.join(dns_mod.UPSTREAM_DNS)}" in cf

    # The container zonefile answers the wildcard with the gateway, never the public IP.
    cz = _app_zonefile(tmp_path).with_suffix(".zone.container").read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert PUBLIC_IP not in cz


def test_container_dns_view_skipped_when_gateway_not_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)

    render = _render(tmp_path, container_gateway_ip="10.200.0.1")
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)

    cf = render["corefile_path"].read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    assert not _app_zonefile(tmp_path).with_suffix(".zone.container").exists()


def test_container_view_forward_and_distinct_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The public view and the container view must bind different addresses (the default-route
    # source vs the gateway), and the container catch-all must forward to the upstreams.
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)

    render = _render(tmp_path, container_gateway_ip=CONTAINER_GATEWAY_IP)
    dns_mod.write_coredns_config((APP_ZONE,), (), serial=1, **render)
    cf = render["corefile_path"].read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert f"bind {CONTAINER_GATEWAY_IP}" in cf  # container view + catch-all
    assert f"forward . {' '.join(dns_mod.UPSTREAM_DNS)}" in cf
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
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(
        tmp_path, Domain(name="host.example.com", tls=True), Domain(name="host.example.org", tls=True)
    )
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    await dns.start()
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
        await dns.stop()


@pytest.mark.asyncio
async def test_adding_a_zone_regenerates_the_corefile_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
        await dns.start()
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
            await dns.stop()


@pytest.mark.asyncio
async def test_re_adding_a_served_zone_raises_and_leaves_coredns_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /api/domains rejects an already-configured domain before it gets here, so a duplicate reaching
    # the provider means the two disagree -- worth raising over.  The refusal must not cost a
    # restart, which would drop DNS for no reason.
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
        await dns.start()
        try:
            assert dns._coredns is not None
            first_proc = dns._coredns.proc
            with pytest.raises(DnsZoneError):
                await dns.add_zone("host.example.com")
            assert dns._coredns.proc is first_proc
        finally:
            await dns.stop()


@pytest.mark.asyncio
async def test_concurrent_zone_changes_serialize_their_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two /api/domains requests are two tasks on one event loop.  Overlapping restarts would leave
    # an orphaned CoreDNS holding :53, with the surviving handle pointing at the other process.
    _stub_spawn(monkeypatch)

    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    await dns.start()
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
        await dns.stop()


@pytest.mark.asyncio
async def test_a_zone_change_before_start_is_stored_but_restarts_nothing(tmp_path: Path) -> None:
    # /api/domains can be served by a router with coredns_enabled off; the zone set still has to be
    # kept honest so a later start picks it up.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
    await dns.add_zone("host.example.org")
    assert list(dns.zones) == ["host.example.com", "host.example.org"]


@pytest.mark.asyncio
async def test_removing_a_zone_discards_its_files_but_keeps_the_records(tmp_path: Path) -> None:
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

    assert not removed_zone.exists()
    assert [r.data for r in dns.records] == ["198.51.100.7"]
    assert "www   300  IN A  198.51.100.7" in _zonefile(config, "host.example.com").read_text()


@pytest.mark.asyncio
async def test_a_new_zone_serves_the_records_written_before_it_existed(tmp_path: Path) -> None:
    # The point of records that carry no zone: one that appears later renders from the same set as
    # every other, with nothing to backfill.
    config = _seed_dns_cfg(tmp_path, Domain(name="host.example.com", tls=True))
    with closing(open_db(config)) as db:
        dns = _provider(config, _zones(db))
        dns.set_records("www", RecordType.A, ["198.51.100.7"])

        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        await dns.add_zone("host.example.org")

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
