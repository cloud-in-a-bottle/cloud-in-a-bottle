"""How anything inside the router calls the ``dns`` service.

There is one way to touch a DNS record and this is it, whether the records live in our own CoreDNS
zone files or at a registrar.  The service handles that distinction; callers here — cert
acquisition, dynamic DNS — only ever see the service API.

Dispatch is the only branch: the router's own implementation runs in-process (no point talking to
ourselves over loopback), an app provider is dialled on its loopback port.  Both return the same
``(status, body)``, so everything downstream of ``_call`` is shared.

The router has no app token, but it is the sole authority for the ``X-OpenHost-*`` identity headers
in the first place, so it asserts the same headers the service proxy would have injected for it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import attr
import httpx

from compute_space.config import Config
from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.dns.records import normalize_zone
from compute_space.core.dns.service import DNS_SERVICE_URL
from compute_space.core.dns.service import DNS_SERVICE_VERSION
from compute_space.core.dns.service import ROUTER_CONSUMER_ID
from compute_space.core.dns.service import ROUTER_CONSUMER_NAME
from compute_space.core.dns.service import ROUTER_DNS_PROVIDER_ID
from compute_space.core.dns.service import handle_dns_service_call
from compute_space.core.dns.service import parse_grants
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.services_v2 import resolve_provider

CHALLENGE_TTL_SECONDS = 60

# CoreDNS's `reload 2s` is instant by comparison, so the real wait is the external check the
# delegation makes unavoidable.  External providers are orders of magnitude slower; some registrars
# take minutes to publish.
_LOCAL_PROPAGATION_TIMEOUT_SECONDS = 120.0
_REMOTE_PROPAGATION_TIMEOUT_SECONDS = 600.0
_REQUEST_TIMEOUT_SECONDS = 60.0


class DnsServiceError(RuntimeError):
    """The DNS service could not carry out the operation."""


class UnknownZone(DnsServiceError):
    """No configured zone covers the requested name."""


def dns_provider_id(db: sqlite3.Connection) -> str:
    """Which app provides the ``dns`` service.

    The router is the *implicit* provider: it has no row in ``apps``, and ``service_defaults.app_id``
    is a foreign key into that table, so it is what you get when no app has claimed the service.
    """
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (DNS_SERVICE_URL,)).fetchone()
    return row["app_id"] if row else ROUTER_DNS_PROVIDER_ID


def uses_local_dns(db: sqlite3.Connection) -> bool:
    """True when this instance answers its own DNS, so CoreDNS must serve the public zones."""
    return dns_provider_id(db) == ROUTER_DNS_PROVIDER_ID


def router_managed_domains(db: sqlite3.Connection) -> list[str]:
    return [d.name_no_port for d in effective_domains(db) if not d.mdns]


def router_grants(domains: list[str]) -> list[dict[str, Any]]:
    """The grants the router asserts for itself: challenge TXT plus the records it maintains.

    Narrow rather than a blanket ``**``, because a provider app's audit log is one of the few places
    an owner sees what touched their registrar and it should say something useful.  Each domain
    contributes two shapes, since the provider's zone may be the domain itself or a parent.
    """
    grants: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, rrtype: str) -> None:
        if (name, rrtype) not in seen:
            seen.add((name, rrtype))
            grants.append({"grant": {"name": name, "type": rrtype, "access": "rw"}, "scope": "global"})

    for domain in domains:
        for base in ("", domain.strip(".").lower()):
            add(_join(base, "_acme-challenge"), "TXT")
            add(base or APEX, "A")
            # The same set dynamic DNS re-points, and the same set the service reserves from apps.
            for label in ("ns", "*"):
                add(_join(base, label), "A")
    return grants


def _join(base: str, label: str) -> str:
    return f"{label}.{base}" if base else label


@attr.s(auto_attribs=True, frozen=True)
class ZoneMatch:
    zone: str
    name: str


@attr.s(auto_attribs=True)
class DnsClient:
    """A typed view of the ``dns`` service, bound to whichever provider this space uses."""

    config: Config
    db: sqlite3.Connection
    provider_id: str
    grants: list[dict[str, Any]]
    # None when the router serves the service itself.
    endpoint_url: str | None = None
    http: httpx.Client | None = None

    @property
    def is_local(self) -> bool:
        return self.provider_id == ROUTER_DNS_PROVIDER_ID

    @property
    def propagation_timeout_seconds(self) -> float:
        return _LOCAL_PROPAGATION_TIMEOUT_SECONDS if self.is_local else _REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    # ─── the service API ───

    def zones(self) -> list[str]:
        zones = self._call("/zones", {}).get("zones")
        if not isinstance(zones, list):
            raise DnsServiceError("DNS service returned no zone list")
        return [str(z) for z in zones]

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]:
        payload: dict[str, Any] = {"zone": zone}
        if name is not None:
            payload["name"] = name
        if rrtype is not None:
            payload["type"] = rrtype
        return self._records_from(self._call("/records/get", payload))

    def set_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        return self._write("/records/set", zone, records)

    def append_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        return self._write("/records/append", zone, records)

    def delete_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """A record with ``data is None`` clears its whole ``(name, type)`` RRset."""
        return self._write("/records/delete", zone, records, allow_rrset_selector=True)

    def resolve(self, fqdn: str) -> ZoneMatch:
        """Locate ``fqdn`` in the most specific zone containing it."""
        return split_fqdn(fqdn, self.zones())

    # ─── dispatch ───

    def _write(
        self, path: str, zone: str, records: list[DnsRecord], *, allow_rrset_selector: bool = False
    ) -> list[DnsRecord]:
        normalized = [normalize_record(r, zone, allow_rrset_selector=allow_rrset_selector) for r in records]
        return self._records_from(self._call(path, {"zone": zone, "records": [_to_wire(r) for r in normalized]}))

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._dispatch(path, payload)
        # 207 is a partial success across zones; per-zone errors surface in _records_from.
        if status not in (200, 207):
            error, message = body.get("error", "unknown_error"), body.get("message", "")
            if error == "unknown_zone":
                raise UnknownZone(f"DNS service does not manage this zone: {message}")
            raise DnsServiceError(f"DNS service call to {path} failed ({status}): {error} {message}")
        return body

    def _dispatch(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.is_local:
            return handle_dns_service_call(
                path, payload, parse_grants(json.dumps(self.grants)), self.config, self.db, ROUTER_CONSUMER_ID
            )

        assert self.endpoint_url is not None and self.http is not None
        try:
            response = self.http.post(
                self.endpoint_url + path,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": ROUTER_CONSUMER_ID,
                    "X-OpenHost-Consumer-Name": ROUTER_CONSUMER_NAME,
                    "X-OpenHost-Permissions": json.dumps(self.grants),
                },
            )
        except httpx.HTTPError as e:
            raise DnsServiceError(f"DNS service unreachable at {self.endpoint_url}: {e}") from e
        try:
            body = response.json()
        except ValueError as e:
            raise DnsServiceError(f"DNS service returned non-JSON ({response.status_code})") from e
        if not isinstance(body, dict):
            raise DnsServiceError(f"DNS service returned unexpected body: {body!r}")
        return response.status_code, body

    def _records_from(self, body: dict[str, Any]) -> list[DnsRecord]:
        """Flatten the per-zone results.  We always name exactly one zone, so a failed zone is a
        failed operation even when the overall status was 207."""
        results = body.get("results")
        if not isinstance(results, list):
            raise DnsServiceError(f"DNS service returned no results: {body!r}")
        out: list[DnsRecord] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            if not result.get("ok"):
                raise DnsServiceError(f"DNS service failed for zone {result.get('zone')}: {result.get('error')}")
            for rec in result.get("records") or []:
                out.append(
                    DnsRecord(
                        name=str(rec.get("name", "")),
                        type=str(rec.get("type", "")),
                        ttl=int(rec.get("ttl", 0)),
                        data=rec.get("data"),
                    )
                )
        return out


def _to_wire(record: DnsRecord) -> dict[str, Any]:
    """Data is omitted entirely for an RRset selector: that is how the API spells "delete whatever
    is at this name and type"."""
    wire: dict[str, Any] = {"name": record.name, "type": record.type, "ttl": record.ttl}
    if record.data is not None:
        wire["data"] = record.data
    return wire


