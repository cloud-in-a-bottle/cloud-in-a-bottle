"""What the ``dns`` service actually does, with no HTTP in it.

Every record applies to every managed zone, so a request addresses ``ALL_ZONES`` or nothing: this
provider serves a set of zones that are aliases for one space, and a record that existed in only
some of them would make them differ.  A request naming a single zone is refused rather than
narrowed, so a caller can't believe it scoped a write that in fact applied everywhere.

Reads are filtered by grant, writes are authorized and validated as a batch and then applied to the
store, and every write re-renders the zone files CoreDNS serves.  Failures are raised as the
exceptions below; turning those into status codes is ``routes``' job.
"""

from __future__ import annotations

import sqlite3

import attr

from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import write_zone_file
from compute_space.core.dns.coredns_provider.grants import Grant
from compute_space.core.dns.coredns_provider.grants import can_read
from compute_space.core.dns.coredns_provider.grants import can_write
from compute_space.core.dns.coredns_provider.records import normalize as normalize_record
from compute_space.core.dns.coredns_provider.settings import DnsSettings
from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.logging import logger

_OPS = {"set": store.set_records, "append": store.append_records, "delete": store.delete_records}
WRITE_OPS = frozenset(_OPS)


class DnsOperationError(Exception):
    """A failure the API has an error code for.  Plain exceptions, not framework ones: the code
    that turns these into responses lives in the web layer."""

    error_code = "invalid_request"


class UnknownZone(DnsOperationError):
    """A zone this instance does not serve.

    Which is every named zone: this provider is addressable only as ``ALL_ZONES``.
    """

    error_code = "unknown_zone"


class NoZonesConfigured(DnsOperationError):
    """The instance serves no zones at all, so there is nothing to write to."""

    error_code = "no_zones_configured"


@attr.s(auto_attribs=True, frozen=True)
class PermissionDenied(DnsOperationError):
    """The caller holds no grant covering a record it tried to touch."""

    error_code = "permission_required"

    name: str
    type: str
    access: str


@attr.s(auto_attribs=True, frozen=True)
class ZoneResult:
    """The outcome of one request.  Still per-zone on the wire, because a connector app fans out
    across zones that can fail independently; this provider always reports the one ``ALL_ZONES``
    result, since a record either applies to every zone or to none."""

    zone: str
    ok: bool
    records: list[DnsRecord] = attr.ib(factory=list)
    error: str | None = None


def require_all_zones(requested: str) -> None:
    """Refuse anything but ``ALL_ZONES``, which is the only zone this provider is addressable as.

    The message names no zone.  Saying whether *this* one is served would answer, one guess at a
    time, the question ``/zones`` was removed for.
    """
    if requested and requested != ALL_ZONES:
        raise UnknownZone(f"this provider serves no zone by name; address every zone at once with {ALL_ZONES!r}")


def read(
    grants: list[Grant], requested: str, name: str | None, rrtype: str | None, db: sqlite3.Connection
) -> list[ZoneResult]:
    """Records the caller may see.

    An app with no grants can read nothing, and gets an empty result rather than an error: telling
    it whether the zone set is empty is itself more than it may know.  Ungranted records are
    omitted rather than refused, so a narrowly scoped app sees just its own.
    """
    require_all_zones(requested)
    if not grants:
        return []
    visible = [
        r
        for r in store.all_records(db)
        if (name is None or r.name == name)
        and (rrtype is None or r.type == rrtype)
        and can_read(grants, r.name, r.type)
    ]
    return [ZoneResult(zone=ALL_ZONES, ok=True, records=visible)]


def write(
    grants: list[Grant],
    requested: str,
    op: str,
    records: list[DnsRecord],
    zones: tuple[DnsZone, ...],
    settings: DnsSettings,
    db: sqlite3.Connection,
) -> list[ZoneResult]:
    """Apply one write op, then re-render every zone.

    The whole batch is authorized and validated before anything is written, so a partially
    permitted request applies none of itself.
    """
    require_all_zones(requested)
    validated = []
    for record in records:
        checked = normalize_record(record, allow_rrset_selector=op == "delete")
        if not can_write(grants, checked.name, checked.type):
            raise PermissionDenied(checked.name, checked.type, "rw")
        validated.append(checked)

    if not zones:
        raise NoZonesConfigured("no DNS zones are configured on this instance")

    # The store write and the render are one outcome: a record that is saved but not rendered is
    # not being served, so reporting success would be a lie.
    try:
        applied = _OPS[op](db, validated)
        for zone in zones:
            write_zone_file(zone, settings, db)
    except Exception as e:
        logger.warning(f"DNS service {op} failed: {e}")
        return [ZoneResult(zone=ALL_ZONES, ok=False, error=str(e))]

    logger.info(f"DNS service: {op} {len(applied)} record(s) across {len(zones)} zone(s)")
    return [ZoneResult(zone=ALL_ZONES, ok=True, records=applied)]
