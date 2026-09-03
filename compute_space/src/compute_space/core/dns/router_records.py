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
    """Point the space's own names at ``public_ip``."""
    for name in ROUTER_ADDRESS_NAMES:
        dns.set_records(name, RecordType.A, [public_ip], ttl=ADDRESS_TTL_SECONDS)
    logger.info(f"Published router address records at {public_ip}")