@contextmanager
def dns_client(config: Config, db: sqlite3.Connection) -> Iterator[DnsClient]:
    """A client bound to whichever provider currently serves the ``dns`` service."""
    provider_id = dns_provider_id(db)
    grants = router_grants(router_managed_domains(db))
    if provider_id == ROUTER_DNS_PROVIDER_ID:
        yield DnsClient(config=config, db=db, provider_id=provider_id, grants=grants)
        return

    _, port, _, endpoint = resolve_provider(
        DNS_SERVICE_URL, f">={DNS_SERVICE_VERSION}", db, provider_app_id=provider_id
    )
    client = DnsClient(
        config=config,
        db=db,
        provider_id=provider_id,
        grants=grants,
        endpoint_url=f"http://127.0.0.1:{port}/{endpoint.strip('/')}",
        http=httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS),
    )
    try:
        yield client
    finally:
        client.close()


# ─── zone resolution and DNS-01 helpers ───


def split_fqdn(fqdn: str, zones: list[str]) -> ZoneMatch:
    """Match ``fqdn`` to the most specific zone containing it, as (zone, zone-relative name).

    Longest suffix wins, so an instance managing both ``example.com`` and a delegated
    ``host.example.com`` writes into the more specific one.
    """
    target = normalize_zone(fqdn)
    best: str | None = None
    for zone in zones:
        z = normalize_zone(zone)
        if (target == z or target.endswith("." + z)) and (best is None or len(z) > len(best)):
            best = z
    if best is None:
        raise UnknownZone(f"no configured DNS zone covers {fqdn!r} (zones: {', '.join(zones) or 'none'})")
    return ZoneMatch(zone=best, name=target[: -len(best)].rstrip(".") or APEX)


