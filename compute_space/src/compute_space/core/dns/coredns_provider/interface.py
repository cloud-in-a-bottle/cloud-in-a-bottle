"""Everything outside this package may use, and nothing else.

The provider is one of two interchangeable implementations of the ``dns`` service, so the compute
space talks to it through as narrow a surface as it can: construct an
:class:`InternalDnsProvider`, start it, hand its ``app`` to the builtin-service registry, and tell
it when the zone set changes.  Import from here rather than from a module inside the package.

Records are deliberately absent.  They arrive over the service API, from apps and from the
router's own ACME path alike, so there is no second way in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import attr
from litestar import Litestar

from compute_space.core.dns.coredns_provider.coredns import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.coredns import CoreDnsProcess
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.coredns import ManagedZone
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.coredns import start_coredns
from compute_space.core.dns.coredns_provider.coredns import write_coredns_config
from compute_space.core.dns.coredns_provider.routes import DbProvider
from compute_space.core.dns.coredns_provider.routes import build_coredns_service_app
from compute_space.core.dns.coredns_provider.settings import DnsSettings
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.logging import logger

__all__ = [
    "ADDRESS_TTL_SECONDS",
    "DnsSettings",
    "DnsZone",
    "InternalDnsProvider",
    "ManagedZone",
    "build_coredns_service_app",
    "public_dns_zones",
    "write_coredns_config",
]


class DnsProviderNotStarted(RuntimeError):
    """Asked for something only a started provider has."""


@attr.s(auto_attribs=True)
class InternalDnsProvider:
    """The router's own ``dns`` service: the CoreDNS process, and the API in front of it.

    One object because they are one thing — the service app accepts records, CoreDNS serves them,
    and a zone has to appear in both or neither.  Held by whoever started it and injected where
    it's needed, rather than reached through a module global, so there is no way to be looking at
    a provider that isn't the running one.

    ``settings`` and ``db_provider`` are the compute space's, passed in rather than imported: this
    package needs nothing from the application around it in order to run.

    The zone set is given at construction and kept in memory.  It is not state to persist -- it is
    a view of the instance's domains, which the compute space already stores and re-derives every
    boot, so a copy in the DB could only go stale.
    """

    settings: DnsSettings
    db_provider: DbProvider
    zones: tuple[ManagedZone, ...] = ()
    coredns_bin: str = "coredns"
    _coredns: CoreDnsProcess | None = attr.ib(default=None, init=False)
    _app: Litestar | None = attr.ib(default=None, init=False)

    @property
    def app(self) -> Litestar:
        """The ASGI app implementing the ``dns`` service, for the builtin-service registry.

        Only once started.  The app itself needs no running CoreDNS — see
        ``build_coredns_service_app`` for the service API alone — but registering one that has no
        process behind it would have the router accept records it cannot serve.
        """
        if self._app is None:
            raise DnsProviderNotStarted("the DNS provider must be started before its app is served")
        return self._app

    @property
    def is_running(self) -> bool:
        return self._coredns is not None

    @property
    def rendered_zones(self) -> tuple[DnsZone, ...]:
        """The managed zones paired with the files they render to."""
        return public_dns_zones(self.settings, self.zones)

    async def start(self, db: sqlite3.Connection) -> None:
        """Render the zone files, spawn CoreDNS, and build the service app.

        The zones answer with whatever records are stored, which at first boot is nothing --
        publishing the ones that route the space is the caller's job, once this app is registered
        and reachable.
        """
        self._coredns = await start_coredns(self.rendered_zones, self.settings, coredns_bin=self.coredns_bin, db=db)
        # A live view, not a snapshot: a write arriving after add_zone must render into the zone
        # that was just added.
        self._app = build_coredns_service_app(self.db_provider, self.settings, lambda: self.rendered_zones)

    async def stop(self) -> None:
        """Shut CoreDNS down for good.  The app stays: it holds no process."""
        if self._coredns is not None:
            await self._coredns.stop()
            self._coredns = None

    async def add_zone(self, db: sqlite3.Connection, zone: ManagedZone) -> bool:
        """Also be authoritative for ``zone``.

        Nothing to backfill — records belong to no zone, so the new one renders from the same rows
        as every other.  Returns whether CoreDNS restarted.
        """
        if any(z.zone == normalize_zone(zone.zone) for z in self.zones):
            return False
        return await self._reconcile(db, (*self.zones, zone))

    async def remove_zone(self, db: sqlite3.Connection, zone: str) -> bool:
        """Stop being authoritative for ``zone``, and discard its rendered files.

        The records survive: they belong to no zone, so a zone leaving takes nothing with it.
        Returns whether CoreDNS restarted.
        """
        name = normalize_zone(zone)
        if not any(z.zone == name for z in self.zones):
            return False
        return await self._reconcile(db, tuple(z for z in self.zones if z.zone != name))

    async def set_zones(self, db: sqlite3.Connection, zones: Sequence[ManagedZone]) -> bool:
        """Make ``zones`` the managed set, whatever it was before.

        For a caller that holds the whole list and would rather not diff it — pushing it is
        idempotent.  Returns whether CoreDNS restarted.
        """
        return await self._reconcile(db, tuple(zones))

    async def _reconcile(self, db: sqlite3.Connection, zones: tuple[ManagedZone, ...]) -> bool:
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
        write_coredns_config(self.rendered_zones, self.settings, db=db)
        if self._coredns is None:
            return False
        await self._coredns.restart()
        return True


def _discard_zone_files(zone: DnsZone) -> None:
    """Drop a removed zone's rendered files.

    Only litter once the Corefile stops referencing them, but litter that a later re-add would
    serve stale if it raced the re-render.
    """
    for path in (zone.zonefile_path, zone.container_zonefile_path):
        path.unlink(missing_ok=True)
