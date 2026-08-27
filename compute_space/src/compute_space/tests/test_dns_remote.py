"""The router as a consumer of the ``dns`` service: header injection, grant scoping, error mapping."""

from __future__ import annotations

import json
from typing import Any

import attr
import httpx
import pytest

from compute_space.core.dns.backend import DnsBackendError
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.backend import clear_txt
from compute_space.core.dns.backend import publish_txt
from compute_space.core.dns.local import LocalZoneFileBackend
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.remote import ROUTER_CONSUMER_ID
from compute_space.core.dns.remote import ServiceDnsBackend
from compute_space.core.dns.remote import router_grants


@attr.s(auto_attribs=True)
class _Recorder:
    """Captures what the router sent and replies with whatever the test queued."""

    responses: dict[str, tuple[int, dict[str, Any]]]
    requests: list[httpx.Request] = attr.ib(factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/api/dns", 1)[-1]
        status, body = self.responses.get(path, (200, {"ok": True, "results": []}))
        return httpx.Response(status, json=body)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _backend(recorder: _Recorder, domains: list[str] | None = None) -> ServiceDnsBackend:
    return ServiceDnsBackend(
        base_url="http://127.0.0.1:9999/api/dns",
        permissions_header=json.dumps(router_grants(domains or ["host.example.com"])),
        client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )


def _zone_ok(zone: str, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ok": True, "results": [{"zone": zone, "ok": True, "records": records or []}]}


# ─── identity the router asserts ───


def test_the_router_identifies_itself_as_a_consumer() -> None:
    # The provider app's service routes reject anything without a consumer identity, and the
    # router is the sole authority for these headers in the first place.
    recorder = _Recorder({"/zones": (200, {"zones": ["host.example.com"]})})
    _backend(recorder).zones()

    headers = recorder.requests[0].headers
    assert headers["X-OpenHost-Consumer-Id"] == ROUTER_CONSUMER_ID
    assert headers["X-OpenHost-Consumer-Name"] == "OpenHost Router"
    assert json.loads(headers["X-OpenHost-Permissions"])


def test_the_router_grants_itself_only_what_it_needs() -> None:
    grants = router_grants(["host.example.com"])
    entries = {(g["grant"]["name"], g["grant"]["type"]) for g in grants}

    # Challenge records plus the apex/wildcard A records it maintains — and nothing else.
    assert ("_acme-challenge", "TXT") in entries
    assert ("@", "A") in entries
    assert ("*", "A") in entries
    # Both zone shapes: the domain may be the provider's zone, or a subdomain of it.
    assert ("_acme-challenge.host.example.com", "TXT") in entries
    assert ("host.example.com", "A") in entries
    # No blanket grant: a bug here stays bounded, and the provider's audit log stays readable.
    assert ("**", "**") not in entries
    assert all(g["scope"] == "global" for g in grants)


def test_the_router_scopes_its_grants_to_the_domains_it_manages() -> None:
    entries = {g["grant"]["name"] for g in router_grants(["a.example.com", "b.example.org"])}
    assert "_acme-challenge.a.example.com" in entries
    assert "_acme-challenge.b.example.org" in entries
    assert "_acme-challenge.c.example.net" not in entries


def test_grants_are_deduplicated_across_domains() -> None:
    grants = router_grants(["a.example.com", "a.example.com"])
    assert len(grants) == len({json.dumps(g, sort_keys=True) for g in grants})


# ─── the wire shape ───


def test_a_write_sends_the_service_record_shape() -> None:
    recorder = _Recorder({"/records/set": (200, _zone_ok("host.example.com"))})
    _backend(recorder).set_records("host.example.com", [DnsRecord("www", "A", 300, "198.51.100.7")])

    body = recorder.body()
    assert body["zone"] == "host.example.com"
    assert body["records"] == [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]


def test_an_rrset_delete_omits_data_entirely() -> None:
    # That is how the API spells "delete whatever is at this name and type"; sending data: null
    # would be a request to delete a record whose value is the string "null".
    recorder = _Recorder({"/records/delete": (200, _zone_ok("host.example.com"))})
    _backend(recorder).delete_records("host.example.com", [DnsRecord("_acme-challenge", "TXT", data=None)])

    assert recorder.body()["records"] == [{"name": "_acme-challenge", "type": "TXT", "ttl": 300}]


def test_publish_txt_resolves_the_zone_through_the_service() -> None:
    recorder = _Recorder(
        {
            "/zones": (200, {"zones": ["example.com"]}),
            "/records/set": (200, _zone_ok("example.com")),
        }
    )
    publish_txt(_backend(recorder), "_acme-challenge.host.example.com", ["tok"])

    body = recorder.body()
    # The provider's zone is the parent, so the relative name keeps the intermediate label.
    assert body["zone"] == "example.com"
    assert body["records"] == [{"name": "_acme-challenge.host", "type": "TXT", "ttl": 60, "data": "tok"}]


def test_clear_txt_sends_an_rrset_delete() -> None:
    recorder = _Recorder(
        {"/zones": (200, {"zones": ["host.example.com"]}), "/records/delete": (200, _zone_ok("host.example.com"))}
    )
    clear_txt(_backend(recorder), "_acme-challenge.host.example.com")

    assert recorder.body()["records"] == [{"name": "_acme-challenge", "type": "TXT", "ttl": 300}]


def test_reads_come_back_as_records() -> None:
    recorder = _Recorder(
        {
            "/records/get": (
                200,
                _zone_ok("host.example.com", [{"name": "www", "type": "A", "ttl": 300, "data": "198.51.100.7"}]),
            )
        }
    )
    records = _backend(recorder).get_records("host.example.com", "www", "A")
    assert records == [DnsRecord("www", "A", 300, "198.51.100.7")]


# ─── failures ───


def test_a_per_zone_failure_is_raised_rather_than_read_as_success() -> None:
    # The service reports 207 for a partial fan-out, but the router always names one zone, so a
    # failed zone is a failed operation — treating it as success would drop a challenge record.
    recorder = _Recorder(
        {
            "/records/set": (
                207,
                {"ok": False, "results": [{"zone": "host.example.com", "ok": False, "error": "rate limited"}]},
            )
        }
    )
    with pytest.raises(DnsBackendError, match="rate limited"):
        _backend(recorder).set_records("host.example.com", [DnsRecord("www", "A", 300, "198.51.100.7")])


def test_an_unknown_zone_maps_to_the_typed_error() -> None:
    recorder = _Recorder({"/records/set": (400, {"error": "unknown_zone", "message": "not configured"})})
    with pytest.raises(UnknownZone):
        _backend(recorder).set_records("other.org", [DnsRecord("www", "A", 300, "198.51.100.7")])


def test_a_permission_denial_surfaces_as_a_backend_error() -> None:
    recorder = _Recorder({"/records/set": (403, {"error": "permission_required", "message": "no grant"})})
    with pytest.raises(DnsBackendError, match="permission_required"):
        _backend(recorder).set_records("host.example.com", [DnsRecord("www", "A", 300, "198.51.100.7")])


def test_an_unreachable_provider_is_a_backend_error_not_a_crash() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = ServiceDnsBackend(
        base_url="http://127.0.0.1:9999/api/dns",
        permissions_header="[]",
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(DnsBackendError, match="unreachable"):
        backend.zones()


def test_a_non_json_response_is_a_backend_error() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    backend = ServiceDnsBackend(
        base_url="http://127.0.0.1:9999/api/dns",
        permissions_header="[]",
        client=httpx.Client(transport=httpx.MockTransport(html)),
    )
    with pytest.raises(DnsBackendError, match="non-JSON"):
        backend.zones()


def test_the_remote_backend_waits_far_longer_for_propagation_than_the_local_one() -> None:
    remote = _backend(_Recorder({}))
    assert remote.propagation_timeout_seconds > LocalZoneFileBackend(zone_paths={}).propagation_timeout_seconds