def publish_txt(client: DnsClient, fqdn: str, values: list[str]) -> None:
    """Publish challenge TXT values at ``fqdn``, replacing whatever is there.

    Set rather than append, so a run that died before cleaning up doesn't leave stale tokens for the
    next attempt.  The short explicit TTL keeps the previous run's token from being served out of a
    resolver cache during a renewal.
    """
    match = client.resolve(fqdn)
    client.set_records(
        match.zone,
        [DnsRecord(name=match.name, type="TXT", ttl=CHALLENGE_TTL_SECONDS, data=v) for v in values],
    )
    logger.info(f"Published {len(values)} TXT record(s) at {fqdn} in zone {match.zone}")


def clear_txt(client: DnsClient, fqdn: str) -> None:
    match = client.resolve(fqdn)
    client.delete_records(match.zone, [DnsRecord(name=match.name, type="TXT", data=None)])
    logger.info(f"Cleared TXT records at {fqdn} in zone {match.zone}")


# Deliberately not the host's own resolver: with the router serving DNS that would query CoreDNS
# directly and confirm nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"


def wait_for_txt_propagation(
    fqdn: str,
    expected_values: list[str],
    timeout: float,
    interval: float = 5,
    resolver: str = _PROPAGATION_RESOLVER,
) -> bool:
    """Poll an external resolver until every expected TXT value is visible.

    False on timeout, and the caller should proceed anyway: the ACME retry loop is the fallback, and
    some providers are slower than any timeout worth blocking on.
    """
    deadline = time.monotonic() + timeout
    expected = set(expected_values)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["dig", f"@{resolver}", fqdn, "TXT", "+short", "+timeout=5", "+tries=1"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            found = {line.strip().strip('"') for line in result.stdout.strip().splitlines()}
            if expected <= found:
                logger.info(f"DNS propagation confirmed: {fqdn} has all {len(expected)} expected TXT value(s)")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        logger.info(f"Waiting for DNS propagation of {fqdn} ({deadline - time.monotonic():.0f}s remaining)")
        time.sleep(interval)

    logger.warning(f"DNS propagation timeout: {fqdn} not fully visible after {timeout}s, proceeding anyway")
    return False
