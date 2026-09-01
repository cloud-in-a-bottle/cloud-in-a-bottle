"""Everything outside this package may use, and nothing else.

The compute space talks to the router's DNS through as narrow a surface as it can: construct an
:class:`InternalDnsProvider`, start it, write records to it, and tell it when the zone set changes.
Import from here rather than from a module inside the package.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import attr

from compute_space.core.dns.coredns_provider.coredns import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.coredns import CoreDnsProcess
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import ManagedZone
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.coredns import start_coredns
from compute_space.core.dns.coredns_provider.coredns import write_coredns_config
from compute_space.core.dns.coredns_provider.coredns import write_zone_file
from compute_space.core.dns.coredns_provider.records import APEX
from compute_space.core.dns.coredns_provider.records import DnsRecord
from compute_space.core.dns.coredns_provider.records import RecordType
from compute_space.core.dns.coredns_provider.records import normalize_zone
from compute_space.core.dns.coredns_provider.settings import DnsSettings
from compute_space.core.logging import logger

__all__ = [
    "ADDRESS_TTL_SECONDS",
    "APEX",
    "DnsRecord",
    "DnsSettings",
    "DnsZone",
    "InternalDnsProvider",
    "ManagedZone",
    "RecordType",
    "public_dns_zones",
]

_RRSet = tuple[str, RecordType]


@attr.s(auto_attribs=True)
class InternalDnsProvider:
    """The router's own DNS: the CoreDNS process, the zones it answers for, and their records.

    One object because they are one thing — a record has to be in the zone files and CoreDNS has to
    be serving those files, or it isn't published.  Held by whoever started it and passed where
    it's needed, rather than reached through a module global, so there is no way to be looking at a
    provider that isn't the running one.

    ``settings`` is the compute space's, passed in rather than imported: this package needs nothing
    from the application around it in order to run.

    Neither the zone set nor the records are persisted.  The zones are a view of the instance's
    domains, which the compute space already stores and re-derives every boot; the records are
    re-published on each boot by whoever owns them.  A copy on disk could only go stale.
    """

    settings: DnsSettings
    zones: tuple[ManagedZone, ...] = ()
    coredns_bin: str = "coredns"
    _records: dict[_RRSet, tuple[DnsRecord, ...]] = attr.ib(factory=dict, init=False)
    _coredns: CoreDnsProcess | None = attr.ib(default=None, init=False)
    _serial: int = attr.ib(default=0, init=False)

    @property
    def is_running(self) -> bool:
        return self._coredns is not None

    @property
    def rendered_zones(self) -> tuple[DnsZone, ...]:
        """The managed zones paired with the files they render to."""
        return public_dns_zones(self.settings, self.zones)

    @property
    def records(self) -> tuple[DnsRecord, ...]:
        """Every record currently served, in the order the zone files list them."""
        return tuple(sorted(_flatten(self._records), key=lambda r: (r.name, r.type, r.data)))

    async def start(self) -> None:
        """Render the config and zone files, then spawn CoreDNS.

        The zones answer with whatever records have been set, which at first boot is nothing --
        publishing the ones that route the space is the caller's job.
        """
        self._write_config()
        logger.info(f"Starting CoreDNS for {', '.join(z.domain for z in self.rendered_zones) or 'no zones'}")
        self._coredns = await start_coredns(self.settings, coredns_bin=self.coredns_bin)

    async def stop(self) -> None:
        """Shut CoreDNS down for good."""
        if self._coredns is not None:
            await self._coredns.stop()
            self._coredns = None

    # ─── records ───

    def set_records(
        self, name: str, rrtype: RecordType, values: Sequence[str], ttl: int = ADDRESS_TTL_SECONDS
    ) -> None:
        """Make ``values`` the only records at ``name``/``rrtype``, replacing whatever is there.

        ``name`` is relative to the zone, and lands in every zone the provider manages -- those
        zones are aliases for one space, so there is no such thing as a record in only some of
        them.

        ``set`` rather than an append, so re-running a publisher (every boot does) replaces what
        the last run wrote instead of accumulating alongside it.
        """
        rrset = tuple(DnsRecord(name=name, type=rrtype, ttl=ttl, data=v) for v in values)
        if self._records.get((name, rrtype)) == rrset:
            return
        self._records[(name, rrtype)] = rrset
        self._write_zone_files()
        logger.info(f"Set {len(rrset)} {rrtype} record(s) at {name!r} in every zone")

    def delete_records(self, name: str, rrtype: RecordType) -> None:
        """Remove every record at ``name``/``rrtype``, whatever it currently holds.

        Names the RRset rather than the values, which is all a cleanup path can do when it doesn't
        know what a previous run wrote.  Absent is not an error, so cleanup is safe to re-run.
        """
        if self._records.pop((name, rrtype), None) is None:
            return
        self._write_zone_files()
        logger.info(f"Cleared {rrtype} records at {name!r} in every zone")

    # ─── zones ───

    async def add_zone(self, zone: ManagedZone) -> bool:
        """Also be authoritative for ``zone``.

        Nothing to backfill -- records belong to no zone, so the new one renders from the same set
        as every other.  Returns whether CoreDNS restarted.
        """
        # Normalize on the way in, not just for the comparison, so the stored set and a later
        # lookup agree on how a zone is spelled.
        added = attr.evolve(zone, zone=normalize_zone(zone.zone))
        if any(z.zone == added.zone for z in self.zones):
            return False
        return await self._reconcile((*self.zones, added))

    async def remove_zone(self, zone: str) -> bool:
        """Stop being authoritative for ``zone``, and discard its rendered files.

        The records survive: they belong to no zone, so a zone leaving takes nothing with it.
        Returns whether CoreDNS restarted.
        """
        name = normalize_zone(zone)
        if not any(z.zone == name for z in self.zones):
            return False
        return await self._reconcile(tuple(z for z in self.zones if z.zone != name))

    async def set_zones(self, zones: Sequence[ManagedZone]) -> bool:
        """Make ``zones`` the managed set, whatever it was before.

        For a caller that holds the whole list and would rather not diff it -- pushing it is
        idempotent.  Returns whether CoreDNS restarted.
        """
        return await self._reconcile(tuple(zones))

    async def _reconcile(self, zones: tuple[ManagedZone, ...]) -> bool:
        """Move the managed set to ``zones``, then re-render and restart if it actually changed.

        A restart, not just a re-render, because a zone appearing or disappearing means a
        different set of Corefile server blocks, which a running process won't pick up.
        """
        before = {z.domain: z for z in self.rendered_zones}
        self.zones = zones
        after = {z.domain for z in self.rendered_zones}
        if before.keys() == after:
            return False

        for name in before.keys() - after:
            _discard_zone_files(before[name])
        logger.info(f"DNS zones are now {sorted(after) or 'none'}")

        # Re-render either way: the zone files should reflect the current set whether or not
        # anything is serving them right now.
        self._write_config()
        if self._coredns is None:
            return False
        await self._coredns.restart()
        return True

    # ─── rendering ───

    def _write_config(self) -> None:
        """The Corefile and every zone file.  Needed whenever the zone set changes, since the
        Corefile has a server block per zone."""
        write_coredns_config(self.rendered_zones, self.settings, self.records, self._next_serial())

    def _write_zone_files(self) -> None:
        """Just the zone data.  A record change can't affect the Corefile, and CoreDNS picks a
        rewritten zone file up on its own once the serial moves."""
        serial = self._next_serial()
        for zone in self.rendered_zones:
            write_zone_file(zone, self.records, serial)

    def _next_serial(self) -> int:
        """A strictly increasing SOA serial, which is what makes CoreDNS reload the zone.

        Wall-clock alone is not enough: two writes in the same second would render the same serial
        and the second change would never be picked up.  In memory only -- a serial matters to a
        *running* CoreDNS, and a restart re-reads every zone file wholesale.
        """
        # Serials are unsigned 32-bit and wrap; RFC 1982 arithmetic makes the wrapped value newer.
        self._serial = max(self._serial + 1, int(time.time())) % 2**32
        return self._serial


def _flatten(records: dict[_RRSet, tuple[DnsRecord, ...]]) -> list[DnsRecord]:
    return [r for rrset in records.values() for r in rrset]


def _discard_zone_files(zone: DnsZone) -> None:
    """Drop a removed zone's rendered files.

    Only litter once the Corefile stops referencing them, but litter that a later re-add would
    serve stale if it raced the re-render.
    """
    for path in (zone.zonefile_path, zone.container_zonefile_path):
        path.unlink(missing_ok=True)
