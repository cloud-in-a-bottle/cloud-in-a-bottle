"""Read/modify/write a CoreDNS zone file, which is the source of truth for the records it holds.

CoreDNS serves the file, so there is no second copy to drift from.  Everything goes through
dnspython rather than line surgery: master-file format has enough sharp edges (``$ORIGIN``/
``$TTL``, blank-owner continuation, parenthesized SOA, TXT quoting and 255-byte chunking) that a
regex approach breaks as soon as records get more varied than the three the template writes.
Round-tripping loses comments and layout; records survive.

Writes are serialized per file and land atomically, since the cert path and any number of apps can
now be writing the same zone.  Every write bumps the SOA serial, which is what triggers a reload.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdataset
import dns.rdatatype
import dns.zone
from dns.exception import DNSException

from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.dns.records import normalize_zone
from compute_space.core.logging import logger

# Guards the whole read-modify-write, so two callers can't both read, both edit their own copy, and
# have the second write discard the first's record.
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


class ZoneFileError(RuntimeError):
    """The zone file could not be read or written."""


def read_records(path: Path, zone: str) -> list[DnsRecord]:
    """Every record in the file, SOA included: a caller listing a zone should see what is actually
    served, and the service layer filters by grant rather than by our idea of what's interesting."""
    zone_obj = _load(path, zone)
    return [
        DnsRecord(
            name=_to_relative(zone_obj, name),
            type=dns.rdatatype.to_text(rdata.rdtype),
            ttl=ttl,
            data=rdata.to_text(origin=zone_obj.origin, relativize=False),
        )
        for name, ttl, rdata in zone_obj.iterate_rdatas()
    ]


def set_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """Replace each ``(name, type)`` RRset mentioned; leave everything else alone."""

    def apply(zone_obj: dns.zone.Zone) -> None:
        for name, rrtype in {(r.name, r.type) for r in records}:
            _delete_rrset(zone_obj, name, rrtype)
        _add_all(zone_obj, records)

    return _edit(path, zone, apply, records)


def append_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    return _edit(path, zone, lambda zone_obj: _add_all(zone_obj, records), records)


def delete_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """A record with ``data is None`` clears its whole RRset, which is how a caller cleans up a
    name without knowing what's there.  Absent records are ignored, so cleanup is safe to re-run."""

    def apply(zone_obj: dns.zone.Zone) -> None:
        for rec in records:
            if rec.is_rrset_selector:
                _delete_rrset(zone_obj, rec.name, rec.type)
            else:
                _delete_one(zone_obj, rec)

    return _edit(path, zone, apply, records)


def update_router_records(path: Path, zone: str, public_ip: str) -> None:
    """Re-point the apex, ``ns``, and wildcard A records, used in place of re-rendering the
    template — which would drop every record written through the DNS service."""
    set_records(path, zone, [DnsRecord(name=n, type="A", ttl=300, data=public_ip) for n in (APEX, "ns", "*")])
    logger.info(f"Pointed router-owned records in {path.name} at {public_ip}")


# ─── internals ───


def _lock_for(path: Path) -> threading.Lock:
    key = path.resolve() if path.exists() else path.absolute()
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _to_relative(zone_obj: dns.zone.Zone, name: dns.name.Name) -> str:
    rel = name.relativize(zone_obj.origin) if zone_obj.origin else name
    return APEX if rel.to_text() in ("@", "") else rel.to_text()


def _from_relative(name: str) -> str:
    """dnspython spells the apex as the empty relative name."""
    return "@" if name == APEX else name


def _load(path: Path, zone: str) -> dns.zone.Zone:
    try:
        return dns.zone.from_file(str(path), origin=normalize_zone(zone) + ".", relativize=False)
    except FileNotFoundError as e:
        raise ZoneFileError(f"zone file {path} does not exist") from e
    except DNSException as e:
        raise ZoneFileError(f"could not parse zone file {path}: {e}") from e


def _edit(path: Path, zone: str, apply: Callable[[dns.zone.Zone], None], records: list[DnsRecord]) -> list[DnsRecord]:
    with _lock_for(path):
        zone_obj = _load(path, zone)
        apply(zone_obj)
        _bump_serial(zone_obj)
        _write_atomic(path, zone_obj)
    return records


def _rdata(zone_obj: dns.zone.Zone, rec: DnsRecord) -> dns.rdata.Rdata:
    assert rec.data is not None
    try:
        return dns.rdata.from_text(
            dns.rdataclass.IN,
            dns.rdatatype.from_text(rec.type),
            rec.data,
            origin=zone_obj.origin,
            relativize=False,
        )
    except DNSException as e:
        raise InvalidRecord(f"invalid {rec.type} data {rec.data!r}: {e}") from e


def _add_all(zone_obj: dns.zone.Zone, records: list[DnsRecord]) -> None:
    for rec in records:
        if rec.data is None:
            raise InvalidRecord(f"record {rec.name} {rec.type} has no data")
        rdataset = zone_obj.find_rdataset(_from_relative(rec.name), dns.rdatatype.from_text(rec.type), create=True)
        rdataset.add(_rdata(zone_obj, rec), ttl=rec.ttl)


def _delete_rrset(zone_obj: dns.zone.Zone, name: str, rrtype: str) -> None:
    zone_obj.delete_rdataset(_from_relative(name), dns.rdatatype.from_text(rrtype))


def _delete_one(zone_obj: dns.zone.Zone, rec: DnsRecord) -> None:
    rdtype = dns.rdatatype.from_text(rec.type)
    rdataset = zone_obj.get_rdataset(_from_relative(rec.name), rdtype)
    if rdataset is None:
        return
    remaining = [r for r in rdataset if r != _rdata(zone_obj, rec)]
    if len(remaining) == len(rdataset):
        return
    if remaining:
        zone_obj.replace_rdataset(_from_relative(rec.name), dns.rdataset.from_rdata_list(rdataset.ttl, remaining))
    else:
        _delete_rrset(zone_obj, rec.name, rec.type)


def _bump_serial(zone_obj: dns.zone.Zone) -> None:
    rdataset = zone_obj.get_rdataset("@", dns.rdatatype.SOA)
    if rdataset is None or len(rdataset) == 0:
        raise ZoneFileError("zone has no SOA record, so its serial cannot be bumped")
    soa = rdataset[0]
    # Serials are unsigned 32-bit and wrap; RFC 1982 arithmetic makes the wrapped value compare as
    # newer, so plain modular increment is correct.
    bumped = soa.replace(serial=(soa.serial + 1) % 2**32)
    zone_obj.replace_rdataset("@", dns.rdataset.from_rdata_list(rdataset.ttl, [bumped]))


def _write_atomic(path: Path, zone_obj: dns.zone.Zone) -> None:
    """CoreDNS re-reads on mtime change, so a partial write caught mid-flight would leave the zone
    unparseable and the domain unresolvable until the next write."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w") as f:
            f.write(zone_obj.to_text(relativize=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ZoneFileError(f"could not write zone file {path}: {e}") from e
