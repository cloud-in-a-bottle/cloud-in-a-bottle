"""How the router itself writes DNS records.

It needs exactly three things — publish a DNS-01 challenge, clear it, and point a domain's address
records at an IP — and they must work whether this space serves its own DNS or an app forwards to
a registrar.  ``core.service_client`` hides that difference entirely, so this is just the three
operations expressed against the ``dns`` service API.

Grants are asserted per call, covering exactly the records that call touches: the narrowest thing
the router can claim, and the most useful line in a provider app's audit log.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.apps import start_app_process
from compute_space.core.builtin_services import builtin_for
from compute_space.core.containers import is_container_running
from compute_space.core.dns.service_api import APEX
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import WILDCARD
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import Grant
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.service_client import ServiceCallError
from compute_space.core.service_client import ServiceEndpoint
from compute_space.core.service_client import service_client

# A short explicit TTL, so the previous run's token can't be served out of a resolver cache during
# the next renewal.
CHALLENGE_TTL_SECONDS = 60

_ADDRESS_NAMES = (APEX, "ns", "*")

# CoreDNS reloads within seconds; an external registrar can take minutes to publish.
LOCAL_PROPAGATION_TIMEOUT_SECONDS = 120.0
REMOTE_PROPAGATION_TIMEOUT_SECONDS = 600.0


def uses_local_dns(db: sqlite3.Connection) -> bool:
    """True when this instance answers its own DNS, so CoreDNS must serve the public zones."""
    return builtin_for(DNS_SERVICE_URL, db) is not None


# How long boot will wait for a DNS provider app before giving up and letting the renewal thread
# take over.  Generous enough for a container to come back after a reboot, short enough that a
# broken app doesn't hold the instance offline.
PROVIDER_START_TIMEOUT_SECONDS = 90.0
_PROVIDER_POLL_SECONDS = 2.0


def ensure_dns_provider_running(config: Config, db: sqlite3.Connection, timeout: float | None = None) -> bool:
    """Make sure whatever provides the ``dns`` service can answer, before a cert is acquired.

    Returns True when the provider is ready.  With the router serving its own DNS that is
    immediate.  When an app provides it, the container may be down after a reboot, so start it and
    wait — bounded, because a rebuild can take minutes and the instance should not stay offline for
    one.  On timeout the caller carries on without a cert and the renewal thread retries.

    Only the provider app is touched, not every app: this is about the one dependency the cert has.
    """
    if uses_local_dns(db):
        return True

    row = db.execute(
        """SELECT a.app_id, a.name, a.status, a.container_id
           FROM service_providers_v2 sp JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ?""",
        (DNS_SERVICE_URL,),
    ).fetchone()
    if row is None:
        logger.warning("An app is the configured DNS provider but no such app is installed")
        return False

    if row["status"] == "running" and row["container_id"] and is_container_running(row["container_id"]):
        return True

    logger.info(f"Starting DNS provider app {row['name']} before acquiring a certificate")
    try:
        start_app_process(row["app_id"], db, config)
    except Exception:
        logger.exception(f"Could not start DNS provider app {row['name']}")
        return False

    deadline = time.monotonic() + (PROVIDER_START_TIMEOUT_SECONDS if timeout is None else timeout)
    while time.monotonic() < deadline:
        current = db.execute("SELECT status, container_id FROM apps WHERE app_id = ?", (row["app_id"],)).fetchone()
        if current and current["status"] == "running" and current["container_id"]:
            if is_container_running(current["container_id"]):
                logger.info(f"DNS provider app {row['name']} is up")
                return True
        if current and current["status"] == "error":
            logger.warning(f"DNS provider app {row['name']} failed to start")
            return False
        time.sleep(_PROVIDER_POLL_SECONDS)

    logger.warning(f"DNS provider app {row['name']} not ready in time; deferring certificate acquisition")
    return False


def router_managed_domains(db: sqlite3.Connection) -> list[str]:
    return [d.name_no_port for d in effective_domains(db) if not d.mdns]


@attr.s(auto_attribs=True, frozen=True)
class DnsClient:
    service: ServiceEndpoint
    # How long to wait for a write to be visible externally.  Set by ``dns_client`` from the
    # provider, since a registrar is orders of magnitude slower than our own zone file.
    propagation_timeout_seconds: float = REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def zones(self) -> list[str]:
        zones = self.service.call("/zones", {}, [Grant(WILDCARD, WILDCARD, "r")]).get("zones")
        if not isinstance(zones, list):
            raise ServiceCallError("DNS service returned no zone list")
        return [str(z) for z in zones]

    def publish_challenge(self, domain: str, values: list[str]) -> None:
        """Replace whatever is at ``_acme-challenge``, so a run that died before cleaning up
        doesn't leave stale tokens for the next attempt."""
        zone, name = self._locate(f"_acme-challenge.{domain}")
        self._write("set", zone, [DnsRecord(name, "TXT", CHALLENGE_TTL_SECONDS, v) for v in values])
        logger.info(f"Published {len(values)} challenge record(s) for {domain} in zone {zone}")

    def clear_challenge(self, domain: str) -> None:
        zone, name = self._locate(f"_acme-challenge.{domain}")
        # No data means "whatever is there now", which is what a cleanup path needs.
        self._write("delete", zone, [DnsRecord(name, "TXT")])
        logger.info(f"Cleared challenge records for {domain} in zone {zone}")

    def set_address(self, domain: str, ip: str, ttl: int = 300) -> None:
        """Point the domain's apex, nameserver, and wildcard A records at ``ip``."""
        zone, base = self._locate(domain)
        prefix = "" if base == APEX else base
        names = [(prefix or APEX) if n == APEX else (f"{n}.{prefix}" if prefix else n) for n in _ADDRESS_NAMES]
        self._write("set", zone, [DnsRecord(n, "A", ttl, ip) for n in names])
        logger.info(f"Pointed {len(names)} address record(s) for {domain} at {ip}")

    def _locate(self, fqdn: str) -> tuple[str, str]:
        """The most specific configured zone containing ``fqdn``, and the name relative to it.

        Longest suffix wins, so an instance managing both ``example.com`` and a delegated
        ``host.example.com`` writes into the more specific one.
        """
        target = normalize_zone(fqdn)
        candidates = [z for z in map(normalize_zone, self.zones()) if target == z or target.endswith("." + z)]
        if not candidates:
            raise ServiceCallError(f"no configured DNS zone covers {fqdn!r}")
        zone = max(candidates, key=len)
        return zone, target[: -len(zone)].rstrip(".") or APEX

    def _write(self, op: str, zone: str, records: list[DnsRecord]) -> None:
        payload = {"zone": zone, "records": [_to_wire(r) for r in records]}
        body = self.service.call(f"/records/{op}", payload, [Grant(r.name, r.type, "rw") for r in records])
        # We always name exactly one zone, so a failed zone is a failed operation even under 207.
        for result in body.get("results") or []:
            if isinstance(result, dict) and not result.get("ok"):
                raise ServiceCallError(f"DNS service failed for {result.get('zone')}: {result.get('error')}")


def _to_wire(record: DnsRecord) -> dict[str, Any]:
    """Data is omitted for an RRset selector: that is how the API spells "delete whatever is at
    this name and type"."""
    wire: dict[str, Any] = {"name": record.name, "type": record.type, "ttl": record.ttl}
    if record.data is not None:
        wire["data"] = record.data
    return wire


@contextmanager
def dns_client(config: Config, db: sqlite3.Connection) -> Iterator[DnsClient]:
    timeout = LOCAL_PROPAGATION_TIMEOUT_SECONDS if uses_local_dns(db) else REMOTE_PROPAGATION_TIMEOUT_SECONDS
    with service_client(DNS_SERVICE_URL, config, db) as service:
        yield DnsClient(service, timeout)


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
