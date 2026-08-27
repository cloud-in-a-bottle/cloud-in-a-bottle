"""The router's own DNS writes: challenge records, address records, and the two dispatch paths."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import attr
import httpx
import pytest

from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.client import DnsServiceError
from compute_space.core.dns.client import dns_client
from compute_space.core.dns.client import router_grants
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.service_api import ROUTER_DNS_PROVIDER_ID
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.domains import Domain
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"


@contextmanager
def _local(tmp_path: Path, *domains: Domain) -> Iterator[DnsClient]:
    """A client for an instance serving its own DNS, over real seeded zone files."""
    config = seeded_dns_config(tmp_path, *(domains or (Domain(ZONE, tls=True),)))
    with closing(open_db(config)) as db, dns_client(config, db) as client:
        yield client


def _stored(client: DnsClient, zone: str = ZONE) -> dict[tuple[str, str], list[str]]:
    """Read the DB directly, so an assertion isn't filtered by the caller's grants."""
    out: dict[tuple[str, str], list[str]] = {}
    for r in store.records_for(client.db, zone):
        assert r.data is not None
        out.setdefault((r.name, r.type), []).append(r.data)
    return out


# ─── the router serving its own DNS ───


def test_publishes_and_clears_a_challenge(tmp_path: Path) -> None:
    with _local(tmp_path) as dns:
        dns.publish_challenge(ZONE, ["base", "wildcard"])
        assert sorted(_stored(dns)[("_acme-challenge", "TXT")]) == ['"base"', '"wildcard"']

        dns.clear_challenge(ZONE)
        assert ("_acme-challenge", "TXT") not in _stored(dns)


def test_publishing_replaces_a_previous_runs_leftovers(tmp_path: Path) -> None:
    # A run that died before cleaning up must not leave stale tokens for the next attempt.
    with _local(tmp_path) as dns:
        dns.publish_challenge(ZONE, ["stale"])
        dns.publish_challenge(ZONE, ["fresh"])
        assert _stored(dns)[("_acme-challenge", "TXT")] == ['"fresh"']


def test_clearing_a_challenge_that_is_not_there_is_fine(tmp_path: Path) -> None:
    # The cert path clears in a finally block, so it runs whether or not anything was published.
    with _local(tmp_path) as dns:
        dns.clear_challenge(ZONE)


def test_challenges_carry_a_short_ttl(tmp_path: Path) -> None:
    # Otherwise the previous run's token is served out of a resolver cache during a renewal.
    with _local(tmp_path) as dns:
        dns.publish_challenge(ZONE, ["tok"])
        records = store.records_for(dns.db, ZONE)
        assert [r.ttl for r in records if r.name == "_acme-challenge"] == [60]


def test_set_address_points_the_router_owned_names(tmp_path: Path) -> None:
    with _local(tmp_path) as dns:
        dns.set_address(ZONE, "198.51.100.7", ttl=60)
        stored = _stored(dns)
        for name in ("@", "ns", "*"):
            assert stored[(name, "A")] == ["198.51.100.7"]


def test_a_challenge_reaches_the_zone_file(tmp_path: Path) -> None:
    # The whole point of the write: CoreDNS serves the file, so the record has to land in it.
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    with closing(open_db(config)) as db, dns_client(config, db) as dns:
        dns.publish_challenge(ZONE, ["tok"])
    assert '_acme-challenge   60  IN TXT  "tok"' in config.coredns_zonefile_path.read_text()


def test_writes_land_in_the_named_zone_only(tmp_path: Path) -> None:
    with _local(tmp_path, Domain(ZONE, tls=True), Domain("host.example.org", tls=True)) as dns:
        dns.publish_challenge("host.example.org", ["tok"])
        assert ("_acme-challenge", "TXT") in _stored(dns, "host.example.org")
        assert ("_acme-challenge", "TXT") not in _stored(dns, ZONE)


def test_an_unserved_domain_is_refused(tmp_path: Path) -> None:
    with _local(tmp_path) as dns:
        with pytest.raises(DnsServiceError, match="no configured DNS zone"):
            dns.publish_challenge("other.org", ["tok"])


def test_the_router_cannot_write_outside_what_it_maintains(tmp_path: Path) -> None:
    # Its grants cover challenge TXT and the address records, so a bug in the cert or dynamic-DNS
    # path cannot rewrite an app's records or an owner's MX.
    with _local(tmp_path) as dns:
        with pytest.raises(DnsServiceError, match="permission_required"):
            dns._records("set", ZONE, [DnsRecord("www", "A", 300, "198.51.100.7")])


# ─── grants the router asserts ───


def test_router_grants_cover_what_it_writes_and_no_more() -> None:
    entries = {(g.name, g.type) for g in router_grants([ZONE])}
    assert ("_acme-challenge", "TXT") in entries
    for name in ("@", "ns", "*"):
        assert (name, "A") in entries
    # Both zone shapes: the provider's zone may be the domain itself or a parent holding it.
    assert (f"_acme-challenge.{ZONE}", "TXT") in entries
    assert (ZONE, "A") in entries
    assert ("**", "**") not in entries


def test_router_grants_are_scoped_to_the_managed_domains() -> None:
    entries = {g.name for g in router_grants(["a.example.com", "b.example.org"])}
    assert "_acme-challenge.a.example.com" in entries
    assert "_acme-challenge.c.example.net" not in entries


def test_router_grants_are_deduplicated() -> None:
    grants = router_grants(["a.example.com", "a.example.com"])
    assert len(grants) == len(set(grants))


# ─── an app provider, over the wire ───


@attr.s(auto_attribs=True)
class _Recorder:
    responses: dict[str, tuple[int, dict[str, Any]]]
    requests: list[httpx.Request] = attr.ib(factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/api/dns", 1)[-1]
        status, body = self.responses.get(path, (200, {"ok": True, "results": []}))
        return httpx.Response(status, json=body)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _remote(recorder: _Recorder) -> DnsClient:
    return DnsClient(
        config=None,  # type: ignore[arg-type]  # unused on the remote path
        db=None,  # type: ignore[arg-type]
        provider_id="external-dns-connector",
        grants=router_grants([ZONE]),
        endpoint_url="http://127.0.0.1:9999/api/dns",
        http=httpx.Client(transport=httpx.MockTransport(recorder)),
    )


def _zones_ok(*zones: str) -> tuple[int, dict[str, Any]]:
    return 200, {"zones": list(zones)}


def _write_ok(zone: str) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "results": [{"zone": zone, "ok": True, "records": []}]}


def test_the_router_identifies_itself_as_a_consumer() -> None:
    # The provider app rejects anything without a consumer identity, and the router is the sole
    # authority for these headers in the first place.
    recorder = _Recorder({"/zones": _zones_ok(ZONE)})
    _remote(recorder).zones()

    headers = recorder.requests[0].headers
    assert headers["X-OpenHost-Consumer-Id"] == "_openhost_router"
    assert json.loads(headers["X-OpenHost-Permissions"])[0]["scope"] == "global"


def test_a_challenge_under_a_parent_zone_keeps_its_prefix() -> None:
    recorder = _Recorder({"/zones": _zones_ok("example.com"), "/records/set": _write_ok("example.com")})
    _remote(recorder).publish_challenge("host.example.com", ["tok"])

    body = recorder.body()
    assert body["zone"] == "example.com"
    assert body["records"] == [{"name": "_acme-challenge.host", "type": "TXT", "ttl": 60, "data": "tok"}]


def test_the_most_specific_zone_wins() -> None:
    recorder = _Recorder(
        {"/zones": _zones_ok("example.com", "host.example.com"), "/records/set": _write_ok("host.example.com")}
    )
    _remote(recorder).publish_challenge("host.example.com", ["tok"])
    assert recorder.body()["zone"] == "host.example.com"


def test_clearing_omits_data_entirely() -> None:
    # That is how the API spells "delete whatever is at this name and type"; sending data: null
    # would ask to delete a record whose value is the string "null".
    recorder = _Recorder({"/zones": _zones_ok(ZONE), "/records/delete": _write_ok(ZONE)})
    _remote(recorder).clear_challenge(ZONE)
    assert recorder.body()["records"] == [{"name": "_acme-challenge", "type": "TXT", "ttl": 300}]


def test_a_per_zone_failure_is_raised_rather_than_read_as_success() -> None:
    # The service reports 207 for a partial fan-out, but we always name one zone, so a failed zone
    # is a failed operation -- treating it as success would silently drop a challenge record.
    recorder = _Recorder(
        {
            "/zones": _zones_ok(ZONE),
            "/records/set": (207, {"ok": False, "results": [{"zone": ZONE, "ok": False, "error": "rate limited"}]}),
        }
    )
    with pytest.raises(DnsServiceError, match="rate limited"):
        _remote(recorder).publish_challenge(ZONE, ["tok"])


def test_an_error_status_surfaces() -> None:
    recorder = _Recorder({"/zones": (403, {"error": "permission_required", "message": "no grant"})})
    with pytest.raises(DnsServiceError, match="permission_required"):
        _remote(recorder).zones()


def test_an_unreachable_provider_is_an_error_not_a_crash() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    recorder = _Recorder({})
    client = _remote(recorder)
    client.http = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(DnsServiceError, match="unreachable"):
        client.zones()


def test_a_remote_provider_gets_a_far_longer_propagation_timeout() -> None:
    local = DnsClient(config=None, db=None, provider_id=ROUTER_DNS_PROVIDER_ID, grants=[])  # type: ignore[arg-type]
    assert _remote(_Recorder({})).propagation_timeout_seconds > local.propagation_timeout_seconds
