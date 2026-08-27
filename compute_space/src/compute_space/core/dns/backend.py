"""Where this space's DNS records actually live, and how to get at them.

One interface, two implementations: ``LocalZoneFileBackend`` writes the CoreDNS zone files this
instance serves, ``ServiceDnsBackend`` calls whatever app provides the ``dns`` service.  Callers
work against the interface and never learn which one they got.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Protocol

import attr
import httpx

from compute_space.config import Config
from compute_space.core.dns import zonefile
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.dns.records import normalize_zone
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.services_v2 import resolve_provider

# The service both backends speak.  The router and a connector app are interchangeable providers
# of it; which one applies is the ordinary service default.
DNS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/dns"
DNS_SERVICE_VERSION = "0.1.0"

# Provider id for the router's own implementation.  It has no row in ``apps`` — and
# ``service_defaults.app_id`` is a foreign key into that table — so the router is the *implicit*
# provider rather than a registered one: it is what you get when no app has claimed the service.
ROUTER_DNS_PROVIDER_ID = "_openhost_router_dns"

CHALLENGE_TTL_SECONDS = 60


class DnsBackendError(RuntimeError):
    """The backend could not carry out the operation."""


class UnknownZone(DnsBackendError):
    """No configured zone covers the requested name."""


class DnsBackend(Protocol):
    """Mirrors the ``dns`` service API rather than inventing a second vocabulary, so the remote
    implementation is a thin shim and the local one is what the router's provider serves."""

    def zones(self) -> list[str]: ...

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]: ...

    def set_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """Replace each ``(name, type)`` RRset named in ``records``."""

    def append_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]: ...

    def delete_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """Remove records.  One with ``data is None`` clears its whole RRset."""

    @property
    def propagation_timeout_seconds(self) -> float: ...


@attr.s(auto_attribs=True, frozen=True)
class ZoneMatch:
    zone: str
    name: str


def split_fqdn(fqdn: str, zones: list[str]) -> ZoneMatch:
    """Locate ``fqdn`` in the most specific zone containing it, as (zone, zone-relative name).

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


# ─── DNS-01 helpers, usable against either backend ───


def publish_txt(backend: DnsBackend, fqdn: str, values: list[str]) -> None:
    """Publish challenge TXT values at ``fqdn``, replacing whatever is there.

    Set rather than append, so a run that died before cleaning up doesn't leave stale tokens for
    the next attempt.  The short explicit TTL keeps the previous run's token from being served out
    of a resolver cache during a renewal.
    """
    match = split_fqdn(fqdn, backend.zones())
    backend.set_records(
        match.zone,
        [DnsRecord(name=match.name, type="TXT", ttl=CHALLENGE_TTL_SECONDS, data=v) for v in values],
    )
    logger.info(f"Published {len(values)} TXT record(s) at {fqdn} in zone {match.zone}")


def clear_txt(backend: DnsBackend, fqdn: str) -> None:
    match = split_fqdn(fqdn, backend.zones())
    backend.delete_records(match.zone, [DnsRecord(name=match.name, type="TXT", data=None)])
    logger.info(f"Cleared TXT records at {fqdn} in zone {match.zone}")


# Deliberately not the host's own resolver: with the local backend that would query CoreDNS
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

    False on timeout, and the caller should proceed anyway: the ACME retry loop is the fallback,
    and some providers are slower than any timeout worth blocking on.
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


# ─── local: our own CoreDNS zone files ───

# CoreDNS's `reload 2s` is instant by comparison; the real wait is the external check, which the
# delegation makes unavoidable.
_LOCAL_PROPAGATION_TIMEOUT_SECONDS = 120.0


@attr.s(auto_attribs=True, frozen=True)
class LocalZoneFileBackend:
    """Used when the space is its own DNS provider.  Also backs the router's ``dns`` provider, so
    an app's record and a challenge record land in exactly the same place."""

    # Snapshotted per operation from the live domain set, so a stale map can't outlive a domain
    # change.
    zone_paths: dict[str, Path]

    @classmethod
    def create(cls, config: Config, db: sqlite3.Connection) -> LocalZoneFileBackend:
        return cls(zone_paths={z.domain: z.zonefile_path for z in public_dns_zones(config, db)})

    def zones(self) -> list[str]:
        return sorted(self.zone_paths)

    @property
    def propagation_timeout_seconds(self) -> float:
        return _LOCAL_PROPAGATION_TIMEOUT_SECONDS

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]:
        records = zonefile.read_records(self._path(zone), zone)
        if name is not None:
            records = [r for r in records if r.name == name.strip().lower()]
        if rrtype is not None:
            records = [r for r in records if r.type == rrtype.strip().upper()]
        return records

    def set_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        path, normalized = self._prepare(zone, records)
        logger.info(f"Setting {len(normalized)} record(s) in local zone {zone}")
        return zonefile.set_records(path, zone, normalized)

    def append_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        path, normalized = self._prepare(zone, records)
        logger.info(f"Appending {len(normalized)} record(s) to local zone {zone}")
        return zonefile.append_records(path, zone, normalized)

    def delete_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        path, normalized = self._prepare(zone, records, allow_rrset_selector=True)
        logger.info(f"Deleting {len(normalized)} record(s) from local zone {zone}")
        return zonefile.delete_records(path, zone, normalized)

    def _path(self, zone: str) -> Path:
        path = self.zone_paths.get(normalize_zone(zone))
        if path is None:
            raise UnknownZone(f"{zone!r} is not a zone this instance serves (zones: {', '.join(self.zones())})")
        if not path.exists():
            raise DnsBackendError(f"zone file for {zone} has not been created yet")
        return path

    def _prepare(
        self, zone: str, records: list[DnsRecord], *, allow_rrset_selector: bool = False
    ) -> tuple[Path, list[DnsRecord]]:
        """Resolve and validate everything before touching the file, so one bad record in a batch
        fails the whole write rather than half-applying it."""
        path = self._path(zone)
        return path, [normalize_record(r, zone, allow_rrset_selector=allow_rrset_selector) for r in records]


