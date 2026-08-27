"""The router's own implementation of the ``dns`` service: grants, filtering, reserved records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from compute_space.core.dns.coredns_provider import zonefile
from compute_space.core.dns.coredns_provider.service import handle_dns_service_call
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.service_api import parse_grants
from compute_space.core.domains import Domain
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"


def _grants(*entries: tuple[str, str, str]) -> list[Any]:
    return parse_grants(
        json.dumps([{"grant": {"name": n, "type": t, "access": a}, "scope": "global"} for n, t, a in entries])
    )


class _Space:
    """A seeded instance with real zone files, callable like the service proxy would call it."""

    def __init__(self, tmp_path: Path, *domains: Domain) -> None:
        self.config = seeded_dns_config(tmp_path, *domains)
        self.db = open_db(self.config)

    def call(self, path: str, payload: dict[str, Any], grants: list[Any]) -> tuple[int, dict[str, Any]]:
        return handle_dns_service_call(path, payload, grants, self.config, self.db)

    def records(self, name: str, rrtype: str) -> list[str]:
        """Read straight from the zone file, so an assertion isn't filtered by the caller's grants."""
        found = zonefile.read_records(self.config.coredns_zonefile_path, ZONE)
        return [r.data for r in found if r.name == name and r.type == rrtype and r.data]

    def write(self, records: list[DnsRecord]) -> None:
        zonefile.append_records(self.config.coredns_zonefile_path, ZONE, records)

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def space(tmp_path: Path) -> Any:
    s = _Space(tmp_path, Domain(ZONE, tls=True))
    yield s
    s.close()


RW_ALL = ("**", "**", "rw")
# Two patterns, because "_acme-challenge.**" alone does not cover the bare "_acme-challenge" —
# see test_a_dotted_wildcard_grant_does_not_cover_the_bare_label.
ACME = ("_acme-challenge**", "TXT", "rw")


# ─── grant parsing ───


def test_only_global_scoped_grants_are_honored() -> None:
    header = json.dumps(
        [
            {"grant": {"name": "a", "type": "TXT", "access": "rw"}, "scope": "app"},
            {"grant": {"name": "b", "type": "TXT", "access": "rw"}, "scope": "global"},
        ]
    )
    assert [g.name for g in parse_grants(header)] == ["b"]


def test_a_malformed_grant_narrows_access_rather_than_breaking_the_call() -> None:
    header = json.dumps(
        [
            {"grant": {"name": "ok", "type": "TXT", "access": "rw"}, "scope": "global"},
            {"grant": {"name": "bad", "type": "TXT", "access": "sudo"}, "scope": "global"},
            {"grant": "not-an-object", "scope": "global"},
        ]
    )
    assert [g.name for g in parse_grants(header)] == ["ok"]


def test_a_dotted_wildcard_grant_does_not_cover_the_bare_label() -> None:
    # Documents a sharp edge shared with the connector app (internal/grants/match.go), kept
    # identical here on purpose: two providers of one service must not disagree about what a
    # grant means. "**" matches a run of characters, so the "." in "_acme-challenge.**" is
    # required literally — and a cert for the zone apex puts its challenge at the bare
    # "_acme-challenge". A grant meaning "this label and anything under it" is written without
    # the dot.
    dotted = _grants(("_acme-challenge.**", "TXT", "rw"))[0]
    assert dotted.matches("_acme-challenge.host", "TXT")
    assert not dotted.matches("_acme-challenge", "TXT")

    undotted = _grants(("_acme-challenge**", "TXT", "rw"))[0]
    assert undotted.matches("_acme-challenge", "TXT")
    assert undotted.matches("_acme-challenge.host", "TXT")


def test_a_single_star_is_a_literal_dns_wildcard_label(space: Any) -> None:
    # "*.app" is a real record name, so a grant naming it must not become a pattern.
    grants = _grants(("*", "A", "rw"))
    status, body = space.call("/records/get", {"zone": ZONE, "name": "www", "type": "A"}, grants)
    assert status == 200
    assert body["results"][0]["records"] == []


# ─── zones ───


def test_zones_needs_a_grant_before_it_names_the_owner_s_domains(space: Any) -> None:
    status, body = space.call("/zones", {}, [])
    assert status == 403
    assert body["error"] == "permission_required"


def test_zones_lists_the_instance_s_domains(space: Any) -> None:
    status, body = space.call("/zones", {}, _grants(ACME))
    assert status == 200
    assert body["zones"] == [ZONE]


# ─── reads ───


def test_a_read_returns_only_what_the_grants_match(space: Any) -> None:
    space.write([DnsRecord("_acme-challenge", "TXT", 60, "tok"), DnsRecord("secret", "TXT", 300, "other")])

    status, body = space.call("/records/get", {"zone": ZONE}, _grants(ACME))

    assert status == 200
    names = {r["name"] for r in body["results"][0]["records"]}
    # Everything else is omitted rather than refused, so a narrow app sees only its own records.
    assert names == {"_acme-challenge"}


def test_an_ungranted_app_reads_nothing_and_learns_no_zone_names(space: Any) -> None:
    status, body = space.call("/records/get", {"zone": ZONE}, [])
    assert status == 200
    assert body["results"] == []


def test_a_read_defaults_to_every_zone(space: Any) -> None:
    status, body = space.call("/records/get", {}, _grants(RW_ALL))
    assert status == 200
    assert [r["zone"] for r in body["results"]] == [ZONE]


# ─── writes ───


def test_a_write_must_name_a_zone(space: Any) -> None:
    status, body = space.call(
        "/records/set", {"records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]}, _grants(RW_ALL)
    )
    assert status == 400
    assert body["error"] == "zone_required"


def test_a_write_without_a_covering_grant_is_refused_with_the_grant_it_would_need(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _grants(ACME),
    )
    assert status == 403
    assert body["error"] == "permission_required"
    assert body["required_grant"]["grant"] == {"name": "www", "type": "A", "access": "rw"}
    assert body["required_grant"]["scope"] == "global"


def test_a_read_only_grant_cannot_write(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _grants(("**", "**", "r")),
    )
    assert status == 403


def test_a_partly_permitted_batch_applies_none_of_it(space: Any) -> None:
    status, _ = space.call(
        "/records/append",
        {
            "zone": ZONE,
            "records": [
                {"name": "_acme-challenge", "type": "TXT", "ttl": 60, "data": "ok"},
                {"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"},
            ],
        },
        _grants(ACME),
    )
    assert status == 403
    assert space.records("_acme-challenge", "TXT") == []


def test_an_ungranted_app_cannot_probe_zone_names_through_write_errors(space: Any) -> None:
    # Authorization happens before zone resolution, so a bogus zone still yields 403, not
    # "unknown_zone" — which would confirm which zones do exist.
    status, body = space.call(
        "/records/set",
        {"zone": "other.org", "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        [],
    )
    assert status == 403
    assert body["error"] == "permission_required"


def test_a_granted_write_lands_in_the_zone_file(space: Any) -> None:
    status, body = space.call(
        "/records/append",
        {"zone": ZONE, "records": [{"name": "_acme-challenge", "type": "TXT", "ttl": 60, "data": "tok"}]},
        _grants(ACME),
    )
    assert status == 200
    assert body["ok"] is True
    assert space.records("_acme-challenge", "TXT") == ['"tok"']


def test_delete_without_data_clears_the_rrset(space: Any) -> None:
    space.write([DnsRecord("_acme-challenge", "TXT", 60, "tok")])

    status, _ = space.call(
        "/records/delete",
        {"zone": ZONE, "records": [{"name": "_acme-challenge", "type": "TXT", "ttl": 60}]},
        _grants(ACME),
    )

    assert status == 200
    assert space.records("_acme-challenge", "TXT") == []


def test_an_unknown_zone_is_reported_once_the_caller_is_authorized(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": "other.org", "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _grants(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "unknown_zone"


# ─── reserved records ───


@pytest.mark.parametrize(
    "name,rrtype",
    [("@", "A"), ("*", "A"), ("ns", "A"), ("@", "NS"), ("*", "AAAA")],
)
def test_router_owned_records_are_refused_even_with_a_blanket_grant(space: Any, name: str, rrtype: str) -> None:
    # These route the space and get rewritten on any domain or IP change, so a write would be
    # silently undone — and deleting the wildcard would take every app offline.
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": name, "type": rrtype, "ttl": 300, "data": "198.51.100.7"}]},
        _grants(RW_ALL),
    )
    assert status == 403
    assert body["error"] == "reserved_record"


def test_deleting_a_router_owned_record_is_refused_too(space: Any) -> None:
    status, body = space.call(
        "/records/delete", {"zone": ZONE, "records": [{"name": "*", "type": "A", "ttl": 300}]}, _grants(RW_ALL)
    )
    assert status == 403
    assert body["error"] == "reserved_record"
    assert space.records("*", "A") == ["203.0.113.10"]


def test_mail_records_at_the_apex_are_not_reserved(space: Any) -> None:
    # Only the records OpenHost maintains are off limits; an apex MX or TXT is exactly what a mail
    # setup needs and nothing in the router touches them.
    status, _ = space.call(
        "/records/append",
        {
            "zone": ZONE,
            "records": [
                {"name": "@", "type": "MX", "ttl": 300, "data": "10 mail.example.com."},
                {"name": "@", "type": "TXT", "ttl": 300, "data": "v=spf1 -all"},
            ],
        },
        _grants(RW_ALL),
    )
    assert status == 200
    assert space.records("@", "MX") == ["10 mail.example.com."]


def test_a_normal_subdomain_is_writable(space: Any) -> None:
    status, _ = space.call(
        "/records/append",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]},
        _grants(RW_ALL),
    )
    assert status == 200


# ─── malformed input ───


def test_an_unwritable_record_type_is_rejected(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "DNSKEY", "ttl": 300, "data": "x"}]},
        _grants(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "invalid_record"


def test_an_empty_record_list_is_rejected(space: Any) -> None:
    status, body = space.call("/records/set", {"zone": ZONE, "records": []}, _grants(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_request"


def test_an_unknown_path_is_rejected(space: Any) -> None:
    status, body = space.call("/records/frobnicate", {"zone": ZONE}, _grants(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_request"
