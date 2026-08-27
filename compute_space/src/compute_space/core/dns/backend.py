"""The interface every DNS backend implements, and the propagation check they share.

Two implementations: ``local.LocalZoneFileBackend`` writes the CoreDNS zone files this instance
serves itself, and ``remote.ServiceDnsBackend`` calls whatever app provides the ``dns`` service
(the external-dns-connector, talking to a registrar).  Callers — cert acquisition, dynamic DNS —
work against this interface and never learn which one they got.

Zone resolution belongs here rather than in the callers: a backend knows its own zone set, and
mapping ``_acme-challenge.host.example.com`` to zone ``example.com`` plus relative name
``_acme-challenge.host`` is exactly the step every caller would otherwise reimplement.
"""

from __future__ import annotations

import subprocess
import time
from typing import Protocol

import attr

from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import normalize_zone
from compute_space.core.logging import logger


class DnsBackendError(RuntimeError):
    """The backend could not carry out the operation."""


class UnknownZone(DnsBackendError):
    """No configured zone covers the requested name."""


@attr.s(auto_attribs=True, frozen=True)
class ZoneMatch:
    """Where a fully-qualified name lives: which zone holds it, under what relative name."""

    zone: str
    name: str


def split_fqdn(fqdn: str, zones: list[str]) -> ZoneMatch:
    """Match ``fqdn`` to the most specific zone that contains it.

    Longest suffix wins, so an instance that manages both ``example.com`` and a delegated
    ``host.example.com`` writes into the more specific one rather than the parent.
    """
    target = normalize_zone(fqdn)
    best: str | None = None
    for zone in zones:
        z = normalize_zone(zone)
        if target == z or target.endswith("." + z):
            if best is None or len(z) > len(best):
                best = z
    if best is None:
        raise UnknownZone(f"no configured DNS zone covers {fqdn!r} (zones: {', '.join(zones) or 'none'})")
    relative = target[: -len(best)].rstrip(".")
    return ZoneMatch(zone=best, name=relative or "@")


class DnsBackend(Protocol):
    """Read and write DNS records, wherever this space's DNS actually lives.

    Mirrors the ``dns`` service API rather than inventing a second vocabulary, so the remote
    implementation is a thin HTTP shim and the local one is what the router's own service
    provider serves.
    """

    def zones(self) -> list[str]:
        """Every zone this backend can write to."""

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]:
        """Records in ``zone``, optionally filtered to an exact name and/or type."""

    def set_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """Replace each ``(name, type)`` RRset named in ``records``."""

    def append_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """Add records without disturbing existing ones at the same name and type."""

    def delete_records(self, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
        """Remove records.  A record with ``data is None`` clears its whole RRset."""

    @property
    def propagation_timeout_seconds(self) -> float:
        """How long to wait for a write to become visible to an outside resolver."""


# ─── shared helpers, usable against any backend ───


def resolve_fqdn(backend: DnsBackend, fqdn: str) -> ZoneMatch:
    return split_fqdn(fqdn, backend.zones())


def publish_txt(backend: DnsBackend, fqdn: str, values: list[str]) -> None:
    """Publish DNS-01 challenge TXT values at ``fqdn``, replacing anything already there.

    Set rather than append: a run that died before cleaning up leaves stale tokens behind, and
    replacing the RRset outright means the next attempt starts from a known state.
    """
    match = resolve_fqdn(backend, fqdn)
    backend.set_records(
        match.zone,
        # A short explicit TTL, not the zone default: a renewal must not have the CA (or our own
        # propagation check) served the previous run's token out of a resolver cache.
        [DnsRecord(name=match.name, type="TXT", ttl=CHALLENGE_TTL_SECONDS, data=v) for v in values],
    )
    logger.info(f"Published {len(values)} TXT record(s) at {fqdn} in zone {match.zone}")


def clear_txt(backend: DnsBackend, fqdn: str) -> None:
    """Remove every TXT record at ``fqdn``, whatever it currently holds."""
    match = resolve_fqdn(backend, fqdn)
    backend.delete_records(match.zone, [DnsRecord(name=match.name, type="TXT", data=None)])
    logger.info(f"Cleared TXT records at {fqdn} in zone {match.zone}")


CHALLENGE_TTL_SECONDS = 60

# Resolver used to confirm a challenge record is visible from outside.  Deliberately not the
# host's own resolver: with the local backend that would query CoreDNS directly and confirm
# nothing about whether the delegation works.
_PROPAGATION_RESOLVER = "8.8.8.8"


def wait_for_txt_propagation(
    fqdn: str,
    expected_values: list[str],
    timeout: float,
    interval: float = 5,
    resolver: str = _PROPAGATION_RESOLVER,
) -> bool:
    """Poll an external resolver until every expected TXT value is visible.

    Returns True if all values were found, False on timeout.  On timeout the caller should
    proceed anyway — the ACME retry loop is the fallback, and some providers are slower than any
    timeout we would want to block on.
    """
    deadline = time.monotonic() + timeout
    expected_set = set(expected_values)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["dig", f"@{resolver}", fqdn, "TXT", "+short", "+timeout=5", "+tries=1"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # dig +short TXT output looks like: "token-value-here"
            found = {line.strip().strip('"') for line in result.stdout.strip().splitlines()}
            if expected_set <= found:
                logger.info(f"DNS propagation confirmed: {fqdn} has all {len(expected_set)} expected TXT value(s)")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        remaining = deadline - time.monotonic()
        logger.info(f"Waiting for DNS propagation of {fqdn} ({remaining:.0f}s remaining)")
        time.sleep(interval)

    logger.warning(f"DNS propagation timeout: {fqdn} not fully visible after {timeout}s, proceeding anyway")
    return False