# ─── remote: whichever app provides the dns service ───

# External providers are orders of magnitude slower than a local file, and some registrars take
# minutes to publish.
_REMOTE_PROPAGATION_TIMEOUT_SECONDS = 600.0
_REQUEST_TIMEOUT_SECONDS = 60.0

# Reserved consumer identity for the router's own calls.  App names are DNS-label-like (see
# core.app_id), so the leading underscore cannot collide with a real one.
ROUTER_CONSUMER_ID = "_openhost_router"
ROUTER_CONSUMER_NAME = "OpenHost Router"


def router_grants(domains: list[str]) -> list[dict[str, Any]]:
    """The grants the router asserts for itself: challenge TXT plus the records it maintains.

    Narrow rather than a blanket ``**``, because the provider app's audit log is one of the few
    places an owner sees what touched their registrar, and it should say something useful.  Each
    domain contributes two shapes, since the provider's zone may be the domain itself or a parent.
    """
    grants: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, rrtype: str) -> None:
        if (name, rrtype) not in seen:
            seen.add((name, rrtype))
            grants.append({"grant": {"name": name, "type": rrtype, "access": "rw"}, "scope": "global"})

    for domain in domains:
        for base in ("", domain.strip(".").lower()):
            add(f"_acme-challenge.{base}" if base else "_acme-challenge", "TXT")
            add(base or APEX, "A")
            add(f"*.{base}" if base else "*", "A")
    return grants


def router_managed_domains(db: sqlite3.Connection) -> list[str]:
    return [d.name_no_port for d in effective_domains(db) if not d.mdns]


