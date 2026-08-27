"""The record shape every DNS backend speaks, and the rules about what may be written.

Deliberately the same wire shape as the ``dns`` service (``services/dns/openapi.yaml``): a
zone-relative name, an RR type, a TTL, and unescaped zone-file RDATA.  One flat shape covers every
type, so nothing downstream needs per-type branching.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import attr

APEX = "@"

# Matches the connector app's allowlist so both providers accept the same requests.  Reads are
# unrestricted — whatever is in the zone comes back.
WRITABLE_TYPES = frozenset({"A", "AAAA", "CAA", "CNAME", "MX", "NS", "SRV", "TXT"})

_ROUTER_OWNED_NAMES = frozenset({APEX, "ns", "*"})
_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")


class InvalidRecord(ValueError):
    """A record name, type, or value that can't be written as given."""


class ReservedRecord(InvalidRecord):
    """A router-owned record a service caller tried to write.  Separate from InvalidRecord so the
    service handler can give it its own error code."""


@attr.s(auto_attribs=True, frozen=True)
class DnsRecord:
    """``data`` is None only in a delete, where it selects the whole ``(name, type)`` RRset."""

    name: str
    type: str
    ttl: int = 300
    data: str | None = None

    @property
    def is_rrset_selector(self) -> bool:
        return self.data is None


def normalize_zone(zone: str) -> str:
    """Zone files carry a trailing dot; nothing else does."""
    return zone.strip().rstrip(".").lower()


def normalize_name(name: str, zone: str = "") -> str:
    """Validate and canonicalize a zone-relative record name.

    Fully-qualified input is rejected rather than fixed up: a zone file reads ``www.example.com``
    inside zone ``example.com`` as ``www.example.com.example.com``.
    """
    n = name.strip().lower()
    if not n:
        raise InvalidRecord(f"record name is empty (use {APEX!r} for the zone apex)")
    if n == APEX:
        return APEX
    if n.endswith("."):
        raise InvalidRecord(f"record name {name!r} is fully qualified; names are relative to the zone")
    z = normalize_zone(zone)
    if z and (n == z or n.endswith("." + z)):
        suggestion = n[: -len(z)].rstrip(".") or APEX
        raise InvalidRecord(f"record name {name!r} already includes the zone {z!r} (did you mean {suggestion!r}?)")
    for label in n.split("."):
        # "*" is a real DNS wildcard label, not a pattern, so it stays literal.
        if not label or len(label) > 63 or (label != "*" and not _LABEL_RE.match(label)):
            raise InvalidRecord(f"record name {name!r} has an invalid label {label!r}")
    return n


def normalize_type(rrtype: str) -> str:
    t = rrtype.strip().upper()
    if not t:
        raise InvalidRecord("record type is empty")
    if t not in WRITABLE_TYPES:
        raise InvalidRecord(f"record type {t!r} is not writable (supported: {', '.join(sorted(WRITABLE_TYPES))})")
    return t


def normalize_record(record: DnsRecord, zone: str = "", *, allow_rrset_selector: bool = False) -> DnsRecord:
    """Canonicalize name and type, and require data unless a selector is allowed.

    Omitting data means "whatever is there now", which only makes sense when removing records; a
    set or append with no data is a mistake, not a wildcard.
    """
    name = normalize_name(record.name, zone)
    rrtype = normalize_type(record.type)
    data = record.data.strip() if record.data is not None else None
    if not data:
        if not allow_rrset_selector:
            raise InvalidRecord(f"record {name} {rrtype} has no data")
        data = None
    return DnsRecord(name=name, type=rrtype, ttl=record.ttl, data=data)


def is_router_owned(name: str, rrtype: str) -> bool:
    """True if the router generates and maintains this record itself.

    The apex is reserved only for what the template puts there, so an apex MX or TXT — which is
    what a mail setup needs — is left alone.
    """
    name, rrtype = name.lower(), rrtype.upper()
    if name == APEX and rrtype in ("SOA", "NS"):
        return True
    return name in _ROUTER_OWNED_NAMES and rrtype in ("A", "AAAA")


def reject_router_owned(records: Iterable[DnsRecord]) -> None:
    """Applied to service calls only; the router's own backend writes bypass it."""
    for rec in records:
        if is_router_owned(rec.name, rec.type):
            raise ReservedRecord(
                f"{rec.type} record {rec.name!r} is maintained by OpenHost and cannot be changed "
                f"through the DNS service"
            )
