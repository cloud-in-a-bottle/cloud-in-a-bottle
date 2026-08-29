"""Which of the instance's domains the DNS provider should be authoritative for.

The adapter between the compute space's domain set and the provider's zone set, so that neither
has to know the other's model: the provider is told its zones, and never reads ``domains``.
"""

from __future__ import annotations

import sqlite3

from compute_space.core.dns.coredns_provider.interface import ManagedZone
from compute_space.core.domains import effective_domains
from compute_space.core.domains import primary_domain_or_none


def zones_for_domains(db: sqlite3.Connection) -> list[ManagedZone]:
    """Every non-mDNS domain, as a zone.

    mDNS ``.local`` domains are excluded: the wildcard mDNS responder serves them, and they never
    reach CoreDNS or ACME.
    """
    primary = primary_domain_or_none(db)
    primary_no_port = primary.name_no_port if primary else None
    return [
        ManagedZone(zone=d.name_no_port, is_primary=d.name_no_port == primary_no_port)
        for d in effective_domains(db)
        if not d.mdns
    ]