@attr.s(auto_attribs=True, frozen=True)
class ServiceDnsBackend:
    """Talks the ``dns`` service API to a provider app over loopback.

    The router has no app token, but it is the sole authority for the ``X-OpenHost-*`` identity
    headers in the first place, so it dials the provider's port directly and injects the same
    headers the service proxy would have.
    """

    base_url: str
    permissions_header: str
    client: httpx.Client

    @classmethod
    def create(cls, port: int, endpoint: str, domains: list[str]) -> ServiceDnsBackend:
        return cls(
            base_url=f"http://127.0.0.1:{port}/{endpoint.strip('/')}",
            permissions_header=json.dumps(router_grants(domains)),
            client=httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS),
        )

    def close(self) -> None:
        self.client.close()

    @property
    def propagation_timeout_seconds(self) -> float:
        return _REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def zones(self) -> list[str]:
        zones = self._call("/zones", {}).get("zones")
        if not isinstance(zones, list):
            raise DnsBackendError("DNS service returned no zone list")
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
        return self._write("/records/delete", zone, records, allow_rrset_selector=True)

    def _write(
        self, path: str, zone: str, records: list[DnsRecord], *, allow_rrset_selector: bool = False
    ) -> list[DnsRecord]:
        normalized = [normalize_record(r, zone, allow_rrset_selector=allow_rrset_selector) for r in records]
        return self._records_from(self._call(path, {"zone": zone, "records": [_to_wire(r) for r in normalized]}))

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                self.base_url + path,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": ROUTER_CONSUMER_ID,
                    "X-OpenHost-Consumer-Name": ROUTER_CONSUMER_NAME,
                    "X-OpenHost-Permissions": self.permissions_header,
                },
            )
        except httpx.HTTPError as e:
            raise DnsBackendError(f"DNS service unreachable at {self.base_url}: {e}") from e

        try:
            body = response.json()
        except ValueError as e:
            raise DnsBackendError(f"DNS service returned non-JSON ({response.status_code})") from e
        if not isinstance(body, dict):
            raise DnsBackendError(f"DNS service returned unexpected body: {body!r}")

        # 207 is a partial success across zones; per-zone errors surface in _records_from.
        if response.status_code not in (200, 207):
            error, message = body.get("error", "unknown_error"), body.get("message", "")
            if error == "unknown_zone":
                raise UnknownZone(f"DNS service does not manage this zone: {message}")
            raise DnsBackendError(f"DNS service call to {path} failed ({response.status_code}): {error} {message}")
        return body

    def _records_from(self, body: dict[str, Any]) -> list[DnsRecord]:
        """Flatten the per-zone results.  We always name exactly one zone, so a failed zone is a
        failed operation even when the overall status was 207."""
        results = body.get("results")
        if not isinstance(results, list):
            raise DnsBackendError(f"DNS service returned no results: {body!r}")
        out: list[DnsRecord] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            if not result.get("ok"):
                raise DnsBackendError(f"DNS service failed for zone {result.get('zone')}: {result.get('error')}")
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


# ─── which one applies ───


def dns_provider_id(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (DNS_SERVICE_URL,)).fetchone()
    return row["app_id"] if row else ROUTER_DNS_PROVIDER_ID


def uses_local_dns(db: sqlite3.Connection) -> bool:
    """True when this instance answers its own DNS, so CoreDNS must serve the public zones."""
    return dns_provider_id(db) == ROUTER_DNS_PROVIDER_ID


@contextmanager
def dns_backend(config: Config, db: sqlite3.Connection) -> Iterator[DnsBackend]:
    """The backend for the router's own writes.

    Deciding by service default rather than a separate config knob keeps one answer to "where does
    this space's DNS live".  A local default is dispatched in-process — no point making the router
    talk to itself over loopback.
    """
    provider_id = dns_provider_id(db)
    if provider_id == ROUTER_DNS_PROVIDER_ID:
        yield LocalZoneFileBackend.create(config, db)
        return

    _, port, _, endpoint = resolve_provider(
        DNS_SERVICE_URL, f">={DNS_SERVICE_VERSION}", db, provider_app_id=provider_id
    )
    backend = ServiceDnsBackend.create(port=port, endpoint=endpoint, domains=router_managed_domains(db))
    try:
        yield backend
    finally:
        backend.close()
