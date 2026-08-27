"""Zone resolution, the local backend, and the DNS-01 helpers built on the backend interface."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.backend import clear_txt
from compute_space.core.dns.backend import publish_txt
from compute_space.core.dns.backend import split_fqdn
from compute_space.core.dns.coredns import _write_coredns_config
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.local import LocalZoneFileBackend
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db import init_db
from compute_space.tests.conftest import open_db


def _backend(tmp_path: Path, *domains: Domain) -> LocalZoneFileBackend:
    """A local backend over real, seeded zone files for ``domains`` (primary first)."""
    config = DefaultConfig(data_root_dir=str(tmp_path), public_ip="203.0.113.10")
    config.make_all_dirs()
    init_db(config.db_path)
    with closing(open_db(config)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
        zones = public_dns_zones(config, db)
        _write_coredns_config(zones, "203.0.113.10", config.coredns_corefile_path, None, serve_public=True)
        return LocalZoneFileBackend.create(config, db)


# ─── zone resolution ───


def test_split_fqdn_prefers_the_most_specific_zone() -> None:
    zones = ["example.com", "host.example.com"]
    match = split_fqdn("_acme-challenge.host.example.com", zones)
    assert (match.zone, match.name) == ("host.example.com", "_acme-challenge")


def test_split_fqdn_handles_a_name_under_a_parent_zone() -> None:
    match = split_fqdn("_acme-challenge.host.example.com", ["example.com"])
    assert (match.zone, match.name) == ("example.com", "_acme-challenge.host")


def test_split_fqdn_maps_the_zone_itself_to_the_apex() -> None:
    match = split_fqdn("example.com", ["example.com"])
    assert (match.zone, match.name) == ("example.com", "@")


def test_split_fqdn_does_not_match_a_suffix_that_is_not_a_label_boundary() -> None:
    # "notexample.com" ends with "example.com" as a string but is a different domain.
    with pytest.raises(UnknownZone):
        split_fqdn("www.notexample.com", ["example.com"])


def test_split_fqdn_reports_the_zones_it_knows_about() -> None:
    with pytest.raises(UnknownZone, match="example.com"):
        split_fqdn("www.other.org", ["example.com"])


# ─── the local backend ───


def test_local_backend_lists_its_zones(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        Domain("host.example.com", tls=True),
        Domain("host.example.org", tls=True),
        Domain("myhost.local", mdns=True),
    )
    # mDNS domains are not zones: CoreDNS never serves them.
    assert backend.zones() == ["host.example.com", "host.example.org"]


def test_local_backend_writes_into_the_right_zone_file(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True), Domain("host.example.org", tls=True))

    backend.append_records("host.example.org", [DnsRecord("www", "A", 300, "198.51.100.7")])

    assert [r.data for r in backend.get_records("host.example.org", "www", "A")] == ["198.51.100.7"]
    # The secondary's record must not land in the primary's zone.
    assert backend.get_records("host.example.com", "www", "A") == []


def test_local_backend_rejects_an_unconfigured_zone(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    with pytest.raises(UnknownZone):
        backend.append_records("other.org", [DnsRecord("www", "A", 300, "198.51.100.7")])


def test_local_backend_rejects_a_fully_qualified_name(tmp_path: Path) -> None:
    # A zone file reads "www.host.example.com" inside zone "host.example.com" as
    # www.host.example.com.host.example.com, so this has to fail rather than be fixed up.
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    with pytest.raises(InvalidRecord, match="already includes the zone"):
        backend.append_records("host.example.com", [DnsRecord("www.host.example.com", "A", 300, "198.51.100.7")])


def test_local_backend_validates_the_whole_batch_before_writing_any_of_it(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))

    with pytest.raises(InvalidRecord):
        backend.append_records(
            "host.example.com",
            [DnsRecord("good", "A", 300, "198.51.100.7"), DnsRecord("bad", "NOPE", 300, "x")],
        )

    assert backend.get_records("host.example.com", "good", "A") == []


def test_local_backend_requires_data_except_on_delete(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    with pytest.raises(InvalidRecord, match="no data"):
        backend.set_records("host.example.com", [DnsRecord("www", "A", 300, None)])
    # A delete selector is exactly the case where omitting data is meaningful.
    backend.delete_records("host.example.com", [DnsRecord("www", "A", data=None)])


# ─── DNS-01 helpers over the interface ───


def test_publish_txt_resolves_the_zone_and_writes_the_challenge(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))

    publish_txt(backend, "_acme-challenge.host.example.com", ["base", "wildcard"])

    records = backend.get_records("host.example.com", "_acme-challenge", "TXT")
    assert sorted(r.data for r in records if r.data) == ['"base"', '"wildcard"']
    # Challenge records carry a short explicit TTL, not the zone default, so the previous run's
    # token can't be served out of a resolver cache during the next renewal.
    assert {r.ttl for r in records} == {60}


def test_publish_txt_replaces_a_previous_run_s_leftovers(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    publish_txt(backend, "_acme-challenge.host.example.com", ["stale"])

    publish_txt(backend, "_acme-challenge.host.example.com", ["fresh"])

    records = backend.get_records("host.example.com", "_acme-challenge", "TXT")
    assert [r.data for r in records] == ['"fresh"']


def test_clear_txt_removes_the_challenge_without_knowing_its_value(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    publish_txt(backend, "_acme-challenge.host.example.com", ["tok"])

    clear_txt(backend, "_acme-challenge.host.example.com")

    assert backend.get_records("host.example.com", "_acme-challenge", "TXT") == []


def test_clear_txt_leaves_the_zone_s_other_records_alone(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Domain("host.example.com", tls=True))
    backend.append_records("host.example.com", [DnsRecord("v", "TXT", 300, "spf-ish")])
    publish_txt(backend, "_acme-challenge.host.example.com", ["tok"])

    clear_txt(backend, "_acme-challenge.host.example.com")

    # The old implementation stripped every TXT line in the file; this must not.
    assert [r.data for r in backend.get_records("host.example.com", "v", "TXT")] == ['"spf-ish"']
    assert [r.data for r in backend.get_records("host.example.com", "*", "A")] == ["203.0.113.10"]
