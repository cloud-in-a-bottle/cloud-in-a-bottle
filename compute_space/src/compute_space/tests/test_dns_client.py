"""Zone resolution, the router-served DNS path, and the DNS-01 helpers over the service client."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path

import pytest

from compute_space.core.dns import zonefile
from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.client import DnsServiceError
from compute_space.core.dns.client import UnknownZone
from compute_space.core.dns.client import clear_txt
from compute_space.core.dns.client import dns_client
from compute_space.core.dns.client import publish_txt
from compute_space.core.dns.client import split_fqdn
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.domains import Domain
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config


@contextmanager
def _dns(tmp_path: Path, *domains: Domain) -> Iterator[DnsClient]:
    """A client for an instance serving its own DNS over real, seeded zone files."""
    config = seeded_dns_config(tmp_path, *domains)
    with closing(open_db(config)) as db, dns_client(config, db) as client:
        yield client


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


# ─── the router-served path ───


def test_lists_the_instances_zones(tmp_path: Path) -> None:
    with _dns(
        tmp_path,
        Domain("host.example.com", tls=True),
        Domain("host.example.org", tls=True),
        Domain("myhost.local", mdns=True),
    ) as dns:
        # mDNS domains are not zones: CoreDNS never serves them.
        assert dns.zones() == ["host.example.com", "host.example.org"]


def test_writes_land_in_the_right_zone(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True), Domain("host.example.org", tls=True)) as dns:
        dns.append_records("host.example.org", [DnsRecord("_acme-challenge", "TXT", 60, "tok")])

        assert [r.data for r in dns.get_records("host.example.org", "_acme-challenge", "TXT")] == ['"tok"']
        # The secondary's record must not land in the primary's zone.
        assert dns.get_records("host.example.com", "_acme-challenge", "TXT") == []


def test_an_unconfigured_zone_is_rejected(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        with pytest.raises(UnknownZone):
            dns.append_records("other.org", [DnsRecord("_acme-challenge", "TXT", 60, "tok")])


def test_a_fully_qualified_name_is_rejected(tmp_path: Path) -> None:
    # A zone file reads "www.host.example.com" inside zone "host.example.com" as
    # www.host.example.com.host.example.com, so this has to fail rather than be fixed up.
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        with pytest.raises(InvalidRecord, match="already includes the zone"):
            dns.append_records("host.example.com", [DnsRecord("www.host.example.com", "A", 300, "198.51.100.7")])


def test_the_whole_batch_is_validated_before_anything_is_written(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        with pytest.raises(InvalidRecord):
            dns.append_records(
                "host.example.com",
                [DnsRecord("_acme-challenge", "TXT", 60, "tok"), DnsRecord("bad", "NOPE", 300, "x")],
            )

        assert dns.get_records("host.example.com", "_acme-challenge", "TXT") == []


def test_data_is_required_except_on_delete(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        with pytest.raises(InvalidRecord, match="no data"):
            dns.set_records("host.example.com", [DnsRecord("_acme-challenge", "TXT", 60, None)])
        # A delete selector is exactly the case where omitting data is meaningful.
        dns.delete_records("host.example.com", [DnsRecord("_acme-challenge", "TXT", data=None)])


def test_the_router_may_write_records_reserved_from_apps(tmp_path: Path) -> None:
    # The reserved-record rule protects these from apps; the router is what maintains them, and
    # dynamic DNS would be unable to do its job otherwise.
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        dns.set_records("host.example.com", [DnsRecord("*", "A", 60, "198.51.100.7")])
        assert [r.data for r in dns.get_records("host.example.com", "*", "A")] == ["198.51.100.7"]


# ─── DNS-01 helpers over the client ───


def test_publish_txt_resolves_the_zone_and_writes_the_challenge(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        publish_txt(dns, "_acme-challenge.host.example.com", ["base", "wildcard"])

        records = dns.get_records("host.example.com", "_acme-challenge", "TXT")
        assert sorted(r.data for r in records if r.data) == ['"base"', '"wildcard"']
        # A short explicit TTL, not the zone default, so the previous run's token can't be served
        # out of a resolver cache during the next renewal.
        assert {r.ttl for r in records} == {60}


def test_publish_txt_replaces_a_previous_runs_leftovers(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        publish_txt(dns, "_acme-challenge.host.example.com", ["stale"])
        publish_txt(dns, "_acme-challenge.host.example.com", ["fresh"])

        assert [r.data for r in dns.get_records("host.example.com", "_acme-challenge", "TXT")] == ['"fresh"']


def test_clear_txt_removes_the_challenge_without_knowing_its_value(tmp_path: Path) -> None:
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        publish_txt(dns, "_acme-challenge.host.example.com", ["tok"])

        clear_txt(dns, "_acme-challenge.host.example.com")

        assert dns.get_records("host.example.com", "_acme-challenge", "TXT") == []


def test_clear_txt_leaves_the_zones_other_records_alone(tmp_path: Path) -> None:
    config = seeded_dns_config(tmp_path, Domain("host.example.com", tls=True))
    # An app's record, written the way an app would rather than through the router's own grants.
    zonefile.append_records(config.coredns_zonefile_path, "host.example.com", [DnsRecord("v", "TXT", 300, "spf")])

    with closing(open_db(config)) as db, dns_client(config, db) as dns:
        publish_txt(dns, "_acme-challenge.host.example.com", ["tok"])
        clear_txt(dns, "_acme-challenge.host.example.com")

    # Read the file rather than the client: the app's record is outside the router's grants, so a
    # grant-filtered read would omit it whether or not it survived.
    records = zonefile.read_records(config.coredns_zonefile_path, "host.example.com")
    by_key = {(r.name, r.type): r.data for r in records}
    # The old implementation stripped every TXT line in the file; this must not.
    assert by_key[("v", "TXT")] == '"spf"'
    assert by_key[("*", "A")] == "203.0.113.10"
    assert ("_acme-challenge", "TXT") not in by_key


def test_the_router_cannot_write_records_outside_what_it_maintains(tmp_path: Path) -> None:
    # The router grants itself challenge TXT plus the apex/ns/wildcard A records and nothing else,
    # so a bug in the cert or dynamic-DNS path can't rewrite an app's records or an owner's MX.
    with _dns(tmp_path, Domain("host.example.com", tls=True)) as dns:
        with pytest.raises(DnsServiceError, match="permission_required"):
            dns.append_records("host.example.com", [DnsRecord("www", "A", 300, "198.51.100.7")])
