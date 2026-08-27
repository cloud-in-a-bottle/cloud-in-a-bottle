"""DNS-01 challenge records: naming, TTL, and cleanup semantics."""

from __future__ import annotations

from typing import Any

import attr
import pytest

from compute_space.core.tls import challenge


@attr.s(auto_attribs=True)
class _RecordingDns:
    """Stands in for DnsClient, capturing the record-level calls the challenge helpers make."""

    propagation_timeout_seconds: float = 120.0
    calls: list[tuple[str, ...]] = attr.ib(factory=list)

    async def set_records(self, fqdn: str, rrtype: str, values: list[str], ttl: int = 300) -> None:
        self.calls.append(("set", fqdn, rrtype, ",".join(values), str(ttl)))

    async def delete_records(self, fqdn: str, rrtype: str) -> None:
        self.calls.append(("delete", fqdn, rrtype))


def test_a_wildcard_order_validates_against_the_base_domain() -> None:
    # *.example.com and example.com share one challenge name, which is why both authorizations'
    # tokens have to be live at the same time.
    assert challenge.challenge_fqdn("*.example.com") == "_acme-challenge.example.com"
    assert challenge.challenge_fqdn("example.com") == "_acme-challenge.example.com"


def test_only_a_leading_wildcard_label_is_stripped() -> None:
    # `lstrip("*.")` would take a character set and mangle these.
    assert challenge.challenge_fqdn("host.example.com") == "_acme-challenge.host.example.com"
    assert challenge.challenge_fqdn("*.a.example.com") == "_acme-challenge.a.example.com"


@pytest.mark.asyncio
async def test_publishing_replaces_the_rrset_with_a_short_ttl() -> None:
    # Replace, not append: a run that died before cleaning up must not leave stale tokens. Short
    # TTL so the previous run's token isn't served from a resolver cache during a renewal.
    dns: Any = _RecordingDns()
    await challenge.publish(dns, "example.com", ["base", "wildcard"])
    assert dns.calls == [("set", "_acme-challenge.example.com", "TXT", "base,wildcard", "60")]


@pytest.mark.asyncio
async def test_clearing_removes_the_whole_rrset() -> None:
    dns: Any = _RecordingDns()
    await challenge.clear(dns, "*.example.com")
    assert dns.calls == [("delete", "_acme-challenge.example.com", "TXT")]


@pytest.mark.asyncio
async def test_the_wait_uses_the_providers_own_timeout(monkeypatch: Any) -> None:
    # A registrar is far slower than our own zone file, and the client knows which one is in play.
    seen: dict[str, Any] = {}

    async def record(fqdn: str, rrtype: str, values: list[str], timeout: float) -> bool:
        seen.update(fqdn=fqdn, rrtype=rrtype, timeout=timeout)
        return True

    monkeypatch.setattr(challenge, "wait_for_records", record)
    await challenge.wait_until_visible(_RecordingDns(propagation_timeout_seconds=600.0), "example.com", ["tok"])  # type: ignore[arg-type]
    assert seen == {"fqdn": "_acme-challenge.example.com", "rrtype": "TXT", "timeout": 600.0}
