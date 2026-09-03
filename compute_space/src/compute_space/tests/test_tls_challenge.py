"""DNS-01 challenge records: naming, TTL, and cleanup semantics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import attr
import pytest

from compute_space.core.dns.coredns_provider.interface import RecordType
from compute_space.core.tls import dns_challenge


@attr.s(auto_attribs=True)
class _RecordingDns:
    """Stands in for InternalDnsProvider, capturing the record calls the challenge helpers make."""

    calls: list[tuple[str, ...]] = attr.ib(factory=list)

    def set_records(self, name: str, record_type: RecordType, values: Sequence[str], ttl: int = 300) -> None:
        self.calls.append(("set", name, record_type, ",".join(values), str(ttl)))

    def delete_records(self, name: str, record_type: RecordType) -> None:
        self.calls.append(("delete", name, record_type))


def test_a_wildcard_order_validates_against_the_base_domain() -> None:
    # *.example.com and example.com share one challenge name, which is why both authorizations'
    # tokens have to be live at the same time.  Only the propagation check uses the FQDN.
    assert dns_challenge.challenge_fqdn("*.example.com") == "_acme-challenge.example.com"
    assert dns_challenge.challenge_fqdn("example.com") == "_acme-challenge.example.com"


def test_only_a_leading_wildcard_label_is_stripped() -> None:
    # `lstrip("*.")` would take a character set and mangle these.
    assert dns_challenge.challenge_fqdn("host.example.com") == "_acme-challenge.host.example.com"
    assert dns_challenge.challenge_fqdn("*.a.example.com") == "_acme-challenge.a.example.com"


def test_publishing_replaces_the_rrset_with_a_short_ttl() -> None:
    # Replace, not append: a run that died before cleaning up must not leave stale tokens. Short
    # TTL so the previous run's token isn't served from a resolver cache during a renewal.
    dns_provider: Any = _RecordingDns()
    dns_challenge.publish(dns_provider, ["base", "wildcard"])
    # Named relative to the zone, not by FQDN: the provider publishes into every zone it manages.
    assert dns_provider.calls == [("set", "_acme-challenge", "TXT", "base,wildcard", "60")]


def test_clearing_removes_the_whole_rrset() -> None:
    dns_provider: Any = _RecordingDns()
    dns_challenge.clear(dns_provider)
    assert dns_provider.calls == [("delete", "_acme-challenge", "TXT")]


@pytest.mark.asyncio
async def test_the_wait_has_a_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bounded, so a delegation that never propagates fails the order instead of hanging the renewal.
    seen: dict[str, Any] = {}

    async def record(fqdn: str, record_type: str, values: Sequence[str], timeout: float) -> bool:
        seen.update(fqdn=fqdn, record_type=record_type, timeout=timeout)
        return True

    monkeypatch.setattr(dns_challenge, "wait_for_records", record)
    await dns_challenge.wait_until_visible("example.com", ["tok"])
    assert seen == {"fqdn": "_acme-challenge.example.com", "record_type": "TXT", "timeout": 90}
