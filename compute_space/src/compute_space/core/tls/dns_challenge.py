from __future__ import annotations

from collections.abc import Sequence

from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.dns.propagation import wait_for_records

CHALLENGE_TTL_SECONDS = 60
_LABEL = "_acme-challenge"

# How long to wait for the CA's view of the record to catch up before giving up and letting the
# ACME retry loop deal with it.
_PROPAGATION_TIMEOUT_SECONDS = 90


def challenge_fqdn(domain: str) -> str:
    """Where a DNS-01 token for ``domain`` resolves.  A wildcard order validates against the base
    domain, so ``*.example.com`` and ``example.com`` share one name."""
    return f"{_LABEL}.{domain.removeprefix('*.')}"


def publish(dns_provider: InternalDnsProvider, values: Sequence[str]) -> None:
    dns_provider.set_records(_LABEL, RecordType.TXT, values, ttl=CHALLENGE_TTL_SECONDS)


def clear(dns_provider: InternalDnsProvider) -> None:
    dns_provider.delete_records(_LABEL, RecordType.TXT)


async def wait_until_visible(domain: str, values: Sequence[str]) -> None:
    await wait_for_records(challenge_fqdn(domain), RecordType.TXT, values, timeout=_PROPAGATION_TIMEOUT_SECONDS)
