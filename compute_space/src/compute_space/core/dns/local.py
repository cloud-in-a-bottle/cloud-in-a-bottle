"""The DnsBackend backed by this instance's own CoreDNS zone files.

Used when the space is its own DNS provider: the NS delegation points at this box, CoreDNS is
authoritative, and records live in the zone files under the data directory.  This is also what
backs the router's built-in ``dns`` service provider, so an app writing a record through the
service and the router writing a challenge record end up in exactly the same place.

After a write, CoreDNS picks the file up on its own within its ``reload 2s`` window — there is no
reload call to make, only a serial bump, which ``zonefile`` does on every write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import attr

from compute_space.config import Config
from compute_space.core.dns import zonefile
from compute_space.core.dns.backend import DnsBackendError
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.dns.records import normalize_zone
from compute_space.core.logging import logger

# CoreDNS's `reload 2s` plus slack.  Short, because the file is local — the real wait is the
# external propagation check, which the delegation makes unavoidable.
_LOCAL_PROPAGATION_TIMEOUT_SECONDS = 120.0


@attr.s(auto_attribs=True, frozen=True)
class LocalZoneFileBackend:
    """Reads and writes the CoreDNS zone files for the instance's public domains."""

    # zone name -> zone file path.  Snapshotted at construction: the caller builds one of these per
    # operation from the live domain set, so a stale map can't outlive a domain change.
    zone_paths: dict[str, Path]

    @classmethod
    def create(cls, config: Config, db: sqlite3.Connection) -> LocalZoneFileBackend:
        return cls(zone_paths={z.domain: z.zonefile_path for z in public_dns_zones(config, db)})

    def zones(self) -> list[str]:
        return sorted(self.zone_paths)

    @property
    def propagation_timeout_seconds(self) -> float:
        return _LOCAL_PROPAGATION_TIMEOUT_SECONDS

    def _path(self, zone: str) -> Path:
        z = normalize_zone(zone)
        path = self.zone_paths.get(z)
        if path is None:
            raise UnknownZone(f"{zone!r} is not a zone this instance serves (zones: {', '.join(self.zones())})")
        if not path.exists():
            # The zone is configured but CoreDNS never seeded it — serving it would be a lie.
            raise DnsBackendError(f"zone file for {z} has not been created yet")
        return path

    def get_records(self, zone: str, name: str | None = None, rrtype: str | None = None) -> list[DnsRecord]:
        records = zonefile.read_records(self._path(zone), zone)
        if name is not None:
            wanted = name.strip().lower()
            records = [r for r in records if r.name == wanted]
        if rrtype is not None:
            wanted_type = rrtype.strip().upper()
            records = [r for r in records if r.type == wanted_type]
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

    def _prepare(
        self, zone: str, records: list[DnsRecord], *, allow_rrset_selector: bool = False
    ) -> tuple[Path, list[DnsRecord]]:
        """Resolve the zone file and canonicalize every record before touching the file, so an
        invalid record in the batch fails the whole write rather than half-applying it."""
        path = self._path(zone)
        normalized = [normalize_record(r, zone, allow_rrset_selector=allow_rrset_selector) for r in records]
        return path, normalized
