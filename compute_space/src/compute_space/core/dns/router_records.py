"""The records that route the space itself.

The apex, the nameserver's glue, and the wildcard every app subdomain resolves through.  Written
like any other record rather than synthesized inside the provider, so there is one kind of record
and one way to publish one.

They carry no zone.  Every zone the space answers on is an alias for the same set of apps, so all
of them resolve to the same address.
"""

from __future__ import annotations

from compute_space.core.dns.coredns_provider.interface import ADDRESS_TTL_SECONDS
from compute_space.core.dns.coredns_provider.interface import APEX
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.logging import logger

# The apex itself, the glue for the ``ns`` name the SOA and NS records point at, and the wildcard
# that every ``<app>.<domain>`` resolves through.
ROUTER_ADDRESS_NAMES = (APEX, "ns", "*")


def publish_router_addresses(dns: InternalDnsProvider, public_ip: str) -> None:
    """Point the space's own names at ``public_ip``.

    Called on every boot: the provider holds its records in memory, so this is what puts them
    there.  ``set`` rather than an append, so a moved instance replaces the old address instead of
    answering with both.
    """
    for name in ROUTER_ADDRESS_NAMES:
        dns.set_records(name, RecordType.A, [public_ip], ttl=ADDRESS_TTL_SECONDS)
    logger.info(f"Published router address records at {public_ip}")
