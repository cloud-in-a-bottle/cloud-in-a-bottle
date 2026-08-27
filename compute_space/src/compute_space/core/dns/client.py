"""Read and write DNS records from inside the router.

Records are addressed by fully-qualified name; the client resolves which configured zone holds one
and what the name is relative to it, so callers never handle zones.  ``core.service_client`` hides
whether the records live in our own CoreDNS zone files or at a registrar.

Holds nothing but the config and DB handle it was given, so construct one wherever you need it
rather than threading one through.

Deliberately record-level.  What a record *means* — that ``_acme-challenge`` is a DNS-01 token, or
that the apex and wildcard follow the instance's address — belongs to the caller: see
``core.tls.challenge`` and ``core.dns.dynamic_dns``.

Grants are asserted per call, covering exactly the records that call touches: the narrowest thing
the router can claim, and the most useful line in a provider app's audit log.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
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
from compute_space.core.dns.service_api import RecordType
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.dns.service_api import permission
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.service_client import ServiceCallError
from compute_space.core.service_client import call_service

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
    config: Config
    db: sqlite3.Connection

    @property
    def propagation_timeout_seconds(self) -> float:
        """How long a write takes to become visible externally.  Our own zone file reloads in
        seconds; a registrar can take minutes."""
        if uses_local_dns(self.db):
            return LOCAL_PROPAGATION_TIMEOUT_SECONDS
        return REMOTE_PROPAGATION_TIMEOUT_SECONDS

    def zones(self) -> list[str]:
        zones = self._call("/zones", {}, [permission(WILDCARD, WILDCARD, "r")]).get("zones")
        if not isinstance(zones, list):
            raise ServiceCallError("DNS service returned no zone list")
        return [str(z) for z in zones]

    def set_records(self, fqdn: str, rrtype: RecordType, values: list[str], ttl: int = 300) -> None:
        """Make ``values`` the only records at ``fqdn``/``rrtype``, replacing whatever is there."""
        zone, name = self._locate(fqdn)
        self._write("set", zone, [DnsRecord(name, rrtype, ttl, v) for v in values])
        logger.info(f"Set {len(values)} {rrtype} record(s) at {fqdn}")

    def delete_records(self, fqdn: str, rrtype: RecordType) -> None:
        """Remove every record at ``fqdn``/``rrtype``, whatever it currently holds.

        Sends no data, which is how the service API spells "delete whatever is there" — the only
        thing a cleanup path can do when it doesn't know what a previous run wrote.
        """
        zone, name = self._locate(fqdn)
        self._write("delete", zone, [DnsRecord(name, rrtype)])
        logger.info(f"Cleared {rrtype} records at {fqdn}")

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

    def _call(self, path: str, payload: dict[str, Any], permissions: list[dict[str, Any]]) -> dict[str, Any]:
        return call_service(DNS_SERVICE_URL, path, payload, permissions, self.config, self.db)

    def _write(self, op: str, zone: str, records: list[DnsRecord]) -> None:
        payload = {"zone": zone, "records": [_to_wire(r) for r in records]}
        body = self._call(f"/records/{op}", payload, [permission(r.name, r.type) for r in records])
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


# Deliberately not the host's own resolver: with the router serving DNS that would query CoreDNS
# directly and confirm nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"


def wait_for_records(
    fqdn: str, rrtype: RecordType, expected_values: list[str], timeout: float, interval: float = 5
) -> bool:
    """Poll an external resolver until every expected value is visible at ``fqdn``.

    False on timeout, and the caller should decide whether that matters; for ACME the retry loop is
    the fallback, and some providers are slower than any timeout worth blocking on.
    """
    deadline = time.monotonic() + timeout
    expected = set(expected_values)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["dig", f"@{_PROPAGATION_RESOLVER}", fqdn, rrtype, "+short", "+timeout=5", "+tries=1"],
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
