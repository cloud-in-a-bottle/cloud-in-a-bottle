"""How the router itself writes DNS records.

It needs exactly three things — publish a DNS-01 challenge, clear it, and point a domain's address
records at an IP — and they must work whether this space serves its own DNS or an app forwards to
a registrar.  So this speaks the ``dns`` service API, dispatching in-process when the router is
the provider (no point talking to ourselves over loopback) and over the app's loopback port
otherwise.

The router has no app token, but it is the sole authority for the ``X-OpenHost-*`` identity
headers in the first place, so it asserts the same ones the service proxy would have injected.
The grants it asserts are narrow — challenge TXT plus the address records it maintains — because a
provider app's audit log is one of the few places an owner sees what touched their registrar.
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
from compute_space.core.dns.coredns_provider.service import handle_dns_call
from compute_space.core.dns.service_api import APEX
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DNS_SERVICE_VERSION
from compute_space.core.dns.service_api import ROUTER_DNS_PROVIDER_ID
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import Grant
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.services_v2 import resolve_provider

# A short explicit TTL, so the previous run's token can't be served out of a resolver cache during
# the next renewal.
CHALLENGE_TTL_SECONDS = 60

_ADDRESS_NAMES = (APEX, "ns", "*")
_LOCAL_PROPAGATION_TIMEOUT_SECONDS = 120.0
_REMOTE_PROPAGATION_TIMEOUT_SECONDS = 600.0
_REQUEST_TIMEOUT_SECONDS = 60.0


class DnsServiceError(RuntimeError):
    """The DNS service could not carry out the operation."""


def dns_provider_id(db: sqlite3.Connection) -> str:
    """The router is the implicit provider: it has no row in ``apps``, and
    ``service_defaults.app_id`` is a foreign key into that table."""
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (DNS_SERVICE_URL,)).fetchone()
    return row["app_id"] if row else ROUTER_DNS_PROVIDER_ID


def uses_local_dns(db: sqlite3.Connection) -> bool:
    """True when this instance answers its own DNS, so CoreDNS must serve the public zones."""
    return dns_provider_id(db) == ROUTER_DNS_PROVIDER_ID


def router_managed_domains(db: sqlite3.Connection) -> list[str]:
    return [d.name_no_port for d in effective_domains(db) if not d.mdns]


def router_grants(domains: list[str]) -> list[Grant]:
    """Each domain contributes two shapes, since the provider's zone may be the domain itself or a
    parent holding it."""
    names: list[tuple[str, str]] = []
    for domain in domains:
        for base in ("", domain.strip(".").lower()):
            names.append((f"_acme-challenge.{base}" if base else "_acme-challenge", "TXT"))
            for label in _ADDRESS_NAMES:
                names.append((base or APEX, "A") if label == APEX else (f"{label}.{base}" if base else label, "A"))
    return [Grant(name=n, type=t, access="rw") for n, t in dict.fromkeys(names)]


@attr.s(auto_attribs=True)
class DnsClient:
    config: Config
    db: sqlite3.Connection
    provider_id: str
    grants: list[Grant]
    # Set only when an app provides the service.
    endpoint_url: str | None = None
    http: httpx.Client | None = None

    @property
    def is_local(self) -> bool:
        return self.provider_id == ROUTER_DNS_PROVIDER_ID

    @property
    def propagation_timeout_seconds(self) -> float:
        return _LOCAL_PROPAGATION_TIMEOUT_SECONDS if self.is_local else _REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def zones(self) -> list[str]:
        zones = self._call("/zones", {}).get("zones")
        if not isinstance(zones, list):
            raise DnsServiceError("DNS service returned no zone list")
        return [str(z) for z in zones]

    def publish_challenge(self, domain: str, values: list[str]) -> None:
        """Replace whatever is at ``_acme-challenge``, so a run that died before cleaning up
        doesn't leave stale tokens for the next attempt."""
        zone, name = self._locate(f"_acme-challenge.{domain}")
        self._records("set", zone, [DnsRecord(name, "TXT", CHALLENGE_TTL_SECONDS, v) for v in values])
        logger.info(f"Published {len(values)} challenge record(s) for {domain} in zone {zone}")

    def clear_challenge(self, domain: str) -> None:
        zone, name = self._locate(f"_acme-challenge.{domain}")
        # No data means "whatever is there now", which is what a cleanup path needs.
        self._records("delete", zone, [DnsRecord(name, "TXT")])
        logger.info(f"Cleared challenge records for {domain} in zone {zone}")

    def set_address(self, domain: str, ip: str, ttl: int = 300) -> None:
        """Point the domain's apex, nameserver, and wildcard A records at ``ip``."""
        zone, base = self._locate(domain)
        prefix = "" if base == APEX else base
        names = [(prefix or APEX) if n == APEX else (f"{n}.{prefix}" if prefix else n) for n in _ADDRESS_NAMES]
        self._records("set", zone, [DnsRecord(n, "A", ttl, ip) for n in names])
        logger.info(f"Pointed {len(names)} address record(s) for {domain} at {ip}")

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    def _locate(self, fqdn: str) -> tuple[str, str]:
        """Find the most specific configured zone containing ``fqdn``, and the name relative to it."""
        target = normalize_zone(fqdn)
        best = max(
            (
                normalize_zone(z)
                for z in self.zones()
                if target == normalize_zone(z) or target.endswith("." + normalize_zone(z))
            ),
            key=len,
            default=None,
        )
        if best is None:
            raise DnsServiceError(f"no configured DNS zone covers {fqdn!r}")
        return best, target[: -len(best)].rstrip(".") or APEX

    def _records(self, op: str, zone: str, records: list[DnsRecord]) -> None:
        payload = {"zone": zone, "records": [_to_wire(r) for r in records]}
        body = self._call(f"/records/{op}", payload)
        # We always name exactly one zone, so a failed zone is a failed operation even under 207.
        for result in body.get("results") or []:
            if isinstance(result, dict) and not result.get("ok"):
                raise DnsServiceError(f"DNS service failed for {result.get('zone')}: {result.get('error')}")

    def _call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self._dispatch(path, payload)
        if status not in (200, 207):
            raise DnsServiceError(
                f"DNS service call to {path} failed ({status}): "
                f"{body.get('error', 'unknown_error')} {body.get('message', '')}"
            )
        return body

    def _dispatch(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.is_local:
            return handle_dns_call(path, payload, self.grants, self.config, self.db)

        assert self.endpoint_url is not None and self.http is not None
        try:
            response = self.http.post(
                self.endpoint_url + path,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": "_openhost_router",
                    "X-OpenHost-Consumer-Name": "OpenHost Router",
                    "X-OpenHost-Permissions": json.dumps([g.as_permission() for g in self.grants]),
                },
            )
            body = response.json()
        except httpx.HTTPError as e:
            raise DnsServiceError(f"DNS service unreachable at {self.endpoint_url}: {e}") from e
        except ValueError as e:
            raise DnsServiceError(f"DNS service returned non-JSON ({response.status_code})") from e
        if not isinstance(body, dict):
            raise DnsServiceError(f"DNS service returned unexpected body: {body!r}")
        return response.status_code, body


