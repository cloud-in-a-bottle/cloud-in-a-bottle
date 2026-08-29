"""The compute space's side of the DNS provider boundary.

Two things the provider is told rather than left to discover: its settings, and which of the
instance's domains are zones.  Both live here so ``coredns_provider`` imports nothing from the
application, and the application says nothing about how CoreDNS is run.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.dns.coredns_provider.interface import DnsSettings
from compute_space.core.dns.coredns_provider.interface import ManagedZone
from compute_space.core.domains import effective_domains
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

# The provider's SOA serial, in the router's settings store: one counter shared by every zone,
# since serials need not relate across zones.
_SERIAL_KEY = "dns_serial"


def dns_settings_for(
    config: Config, public_ip: str, *, container_gateway_ip: str | None = CONTAINER_GATEWAY_IP
) -> DnsSettings:
    """The provider's settings, drawn from the instance's config."""

    def get_serial(db: sqlite3.Connection) -> int | None:
        raw = get_setting(db, _SERIAL_KEY)
        return int(raw) if raw else None

    def set_serial(db: sqlite3.Connection, serial: int) -> None:
        set_setting(db, _SERIAL_KEY, str(serial))

    return DnsSettings(
        corefile_path=config.coredns_corefile_path,
        zonefile_path=config.coredns_zonefile_path,
        zones_dir=config.zones_dir,
        public_ip=public_ip,
        get_serial=get_serial,
        set_serial=set_serial,
        container_gateway_ip=container_gateway_ip,
    )


def zones_for_domains(db: sqlite3.Connection) -> tuple[ManagedZone, ...]:
    """Every non-mDNS domain, as a zone.

    mDNS ``.local`` domains are excluded: the wildcard mDNS responder serves them, and they never
    reach CoreDNS or ACME.
    """
    primary = primary_domain_or_none(db)
    primary_no_port = primary.name_no_port if primary else None
    return tuple(
        ManagedZone(zone=d.name_no_port, is_primary=d.name_no_port == primary_no_port)
        for d in effective_domains(db)
        if not d.mdns
    )
