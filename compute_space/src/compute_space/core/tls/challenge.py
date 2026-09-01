"""DNS-01 challenge records: what they are called, how long they live, and when they're visible.

Both cert paths — BYO-ACME and the openhost-cert-api broker — publish the same records in the same
place, so the naming and TTL live here rather than in either of them, and out of the DNS provider,
which has no reason to know what ``_acme-challenge`` means.
"""

from __future__ import annotations

from collections.abc import Sequence

from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.dns.propagation import wait_for_records

# Short and explicit, not the zone default: a renewal must not have the CA — or our own propagation
# check — served the previous run's token out of a resolver cache.
CHALLENGE_TTL_SECONDS = 60

_LABEL = "_acme-challenge"

# How long to wait for the CA's view of the record to catch up before giving up and letting the
# ACME retry loop deal with it.
_PROPAGATION_TIMEOUT_SECONDS = 90


def challenge_fqdn(domain: str) -> str:
    """Where a DNS-01 token for ``domain`` resolves.  A wildcard order validates against the base
    domain, so ``*.example.com`` and ``example.com`` share one name.

    Only for the propagation check, which queries a real resolver.  Writes use ``_LABEL`` directly:
    the provider names records relative to the zone, and publishes into every zone it manages.
    """
    return f"{_LABEL}.{domain.removeprefix('*.')}"


def publish(dns: InternalDnsProvider, values: Sequence[str]) -> None:
    """Publish every token for the order at once.

    A wildcard order has two authorizations that must both be answered simultaneously, and setting
    the RRset rather than appending means a run that died before cleaning up doesn't leave stale
    tokens for this one.
    """
    dns.set_records(_LABEL, RecordType.TXT, values, ttl=CHALLENGE_TTL_SECONDS)


def clear(dns: InternalDnsProvider) -> None:
    dns.delete_records(_LABEL, RecordType.TXT)


async def wait_until_visible(domain: str, values: Sequence[str]) -> None:
    """Block until an external resolver can see the tokens, or the timeout elapses.

    Without this the CA's resolvers may get NXDOMAIN — the zone file reload hasn't happened, or the
    parent zone's NS delegation hasn't propagated.
    """
    await wait_for_records(challenge_fqdn(domain), RecordType.TXT, values, timeout=_PROPAGATION_TIMEOUT_SECONDS)
