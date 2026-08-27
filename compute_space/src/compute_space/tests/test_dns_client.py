"""The DNS client: record-level reads and writes, zone resolution, and both dispatch paths."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import attr
import httpx
import pytest

import compute_space.core.dns.client as client_mod
from compute_space.core.dns.client import LOCAL_PROPAGATION_TIMEOUT_SECONDS as LOCAL_TIMEOUT
from compute_space.core.dns.client import REMOTE_PROPAGATION_TIMEOUT_SECONDS as REMOTE_TIMEOUT
from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.client import dns_client
from compute_space.core.dns.client import ensure_dns_provider_running
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.domains import Domain
from compute_space.core.service_client import ServiceCallError
from compute_space.core.service_client import ServiceEndpoint
from compute_space.tests.conftest import open_db
from compute_space.tests.dns_helpers import seeded_dns_config

ZONE = "host.example.com"


def _challenge(domain: str) -> str:
    return f"_acme-challenge.{domain}"


@contextmanager
def _local(tmp_path: Path, *domains: Domain) -> Iterator[tuple[DnsClient, sqlite3.Connection]]:
    """A client for an instance serving its own DNS, over real seeded zone files, plus the DB so a
    test can check what was stored without going back through a grant-filtered read."""
    config = seeded_dns_config(tmp_path, *(domains or (Domain(ZONE, tls=True),)))
    with closing(open_db(config)) as db, dns_client(config, db) as client:
        yield client, db


def _stored(db: sqlite3.Connection, zone: str = ZONE) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for r in store.records_for(db, zone):
        assert r.data is not None
        out.setdefault((r.name, r.type), []).append(r.data)
    return out


# ─── the router serving its own DNS ───


def test_publishes_and_clears_a_challenge(tmp_path: Path) -> None:
    with _local(tmp_path) as (dns, db):
        dns.set_records(_challenge(ZONE), "TXT", ["base", "wildcard"], ttl=60)
        assert sorted(_stored(db)[("_acme-challenge", "TXT")]) == ['"base"', '"wildcard"']

        dns.delete_records(_challenge(ZONE), "TXT")
        assert ("_acme-challenge", "TXT") not in _stored(db)


def test_publishing_replaces_a_previous_runs_leftovers(tmp_path: Path) -> None:
    # A run that died before cleaning up must not leave stale tokens for the next attempt.
    with _local(tmp_path) as (dns, db):
        dns.set_records(_challenge(ZONE), "TXT", ["stale"], ttl=60)
        dns.set_records(_challenge(ZONE), "TXT", ["fresh"], ttl=60)
        assert _stored(db)[("_acme-challenge", "TXT")] == ['"fresh"']


def test_clearing_a_challenge_that_is_not_there_is_fine(tmp_path: Path) -> None:
    # The cert path clears in a finally block, so it runs whether or not anything was published.
    with _local(tmp_path) as (dns, db):
        dns.delete_records(_challenge(ZONE), "TXT")


def test_challenges_carry_a_short_ttl(tmp_path: Path) -> None:
    # Otherwise the previous run's token is served out of a resolver cache during a renewal.
    with _local(tmp_path) as (dns, db):
        dns.set_records(_challenge(ZONE), "TXT", ["tok"], ttl=60)
        records = store.records_for(db, ZONE)
        assert [r.ttl for r in records if r.name == "_acme-challenge"] == [60]


def test_the_apex_is_addressed_by_the_bare_domain(tmp_path: Path) -> None:
    # A caller names records by FQDN; "@" is an encoding detail of the zone the client resolves to.
    with _local(tmp_path) as (dns, db):
        dns.set_records(ZONE, "A", ["198.51.100.7"])
        assert _stored(db)[("@", "A")] == ["198.51.100.7"]


def test_a_subdomain_keeps_its_prefix(tmp_path: Path) -> None:
    with _local(tmp_path) as (dns, db):
        dns.set_records(f"www.{ZONE}", "A", ["198.51.100.7"])
        assert _stored(db)[("www", "A")] == ["198.51.100.7"]


def test_a_challenge_reaches_the_zone_file(tmp_path: Path) -> None:
    # The whole point of the write: CoreDNS serves the file, so the record has to land in it.
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    with closing(open_db(config)) as db, dns_client(config, db) as dns:
        dns.set_records(_challenge(ZONE), "TXT", ["tok"], ttl=60)
    assert '_acme-challenge   60  IN TXT  "tok"' in config.coredns_zonefile_path.read_text()


def test_writes_land_in_the_named_zone_only(tmp_path: Path) -> None:
    with _local(tmp_path, Domain(ZONE, tls=True), Domain("host.example.org", tls=True)) as (dns, db):
        dns.set_records(_challenge("host.example.org"), "TXT", ["tok"], ttl=60)
        assert ("_acme-challenge", "TXT") in _stored(db, "host.example.org")
        assert ("_acme-challenge", "TXT") not in _stored(db, ZONE)


def test_an_unserved_domain_is_refused(tmp_path: Path) -> None:
    with _local(tmp_path) as (dns, db):
        with pytest.raises(ServiceCallError, match="no configured DNS zone"):
            dns.set_records(_challenge("other.org"), "TXT", ["tok"], ttl=60)


# ─── grants the router asserts ───


def test_the_router_claims_only_the_records_it_writes() -> None:
    # Self-asserted, so this is about legibility rather than enforcement: a provider app's audit
    # log should say exactly what was touched.
    recorder = _Recorder({"/zones": _zones_ok(ZONE), "/records/set": _write_ok(ZONE)})
    _remote(recorder).set_records(_challenge(ZONE), "TXT", ["tok"], ttl=60)

    claimed = json.loads(recorder.requests[-1].headers["X-OpenHost-Permissions"])
    assert [e["grant"] for e in claimed] == [{"name": "_acme-challenge", "type": "TXT", "access": "rw"}]
    assert all(e["scope"] == "global" for e in claimed)


def test_reading_the_zone_list_claims_only_read_access() -> None:
    recorder = _Recorder({"/zones": _zones_ok(ZONE)})
    _remote(recorder).zones()

    claimed = json.loads(recorder.requests[0].headers["X-OpenHost-Permissions"])
    assert [e["grant"]["access"] for e in claimed] == ["r"]


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
    """A client bound to an app provider, so every call is a real HTTP round trip."""
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return DnsClient(ServiceEndpoint(http=http, base_url="http://127.0.0.1:9999/api/dns"))


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
    _remote(recorder).set_records(_challenge("host.example.com"), "TXT", ["tok"], ttl=60)

    body = recorder.body()
    assert body["zone"] == "example.com"
    assert body["records"] == [{"name": "_acme-challenge.host", "type": "TXT", "ttl": 60, "data": "tok"}]


def test_the_most_specific_zone_wins() -> None:
    recorder = _Recorder(
        {"/zones": _zones_ok("example.com", "host.example.com"), "/records/set": _write_ok("host.example.com")}
    )
    _remote(recorder).set_records(_challenge("host.example.com"), "TXT", ["tok"], ttl=60)
    assert recorder.body()["zone"] == "host.example.com"


def test_clearing_omits_data_entirely() -> None:
    # That is how the API spells "delete whatever is at this name and type"; sending data: null
    # would ask to delete a record whose value is the string "null".
    recorder = _Recorder({"/zones": _zones_ok(ZONE), "/records/delete": _write_ok(ZONE)})
    _remote(recorder).delete_records(_challenge(ZONE), "TXT")
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
    with pytest.raises(ServiceCallError, match="rate limited"):
        _remote(recorder).set_records(_challenge(ZONE), "TXT", ["tok"], ttl=60)


def test_an_error_status_surfaces() -> None:
    recorder = _Recorder({"/zones": (403, {"error": "permission_required", "message": "no grant"})})
    with pytest.raises(ServiceCallError, match="permission_required"):
        _remote(recorder).zones()


def test_an_unreachable_provider_is_an_error_not_a_crash() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(refuse))
    client = DnsClient(ServiceEndpoint(http=http, base_url="http://127.0.0.1:9999/api/dns"))
    with pytest.raises(ServiceCallError, match="unreachable"):
        client.zones()


def test_a_remote_provider_gets_a_far_longer_propagation_timeout() -> None:
    # An external registrar can take minutes to publish; a local zone file is instant.
    assert REMOTE_TIMEOUT > LOCAL_TIMEOUT


# ─── waiting for a DNS provider app before a cert is acquired ───


def _install_provider_app(config: Any, status: str, container_id: str | None = "c1") -> None:
    """Register an app as the `dns` provider, so the router is no longer the implicit one."""
    with closing(open_db(config)) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, status, container_id, local_port) "
            "VALUES ('dnsapp', 'dns-connector', '0.1.0', '/tmp/dnsapp', ?, ?, 19000)",
            (status, container_id),
        )
        db.execute(
            "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) "
            "VALUES (?, 'dnsapp', '0.1.0', '/api/dns/')",
            (DNS_SERVICE_URL,),
        )
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'dnsapp')", (DNS_SERVICE_URL,))
        db.commit()


def test_the_router_serving_its_own_dns_needs_no_provider_app(tmp_path: Path) -> None:
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    with closing(open_db(config)) as db:
        assert ensure_dns_provider_running(config, db) is True


def test_a_running_provider_app_is_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    _install_provider_app(config, status="running")
    monkeypatch.setattr(client_mod, "is_container_running", lambda cid: True)
    started: list[str] = []
    monkeypatch.setattr(client_mod, "start_app_process", lambda *a: started.append("start"))

    with closing(open_db(config)) as db:
        assert ensure_dns_provider_running(config, db) is True
    assert started == []


def test_a_stopped_provider_app_is_started_and_waited_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The container is dead after a reboot, which is exactly when a first cert is needed.
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    _install_provider_app(config, status="running", container_id=None)

    def fake_start(app_id: str, db: Any, cfg: Any) -> None:
        db.execute("UPDATE apps SET status = 'running', container_id = 'c2' WHERE app_id = ?", (app_id,))
        db.commit()

    monkeypatch.setattr(client_mod, "start_app_process", fake_start)
    monkeypatch.setattr(client_mod, "is_container_running", lambda cid: True)

    with closing(open_db(config)) as db:
        assert ensure_dns_provider_running(config, db) is True


def test_a_provider_app_that_errors_is_not_waited_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    _install_provider_app(config, status="running", container_id=None)

    def fake_start(app_id: str, db: Any, cfg: Any) -> None:
        db.execute("UPDATE apps SET status = 'error' WHERE app_id = ?", (app_id,))
        db.commit()

    monkeypatch.setattr(client_mod, "start_app_process", fake_start)
    monkeypatch.setattr(client_mod, "is_container_running", lambda cid: False)

    with closing(open_db(config)) as db:
        assert ensure_dns_provider_running(config, db) is False


def test_a_provider_app_that_never_comes_up_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bounded, because a rebuild can take minutes and the instance must not stay offline for one.
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    _install_provider_app(config, status="running", container_id=None)
    monkeypatch.setattr(client_mod, "start_app_process", lambda *a: None)
    monkeypatch.setattr(client_mod, "is_container_running", lambda cid: False)
    monkeypatch.setattr(client_mod, "_PROVIDER_POLL_SECONDS", 0.01)

    with closing(open_db(config)) as db:
        assert ensure_dns_provider_running(config, db, timeout=0.05) is False


def test_a_missing_provider_app_is_reported_rather_than_waited_for(tmp_path: Path) -> None:
    config = seeded_dns_config(tmp_path, Domain(ZONE, tls=True))
    with closing(open_db(config)) as db:
        # Default points at an app that was never installed.
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, status, local_port) "
            "VALUES ('ghost', 'ghost', '0.1.0', '/tmp/g', 'running', 19001)"
        )
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'ghost')", (DNS_SERVICE_URL,))
        db.commit()
        assert ensure_dns_provider_running(config, db) is False