def _to_wire(record: DnsRecord) -> dict[str, Any]:
    """Data is omitted for an RRset selector: that is how the API spells "delete whatever is at
    this name and type"."""
    wire: dict[str, Any] = {"name": record.name, "type": record.type, "ttl": record.ttl}
    if record.data is not None:
        wire["data"] = record.data
    return wire


@contextmanager
def dns_client(config: Config, db: sqlite3.Connection) -> Iterator[DnsClient]:
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


# Deliberately not the host's own resolver: with the router serving DNS that would query CoreDNS
# directly and confirm nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"


def wait_for_challenge_propagation(
    domain: str, expected_values: list[str], timeout: float, interval: float = 5
) -> bool:
    """Poll an external resolver until every expected value is visible.

    False on timeout, and the caller should proceed anyway: the ACME retry loop is the fallback,
    and some providers are slower than any timeout worth blocking on.
    """
    fqdn = f"_acme-challenge.{domain}"
    deadline = time.monotonic() + timeout
    expected = set(expected_values)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["dig", f"@{_PROPAGATION_RESOLVER}", fqdn, "TXT", "+short", "+timeout=5", "+tries=1"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if expected <= {line.strip().strip('"') for line in result.stdout.strip().splitlines()}:
                logger.info(f"DNS propagation confirmed for {fqdn}")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        logger.info(f"Waiting for DNS propagation of {fqdn} ({deadline - time.monotonic():.0f}s remaining)")
        time.sleep(interval)

    logger.warning(f"DNS propagation timeout for {fqdn} after {timeout}s, proceeding anyway")
    return False
