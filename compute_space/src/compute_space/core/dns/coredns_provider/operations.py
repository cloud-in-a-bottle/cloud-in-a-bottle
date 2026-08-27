"""What the ``dns`` service actually does, with no HTTP in it.

Reads are filtered by grant, writes are authorized and validated as a batch and then applied to
the store, and every write re-renders the zone file CoreDNS serves.  Failures are raised as the
exceptions below; turning those into status codes is ``routes``' job.
"""

from __future__ import annotations

import sqlite3

import attr

from compute_space.config import Config
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.coredns import write_zone_file
from compute_space.core.dns.coredns_provider.grants import Grant
from compute_space.core.dns.coredns_provider.grants import can_read
from compute_space.core.dns.coredns_provider.grants import can_write
from compute_space.core.dns.coredns_provider.records import normalize as normalize_record
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.logging import logger

_OPS = {"set": store.set_records, "append": store.append_records, "delete": store.delete_records}
WRITE_OPS = frozenset(_OPS)


class UnknownZone(Exception):
    """A zone this instance does not serve."""


class NoZonesConfigured(Exception):
    """The instance serves no zones at all, so there is nothing to write to."""


@attr.s(auto_attribs=True, frozen=True)
class PermissionDenied(Exception):
    """The caller holds no grant covering a record it tried to touch."""

    name: str
    type: str
    access: str


@attr.s(auto_attribs=True, frozen=True)
class ZoneResult:
    """The outcome for one zone.  Reported per zone because one provider being unhappy says
    nothing about the others."""

    zone: str
    ok: bool
    records: list[DnsRecord] = attr.ib(factory=list)
    error: str | None = None


def zone_map(config: Config, db: sqlite3.Connection) -> dict[str, DnsZone]:
    return {z.domain: z for z in public_dns_zones(config, db)}


def resolve_zones(zones: dict[str, DnsZone], requested: str) -> list[str]:
    if requested == ALL_ZONES:
        return sorted(zones)
    wanted = normalize_zone(requested)
    if wanted not in zones:
        raise UnknownZone(f"{requested!r} is not a zone this instance serves")
    return [wanted]


def read(
    zones: dict[str, DnsZone],
    grants: list[Grant],
    requested: str,
    name: str | None,
    rrtype: str | None,
    db: sqlite3.Connection,
) -> list[ZoneResult]:
    """Records the caller may see, per zone.

    An app with no grants can read nothing, so the zone is not even resolved — doing so would only
    tell it which zones exist.  Ungranted records are omitted rather than refused, so a narrowly
    scoped app sees a zone containing just its own.
    """
    if not grants:
        return []
    results = []
    for zone in resolve_zones(zones, requested or ALL_ZONES):
        visible = [
            r
            for r in store.records_for(db, zone)
            if (name is None or r.name == name)
            and (rrtype is None or r.type == rrtype)
            and can_read(grants, r.name, r.type)
        ]
        results.append(ZoneResult(zone=zone, ok=True, records=visible))
    return results


def write(
    zones: dict[str, DnsZone],
    grants: list[Grant],
    requested: str,
    op: str,
    records: list[DnsRecord],
    config: Config,
    db: sqlite3.Connection,
) -> list[ZoneResult]:
    """Apply one write op, then re-render every zone it touched.

    The whole batch is authorized and validated before the zone is resolved or anything is
    written: the other order would let an ungranted app learn which zones exist from the error it
    gets back, and would let a partially-permitted request apply its permitted half.
    """
    validated = []
    for record in records:
        checked = normalize_record(record, allow_rrset_selector=op == "delete")
        if not can_write(grants, checked.name, checked.type):
            raise PermissionDenied(checked.name, checked.type, "rw")
        validated.append(checked)

    targets = resolve_zones(zones, requested)
    if not targets:
        raise NoZonesConfigured("no DNS zones are configured on this instance")

    public_ip = effective_public_ip(config, db)
    results = []
    for zone in targets:
        applied = _OPS[op](db, zone, validated)
        if public_ip:
            write_zone_file(zones[zone], public_ip, db)
        logger.info(f"DNS service: {op} {len(applied)} record(s) in zone {zone}")
        results.append(ZoneResult(zone=zone, ok=True, records=applied))
    return results
