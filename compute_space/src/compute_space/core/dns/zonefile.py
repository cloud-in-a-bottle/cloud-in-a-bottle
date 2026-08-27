"""Read/modify/write a CoreDNS zone file, treating the file itself as the source of truth.

CoreDNS serves the file, so the file *is* the live state — there is no second copy to drift from.
Everything here goes through dnspython's master-file parser rather than line surgery: the format
has enough sharp edges (``$ORIGIN``/``$TTL``, blank-owner continuation, parenthesized SOA, TXT
quoting and 255-byte chunking, relative vs absolute names) that a regex approach breaks as soon
as records get more varied than the three the template writes.

Writes are serialized per file and land atomically, because the router's cert path and any number
of apps can now be writing the same zone concurrently.  Every write bumps the SOA serial, which
is what makes CoreDNS reload.

Round-tripping does not preserve comments or layout — dnspython regenerates canonical output.
Records survive; formatting does not.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
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

# One lock per zone file path.  Guards the whole read-modify-write, so two callers cannot both
# read, both edit their own copy, and have the second write discard the first's record.
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = path.resolve() if path.exists() else path.absolute()
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


class ZoneFileError(RuntimeError):
    """The zone file could not be read or written."""


def _to_relative(zone_obj: dns.zone.Zone, name: dns.name.Name) -> str:
    """Zone-relative text for a record owner name, using ``@`` for the apex."""
    rel = name.relativize(zone_obj.origin) if zone_obj.origin else name
    text = rel.to_text()
    return APEX if text in ("@", "") else text


def _from_relative(name: str) -> str:
    """dnspython wants ``@`` spelled as the empty relative name."""
    return "@" if name == APEX else name


def read_records(path: Path, zone: str) -> list[DnsRecord]:
    """Every record in the zone file, as zone-relative wire records.

    SOA is included: a caller listing a zone should see what is actually served, and the service
    layer filters by grant rather than by our idea of what is interesting.
    """
    zone_obj = _load(path, zone)
    out: list[DnsRecord] = []
    for name, ttl, rdata in zone_obj.iterate_rdatas():
        out.append(
            DnsRecord(
                name=_to_relative(zone_obj, name),
                type=dns.rdatatype.to_text(rdata.rdtype),
                ttl=ttl,
                data=rdata.to_text(origin=zone_obj.origin, relativize=False),
            )
        )
    return out


def set_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """Replace each ``(name, type)`` RRset with the records given for it.  Names and types not
    mentioned are untouched."""

    def apply(zone_obj: dns.zone.Zone) -> None:
        for key in {(r.name, r.type) for r in records}:
            _delete_rrset(zone_obj, key[0], key[1])
        _add_all(zone_obj, records)

    return _edit(path, zone, apply, records)


def append_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """Add records, leaving anything already at the same name and type in place."""
    return _edit(path, zone, lambda zone_obj: _add_all(zone_obj, records), records)


def delete_records(path: Path, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """Remove records.  One with ``data is None`` clears its whole ``(name, type)`` RRset, which is
    how a caller cleans up a name without knowing what is currently there.

    Records that are not present are ignored rather than erroring, matching libdns and the
    connector app, so a cleanup path is safe to run twice.
    """

    def apply(zone_obj: dns.zone.Zone) -> None:
        for rec in records:
            if rec.is_rrset_selector:
                _delete_rrset(zone_obj, rec.name, rec.type)
            else:
                _delete_one(zone_obj, rec)

    return _edit(path, zone, apply, records)


def update_router_records(path: Path, zone: str, public_ip: str) -> None:
    """Point the apex, ``ns``, and wildcard A records at ``public_ip``, leaving everything else.

    Used when the domain set changes or the instance's public IP moves, in place of re-rendering
    the template — which would drop every record written through the DNS service.
    """
    set_records(
        path,
        zone,
        [DnsRecord(name=name, type="A", ttl=300, data=public_ip) for name in (APEX, "ns", "*")],
    )
    logger.info(f"Pointed router-owned records in {path.name} at {public_ip}")


# ─── internals ───


def _load(path: Path, zone: str) -> dns.zone.Zone:
    try:
        return dns.zone.from_file(str(path), origin=normalize_zone(zone) + ".", relativize=False)
    except FileNotFoundError as e:
        raise ZoneFileError(f"zone file {path} does not exist") from e
    except DNSException as e:
        raise ZoneFileError(f"could not parse zone file {path}: {e}") from e


def _edit(
    path: Path,
    zone: str,
    apply: Callable[[dns.zone.Zone], None],
    records: list[DnsRecord],
) -> list[DnsRecord]:
    """Load, mutate, bump the serial, and replace the file atomically, all under the zone's lock."""
    with _lock_for(path):
        zone_obj = _load(path, zone)
        apply(zone_obj)
        _bump_serial(zone_obj)
        _write_atomic(path, zone_obj)
    return records


