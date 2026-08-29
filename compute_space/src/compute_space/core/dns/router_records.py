"""The records that route the space itself.

The apex, the nameserver's glue, and the wildcard every app subdomain resolves through.  Written
over the service API like anything else, rather than synthesized inside a provider: they are
records, and a connector app serving this space's DNS has to end up with the same three.

They carry no zone.  Every zone the space answers on is an alias for the same set of apps, so all
of them resolve to the same address.
"""

from __future__ import annotations

import sqlite3

from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.coredns_provider.interface import ADDRESS_TTL_SECONDS
from compute_space.core.dns.service_api import APEX
from compute_space.core.dns.service_api import RecordType
from compute_space.core.logging import logger

# The apex itself, the glue for the ``ns`` name the SOA and NS records point at, and the wildcard
# that every ``<app>.<domain>`` resolves through.
ROUTER_ADDRESS_NAMES = (APEX, "ns", "*")


async def publish_router_addresses(db: sqlite3.Connection, public_ip: str) -> None:
    """Point the space's own names at ``public_ip``.

    ``set`` rather than ``append``, so a moved instance replaces the old address instead of
    answering with both.
    """
    dns = DnsClient(db)
    for name in ROUTER_ADDRESS_NAMES:
        await dns.set_records(name, RecordType.A, [public_ip], ttl=ADDRESS_TTL_SECONDS)
    logger.info(f"Published router address records at {public_ip}")
