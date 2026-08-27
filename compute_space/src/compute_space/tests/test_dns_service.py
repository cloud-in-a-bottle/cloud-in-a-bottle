"""The router's implementation of the ``dns`` service: grants, filtering, and record ops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

from compute_space.core.dns.coredns_provider import operations
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.grants import parse as parse_grants
from compute_space.core.dns.coredns_provider.routes import dns_service_app
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.domains import Domain
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"


def _perms(*entries: tuple[str, str, str]) -> list[Any]:
    """Permission entries in wire form, as the router injects them."""
    return [{"grant": {"name": n, "type": t, "access": a}, "scope": "global"} for n, t, a in entries]


class _Space:
    """A seeded instance with real zone files, callable like the service proxy would call it."""

    def __init__(self, tmp_path: Path, *domains: Domain) -> None:
        self.config = seeded_dns_config(tmp_path, *domains)
        self.db = open_db(self.config)

    def call(self, path: str, payload: dict[str, Any], permissions: list[Any]) -> tuple[int, dict[str, Any]]:
        """Through the real ASGI app, so routing and body parsing are exercised too."""
        with TestClient(app=dns_service_app) as client:
            response = client.post(path, json=payload, headers={"X-OpenHost-Permissions": json.dumps(permissions)})
        return response.status_code, response.json()

    def records(self, name: str, rrtype: str) -> list[str]:
        """Read the store directly, so an assertion isn't filtered by the caller's grants."""
        found = store.records_for(self.db, ZONE)
        return [r.data for r in found if r.name == name and r.type == rrtype and r.data]

    def write(self, records: list[DnsRecord]) -> None:
        store.append_records(self.db, ZONE, records)

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
    entries = [
        {"grant": {"name": "a", "type": "TXT", "access": "rw"}, "scope": "app"},
        {"grant": {"name": "b", "type": "TXT", "access": "rw"}, "scope": "global"},
    ]
    assert [g.name for g in parse_grants(entries)] == ["b"]


def test_a_malformed_grant_narrows_access_rather_than_breaking_the_call() -> None:
    entries = [
        {"grant": {"name": "ok", "type": "TXT", "access": "rw"}, "scope": "global"},
        {"grant": {"name": "bad", "type": "TXT", "access": "sudo"}, "scope": "global"},
        {"grant": "not-an-object", "scope": "global"},
    ]
    assert [g.name for g in parse_grants(entries)] == ["ok"]


def test_a_dotted_wildcard_grant_does_not_cover_the_bare_label() -> None:
    # Documents a sharp edge shared with the connector app (internal/grants/match.go), kept
    # identical here on purpose: two providers of one service must not disagree about what a
    # grant means. "**" matches a run of characters, so the "." in "_acme-challenge.**" is
    # required literally — and a cert for the zone apex puts its challenge at the bare
    # "_acme-challenge". A grant meaning "this label and anything under it" is written without
    # the dot.
    dotted = parse_grants(_perms(("_acme-challenge.**", "TXT", "rw")))[0]
    assert dotted.matches("_acme-challenge.host", "TXT")
    assert not dotted.matches("_acme-challenge", "TXT")

    undotted = parse_grants(_perms(("_acme-challenge**", "TXT", "rw")))[0]
    assert undotted.matches("_acme-challenge", "TXT")
    assert undotted.matches("_acme-challenge.host", "TXT")


def test_a_single_star_is_a_literal_dns_wildcard_label(space: Any) -> None:
    # "*.app" is a real record name, so a grant naming it must not become a pattern.
    grants = _perms(("*", "A", "rw"))
    status, body = space.call("/records/get", {"zone": ZONE, "name": "www", "type": "A"}, grants)
    assert status == 200
    assert body["results"][0]["records"] == []


# ─── zones ───


def test_zones_needs_a_grant_before_it_names_the_owner_s_domains(space: Any) -> None:
    status, body = space.call("/zones", {}, [])
    assert status == 403
    assert body["error"] == "permission_required"


def test_zones_lists_the_instance_s_domains(space: Any) -> None:
    status, body = space.call("/zones", {}, _perms(ACME))
    assert status == 200
    assert body["zones"] == [ZONE]


# ─── reads ───


def test_a_read_returns_only_what_the_grants_match(space: Any) -> None:
    space.write([DnsRecord("_acme-challenge", "TXT", 60, "tok"), DnsRecord("secret", "TXT", 300, "other")])

    status, body = space.call("/records/get", {"zone": ZONE}, _perms(ACME))

    assert status == 200
    names = {r["name"] for r in body["results"][0]["records"]}
    # Everything else is omitted rather than refused, so a narrow app sees only its own records.
    assert names == {"_acme-challenge"}


def test_an_ungranted_app_reads_nothing_and_learns_no_zone_names(space: Any) -> None:
    status, body = space.call("/records/get", {"zone": ZONE}, [])
    assert status == 200
    assert body["results"] == []


def test_a_read_defaults_to_every_zone(space: Any) -> None:
    status, body = space.call("/records/get", {}, _perms(RW_ALL))
    assert status == 200
    assert [r["zone"] for r in body["results"]] == [ZONE]


# ─── writes ───


def test_a_write_must_name_a_zone(space: Any) -> None:
    status, body = space.call(
        "/records/set", {"records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]}, _perms(RW_ALL)
    )
    assert status == 400
    assert body["error"] == "zone_required"


def test_a_write_without_a_covering_grant_is_refused_with_the_grant_it_would_need(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _perms(ACME),
    )
    assert status == 403
    assert body["error"] == "permission_required"
    assert body["required_grant"]["grant"] == {"name": "www", "type": "A", "access": "rw"}
    assert body["required_grant"]["scope"] == "global"


def test_a_read_only_grant_cannot_write(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _perms(("**", "**", "r")),
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
        _perms(ACME),
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
        _perms(ACME),
    )
    assert status == 200
    assert body["ok"] is True
    assert space.records("_acme-challenge", "TXT") == ['"tok"']


def test_delete_without_data_clears_the_rrset(space: Any) -> None:
    space.write([DnsRecord("_acme-challenge", "TXT", 60, "tok")])

    status, _ = space.call(
        "/records/delete",
        {"zone": ZONE, "records": [{"name": "_acme-challenge", "type": "TXT", "ttl": 60}]},
        _perms(ACME),
    )

    assert status == 200
    assert space.records("_acme-challenge", "TXT") == []


def test_an_unknown_zone_is_reported_once_the_caller_is_authorized(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": "other.org", "records": [{"name": "www", "type": "A", "ttl": 300, "data": "1.2.3.4"}]},
        _perms(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "unknown_zone"


def test_an_app_may_write_the_router_owned_names(space: Any) -> None:
    # There is no reserved-record rule: the router regenerates its address records from the public
    # IP on the next render, so an app writing them is overwritten rather than rejected.
    status, _ = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]},
        _perms(RW_ALL),
    )
    assert status == 200


def test_mail_records_at_the_apex_are_writable(space: Any) -> None:
    status, _ = space.call(
        "/records/append",
        {
            "zone": ZONE,
            "records": [
                {"name": "@", "type": "MX", "ttl": 300, "data": "10 mail.example.com."},
                {"name": "@", "type": "TXT", "ttl": 300, "data": "v=spf1 -all"},
            ],
        },
        _perms(RW_ALL),
    )
    assert status == 200
    assert space.records("@", "MX") == ["10 mail.example.com."]


def test_a_write_reaches_the_zone_file(space: Any) -> None:
    space.call(
        "/records/append",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]},
        _perms(RW_ALL),
    )
    assert "www   300  IN A  198.51.100.7" in space.config.coredns_zonefile_path.read_text()


def test_unparseable_rdata_is_rejected_before_it_reaches_the_zone_file(space: Any) -> None:
    # Zone files are generated from stored records, so a bad value would make CoreDNS reject the
    # whole zone and take the domain down.
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "not-an-ip"}]},
        _perms(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "invalid_record"


def test_an_a_record_holding_an_ipv6_literal_is_rejected(space: Any) -> None:
    # Otherwise an app granted A could write an AAAA.
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "2001:db8::1"}]},
        _perms(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "invalid_record"


# ─── malformed input ───


def test_an_unwritable_record_type_is_rejected(space: Any) -> None:
    status, body = space.call(
        "/records/set",
        {"zone": ZONE, "records": [{"name": "www", "type": "DNSKEY", "ttl": 300, "data": "x"}]},
        _perms(RW_ALL),
    )
    assert status == 400
    assert body["error"] == "invalid_record"


def test_an_empty_record_list_is_rejected(space: Any) -> None:
    status, body = space.call("/records/set", {"zone": ZONE, "records": []}, _perms(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_request"


def test_an_unknown_path_is_rejected(space: Any) -> None:
    status, body = space.call("/records/frobnicate", {"zone": ZONE}, _perms(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_request"


# ─── partial failures and malformed bodies ───


def test_a_fan_out_reports_207_when_only_some_zones_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One zone failing must not lose the ones that worked, which is what the per-zone results and
    # the 207 status are for.
    space = _Space(tmp_path, Domain(ZONE, tls=True), Domain("other.example.com", tls=True))
    try:
        real = operations.write_zone_file

        def fail_second(zone: Any, ip: str, db: Any, *a: Any) -> None:
            if zone.domain == "other.example.com":
                raise OSError("disk full")
            real(zone, ip, db, *a)

        monkeypatch.setattr(operations, "write_zone_file", fail_second)

        status, body = space.call(
            "/records/append",
            {"zone": "*", "records": [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]},
            _perms(RW_ALL),
        )

        assert status == 207
        assert body["ok"] is False
        assert {r["zone"]: r["ok"] for r in body["results"]} == {ZONE: True, "other.example.com": False}
        assert "disk full" in next(r["error"] for r in body["results"] if not r["ok"])
    finally:
        space.close()


def test_every_zone_failing_is_reported_as_502(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A blanket 200 would let a caller read a total failure as success.
    space = _Space(tmp_path, Domain(ZONE, tls=True))
    try:
        monkeypatch.setattr(operations, "write_zone_file", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        status, body = space.call(
            "/records/append",
            {"zone": ZONE, "records": [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]},
            _perms(RW_ALL),
        )
        assert status == 502
        assert body["ok"] is False
    finally:
        space.close()


def test_a_record_that_is_not_an_object_is_an_invalid_record(space: Any) -> None:
    # Litestar rejects this while parsing the body, so it never reaches our validation; the
    # exception handler makes it read the same as any other bad record.
    status, body = space.call("/records/set", {"zone": ZONE, "records": ["not-an-object"]}, _perms(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_record"


def test_an_unknown_operation_is_rejected(space: Any) -> None:
    status, body = space.call("/records/frobnicate", {"zone": ZONE, "records": []}, _perms(RW_ALL))
    assert status == 400
    assert body["error"] == "invalid_request"