def _add_all(zone_obj: dns.zone.Zone, records: list[DnsRecord]) -> None:
    for rec in records:
        if rec.data is None:
            raise InvalidRecord(f"record {rec.name} {rec.type} has no data")
        rdataset = zone_obj.find_rdataset(_from_relative(rec.name), dns.rdatatype.from_text(rec.type), create=True)
        try:
            rdata = dns.rdata.from_text(
                dns.rdataclass.IN,
                dns.rdatatype.from_text(rec.type),
                rec.data,
                origin=zone_obj.origin,
                relativize=False,
            )
        except DNSException as e:
            raise InvalidRecord(f"invalid {rec.type} data {rec.data!r}: {e}") from e
        rdataset.add(rdata, ttl=rec.ttl)


def _delete_rrset(zone_obj: dns.zone.Zone, name: str, rrtype: str) -> None:
    zone_obj.delete_rdataset(_from_relative(name), dns.rdatatype.from_text(rrtype))


def _delete_one(zone_obj: dns.zone.Zone, rec: DnsRecord) -> None:
    rdtype = dns.rdatatype.from_text(rec.type)
    rdataset = zone_obj.get_rdataset(_from_relative(rec.name), rdtype)
    if rdataset is None:
        return
    assert rec.data is not None
    try:
        rdata = dns.rdata.from_text(dns.rdataclass.IN, rdtype, rec.data, origin=zone_obj.origin, relativize=False)
    except DNSException as e:
        raise InvalidRecord(f"invalid {rec.type} data {rec.data!r}: {e}") from e
    remaining = [r for r in rdataset if r != rdata]
    if len(remaining) == len(rdataset):
        return
    if not remaining:
        zone_obj.delete_rdataset(_from_relative(rec.name), rdtype)
        return
    zone_obj.replace_rdataset(_from_relative(rec.name), dns.rdataset.from_rdata_list(rdataset.ttl, remaining))


def _bump_serial(zone_obj: dns.zone.Zone) -> None:
    """Increment the SOA serial so CoreDNS notices the change and reloads the zone."""
    rdataset = zone_obj.get_rdataset("@", dns.rdatatype.SOA)
    if rdataset is None or len(rdataset) == 0:
        raise ZoneFileError("zone has no SOA record, so its serial cannot be bumped")
    soa = rdataset[0]
    # Serials are unsigned 32-bit and wrap; RFC 1982 arithmetic makes the wrapped value compare as
    # newer, so plain modular increment is correct.
    bumped = soa.replace(serial=(soa.serial + 1) % 2**32)
    zone_obj.replace_rdataset("@", dns.rdataset.from_rdata_list(rdataset.ttl, [bumped]))


def _write_atomic(path: Path, zone_obj: dns.zone.Zone) -> None:
    """Write via a temp file in the same directory and rename over the target.

    CoreDNS re-reads the file whenever its mtime changes; a partial write it happened to catch
    mid-flight would leave the zone unparseable and the domain unresolvable until the next write.
    """
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


@contextmanager
def zone_lock(path: Path) -> Iterator[None]:
    """Hold a zone file's write lock across several operations (e.g. read-then-write)."""
    with _lock_for(path):
        yield
