"""DNS-01 challenge records: what they are called, how long they live, and when they're visible.

Both cert paths — BYO-ACME and the openhost-cert-api broker — publish the same records in the same
place, so the naming and TTL live here rather than in either of them, and out of the DNS client,
which has no reason to know what ``_acme-challenge`` means.
"""

from __future__ import annotations

from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.client import wait_for_records
from compute_space.core.dns.service_api import RecordType

# Short and explicit, not the zone default: a renewal must not have the CA — or our own propagation
# check — served the previous run's token out of a resolver cache.
CHALLENGE_TTL_SECONDS = 60

_LABEL = "_acme-challenge"


def challenge_fqdn(domain: str) -> str:
    """Where a DNS-01 token for ``domain`` resolves.  A wildcard order validates against the base
    domain, so ``*.example.com`` and ``example.com`` share one name.

    Only for the propagation check, which queries a real resolver.  Writes use ``_LABEL`` directly:
    the provider names records relative to the zone, and publishes into every zone it manages.
    """
    return f"{_LABEL}.{domain.removeprefix('*.')}"


async def publish(dns: DnsClient, values: list[str]) -> None:
    """Publish every token for the order at once.

    A wildcard order has two authorizations that must both be answered simultaneously, and setting
    the RRset rather than appending means a run that died before cleaning up doesn't leave stale
    tokens for this one.
    """
    await dns.set_records(_LABEL, RecordType.TXT, values, ttl=CHALLENGE_TTL_SECONDS)


async def clear(dns: DnsClient) -> None:
    await dns.delete_records(_LABEL, RecordType.TXT)


async def wait_until_visible(dns: DnsClient, domain: str, values: list[str]) -> None:
    """Block until an external resolver can see the tokens, or the provider's timeout elapses.

    Without this the CA's resolvers may get NXDOMAIN — the zone file reload hasn't happened, the
    registrar hasn't published, or the parent zone's NS delegation hasn't propagated.
    """
    await wait_for_records(challenge_fqdn(domain), RecordType.TXT, values, timeout=90)
