from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path

import attr

from compute_space.core.dns.coredns_provider.coredns import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.coredns import CoreDnsProcess
from compute_space.core.dns.coredns_provider.coredns import discard_zone_files
from compute_space.core.dns.coredns_provider.coredns import write_coredns_config
from compute_space.core.dns.coredns_provider.records import APEX
from compute_space.core.dns.coredns_provider.records import DnsRecord
from compute_space.core.dns.coredns_provider.records import RecordType
from compute_space.core.dns.coredns_provider.records import normalize_zone
from compute_space.core.logging import logger

# Submodule types/consts are re-exported here so users can only import from this file.
__all__ = [
    "ADDRESS_TTL_SECONDS",
    "APEX",
    "DnsNotEnabled",
    "DnsRecord",
    "InternalDnsProvider",
    "RecordType",
]

# Domains that resolve without this instance answering for them: mDNS handles ``.local``, and
# ``lvh.me`` (and friends) are public wildcards pointing at loopback.  An instance configured with
# only these has nothing to be authoritative for, so it never binds :53.
_LOCAL_ONLY_SUFFIXES = (".local", "lvh.me")


def _is_local_only(domain: str) -> bool:
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in _LOCAL_ONLY_SUFFIXES)


class DnsNotEnabled(Exception):
    pass


@attr.s(auto_attribs=True)
class InternalDnsProvider:
    """Interface to the internal DNS server provided by CoreDNS.

    Recreated from scratch on every boot, so it holds no persistent state.
    A set of zones are registered, along with a set of records.
    Records are relative to a zone, and are published identically on all zones.

    The actual CoreDNS process is started and stopped automatically as needed as zones are added and removed.

    Should never be passed into a new thread, but is async-safe.
    """

    # Where the generated CoreDNS config goes.
    corefile_path: Path
    zones_dir: Path

    # what internal interface address to serve coredns on for the main routing records
    # None indicates that we should never bind / serve these records, and will raise DnsNotEnabled on attempts.
    bind_ip: str | None
    # what internal interface address to serve coredns on for the loopback records,
    # or none if these records should not be served (dev/CI)
    container_gateway_ip: str | None = None

    coredns_bin: str = "coredns"

    _zones: tuple[str, ...] = ()
    _records: dict[tuple[str, RecordType], tuple[DnsRecord, ...]] = attr.ib(factory=dict, init=False)
    _coredns: CoreDnsProcess | None = attr.ib(default=None, init=False)
    _serial: int = attr.ib(default=0, init=False)
    # Serializes zone changes: two concurrent /api/domains requests are two tasks on one loop, and
    # two overlapping restarts orphan a CoreDNS still holding :53.  Record writes need no lock --
    # they never await, so they cannot interleave.
    _zone_lock: asyncio.Lock = attr.ib(factory=asyncio.Lock, init=False, eq=False, repr=False)

    async def cleanup(self) -> None:
        """Shut CoreDNS down for good.  A no-op if it isn't running, so a shutdown path needs no check of its own."""
        if self._coredns is not None:
            await self._coredns.stop()
            self._coredns = None

    @property
    def serves_public_zones(self) -> bool:
        return self.bind_ip is not None

    @property
    def zones(self) -> tuple[str, ...]:
        return self._zones

    async def add_zone(self, zone: str) -> None:
        if _is_local_only(zone):
            # these already resolve without coredns
            return
        if not self.serves_public_zones:
            raise DnsNotEnabled(f"No address to serve {zone!r} on; this instance is not bound for public DNS")
        normalized_zone = normalize_zone(zone)
        if normalized_zone in self._zones:
            # Already authoritative for it.  Appending anyway would put two server blocks for the
            # same zone in the Corefile, which CoreDNS refuses to load.
            return
        await self._apply_zones((*self._zones, normalized_zone))

    async def remove_zone(self, zone: str) -> None:
        name = normalize_zone(zone)
        await self._apply_zones(tuple(z for z in self._zones if z != name))

    @property
    def records(self) -> tuple[DnsRecord, ...]:
        flat = [r for rrset in self._records.values() for r in rrset]
        return tuple(sorted(flat, key=lambda r: (r.name, r.type, r.data)))

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

    async def _apply_zones(self, zones: tuple[str, ...]) -> None:
        """Move the managed set to ``zones``, then re-render and restart CoreDNS.

        A restart, not just a re-render, because a zone appearing or disappearing means a
        different set of Corefile server blocks, which a running process won't pick up.
        """
        async with self._zone_lock:
            before = set(self._zones)
            self._zones = zones

            for name in before - set(zones):
                discard_zone_files(self.zones_dir, name)
            logger.info(f"DNS zones are now {sorted(zones) or 'none'}")

            # Re-render whether or not anything is serving the files right now: a later start reads
            # them as they are.
            self._write_config()
            await self._match_process_to_zones()

    async def _match_process_to_zones(self) -> None:
        """Run CoreDNS exactly when there is a zone to answer for.

        Caller holds the zone lock.  With nothing to serve the Corefile has no server blocks, which
        CoreDNS refuses to start against, so not running is the only honest state for it.
        """
        if not self._zones:
            if self._coredns is not None:
                logger.info("No zones left to serve; stopping CoreDNS")
                await self.cleanup()
        elif self._coredns is None:
            logger.info(f"Serving DNS for {', '.join(self._zones)}")
            self._coredns = await CoreDnsProcess.start(self.corefile_path, coredns_bin=self.coredns_bin)
        else:
            await self._coredns.restart()

    def _write_config(self) -> None:
        """The Corefile and every zone file, rendered from scratch.

        Every change goes through here, records included: the whole config is cheap to rebuild, and
        one path means there is no second notion of what a zone file should contain.  A record-only
        change re-renders a byte-identical Corefile, which costs nothing -- CoreDNS watches zone
        files (``reload 2s``), not the Corefile, and picks the data up once the serial moves.
        """
        write_coredns_config(
            self._zones,
            self.records,
            self._next_serial(),
            corefile_path=self.corefile_path,
            zones_dir=self.zones_dir,
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
