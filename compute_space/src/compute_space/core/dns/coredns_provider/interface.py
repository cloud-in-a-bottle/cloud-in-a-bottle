"""Everything outside this package may use, and nothing else.

The compute space talks to the router's DNS through as narrow a surface as it can: construct an
:class:`InternalDnsProvider`, start it, write records to it, and tell it when the zone set changes.
Import from here rather than from a module inside the package.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import attr

from compute_space.core.dns.coredns_provider.coredns import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.coredns import CoreDnsProcess
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.coredns import write_coredns_config
from compute_space.core.dns.coredns_provider.records import APEX
from compute_space.core.dns.coredns_provider.records import DnsRecord
from compute_space.core.dns.coredns_provider.records import RecordType
from compute_space.core.dns.coredns_provider.records import normalize_zone
from compute_space.core.logging import logger

__all__ = [
    "ADDRESS_TTL_SECONDS",
    "APEX",
    "DnsZoneError",
    "DnsRecord",
    "DnsZone",
    "InternalDnsProvider",
    "RecordType",
    "public_dns_zones",
]


class DnsZoneError(Exception):
    """A zone change that contradicts the set already served."""


@attr.s(auto_attribs=True)
class InternalDnsProvider:
    """Interface to the internal DNS server provided by CoreDNS.

    Recreated from scratch on every boot, so it holds no persistent state.
    A set of zones are registered, along with a set of records.
    Records are relative to a zone, and are published identically on all zones.
    """

    # Where the generated CoreDNS config goes.
    corefile_path: Path
    zones_dir: Path

    # what internal interface address to serve coredns on for the main routing records
    bind_ip: str
    # what internal interface address to serve coredns on for the loopback records,
    # or none if these records should not be served (dev/CI)
    container_gateway_ip: str | None = None

    coredns_bin: str = "coredns"

    _zones: tuple[str, ...] = ()
    _records: dict[tuple[str, RecordType], tuple[DnsRecord, ...]] = attr.ib(factory=dict, init=False)
    _coredns: CoreDnsProcess | None = attr.ib(default=None, init=False)
    _serial: int = attr.ib(default=0, init=False)

    @property
    def zones(self) -> tuple[str, ...]:
        return self._zones

    @property
    def is_running(self) -> bool:
        return self._coredns is not None

    @property
    def rendered_zones(self) -> tuple[DnsZone, ...]:
        return public_dns_zones(self.zones_dir, self.zones)

    @property
    def records(self) -> tuple[DnsRecord, ...]:
        flat = [r for rrset in self._records.values() for r in rrset]
        return tuple(sorted(flat, key=lambda r: (r.name, r.type, r.data)))

    async def start(self) -> None:
        self._write_config()
        logger.info(f"Starting CoreDNS for {', '.join(z.domain for z in self.rendered_zones) or 'no zones'}")
        self._coredns = await CoreDnsProcess.start(self.corefile_path, coredns_bin=self.coredns_bin)

    async def stop(self) -> None:
        if self._coredns is not None:
            await self._coredns.stop()
            self._coredns = None

    # ─── records ───

    def set_records(
        self, name: str, record_type: RecordType, values: Sequence[str], ttl: int = ADDRESS_TTL_SECONDS
    ) -> None:
        """Make ``values`` the only records at ``name``/``record_type``, replacing whatever is there.

        ``name`` is relative to the zone, and lands in every zone the provider manages -- those
        zones are aliases for one space, so there is no such thing as a record in only some of
        them.

        ``set`` rather than an append, so re-running a publisher (every boot does) replaces what
        the last run wrote instead of accumulating alongside it.
        """
        rrset = tuple(DnsRecord(name=name, type=record_type, ttl=ttl, data=v) for v in values)
        if self._records.get((name, record_type)) == rrset:
            return
        self._records[(name, record_type)] = rrset
        self._write_config()
        logger.info(f"Set {len(rrset)} {record_type} record(s) at {name!r} in every zone")

    def delete_records(self, name: str, record_type: RecordType) -> None:
        """Remove every record at ``name``/``record_type``, whatever it currently holds (if anything)."""
        if self._records.pop((name, record_type), None) is None:
            return
        self._write_config()
        logger.info(f"Cleared {record_type} records at {name!r} in every zone")

    # ─── zones ───

    async def add_zone(self, zone: str) -> None:
        # Normalize on the way in, not just for the comparison, so the stored set and a later
        # lookup agree on how a zone is spelled.
        added = normalize_zone(zone)
        if added in self._zones:
            raise DnsZoneError(f"Already authoritative for {added!r}")
        await self._reconcile((*self._zones, added))

    async def remove_zone(self, zone: str) -> None:
        name = normalize_zone(zone)
        if name not in self._zones:
            raise DnsZoneError(f"Not authoritative for {name!r}")
        await self._reconcile(tuple(z for z in self._zones if z != name))

    async def _reconcile(self, zones: tuple[str, ...]) -> None:
        """Move the managed set to ``zones``, then re-render and restart CoreDNS.

        A restart, not just a re-render, because a zone appearing or disappearing means a
        different set of Corefile server blocks, which a running process won't pick up.
        """
        before = {z.domain: z for z in self.rendered_zones}
        self._zones = zones
        after = {z.domain for z in self.rendered_zones}

        for name in before.keys() - after:
            _discard_zone_files(before[name])
        logger.info(f"DNS zones are now {sorted(after) or 'none'}")

        # Re-render whether or not anything is serving the files right now: a later start reads
        # them as they are.
        self._write_config()
        if self._coredns is not None:
            await self._coredns.restart()

    # ─── rendering ───

    def _write_config(self) -> None:
        """The Corefile and every zone file, rendered from scratch.

        Every change goes through here, records included: the whole config is cheap to rebuild, and
        one path means there is no second notion of what a zone file should contain.  A record-only
        change re-renders a byte-identical Corefile, which costs nothing -- CoreDNS watches zone
        files (``reload 2s``), not the Corefile, and picks the data up once the serial moves.
        """
        write_coredns_config(
            self.rendered_zones,
            self.records,
            self._next_serial(),
            corefile_path=self.corefile_path,
            bind_ip=self.bind_ip,
            container_gateway_ip=self.container_gateway_ip,
        )

    def _next_serial(self) -> int:
        """A strictly increasing SOA serial, which is what makes CoreDNS reload the zone.

        Wall-clock alone is not enough: two writes in the same second would render the same serial
        and the second change would never be picked up.  In memory only -- a serial matters to a
        *running* CoreDNS, and a restart re-reads every zone file wholesale.
        """
        # Serials are unsigned 32-bit and wrap; RFC 1982 arithmetic makes the wrapped value newer.
        self._serial = max(self._serial + 1, int(time.time())) % 2**32
        return self._serial


def _discard_zone_files(zone: DnsZone) -> None:
    """Drop a removed zone's rendered files.

    Only litter once the Corefile stops referencing them, but litter that a later re-add would
    serve stale if it raced the re-render.
    """
    for path in (zone.zonefile_path, zone.container_zonefile_path):
        path.unlink(missing_ok=True)
