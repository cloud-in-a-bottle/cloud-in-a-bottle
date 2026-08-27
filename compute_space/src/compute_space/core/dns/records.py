"""The record shape shared by every DNS backend, and the rules about what may be written.

Deliberately the same wire shape the ``dns`` service speaks (see ``services/dns/openapi.yaml``):
a zone-relative name, an RR type, a TTL, and unescaped zone-file RDATA.  One flat shape covers
every type, so neither the backends nor the service handler need per-type branching.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import attr

# The zone-relative name of the zone itself, matching the service API and libdns.
APEX = "@"

# Types the service API will create, change, or delete.  Matches the connector app's allowlist so
# the two providers accept the same requests.  Reads are unrestricted — whatever is in the zone
# comes back, including types outside this set.
WRITABLE_TYPES = frozenset({"A", "AAAA", "CAA", "CNAME", "MX", "NS", "SRV", "TXT"})

# Records the router generates and keeps in sync itself: the zone apex, the nameserver glue, and
# the wildcard that routes every app subdomain.  A domain-set change or a dynamic-DNS update
# rewrites all three, so a service caller writing them would have its change silently undone —
# and a broad grant could otherwise delete the wildcard and take the whole space offline.
ROUTER_OWNED_NAMES = frozenset({APEX, "ns", "*"})
ROUTER_OWNED_APEX_TYPES = frozenset({"SOA", "NS"})

_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")


class InvalidRecord(ValueError):
    """A record name, type, or value that can't be written as given."""


class ReservedRecord(InvalidRecord):
    """A router-owned record a service caller tried to write.  Distinct from InvalidRecord so the
    service handler can report it as its own error code rather than a generic bad request."""


@attr.s(auto_attribs=True, frozen=True)
class DnsRecord:
    """One record, relative to a zone.  ``data`` is None only in a delete, where it means "whatever
    is currently at this name and type" — see ``DnsBackend.delete_records``."""

    name: str
    type: str
    ttl: int = 300
    data: str | None = None

    @property
    def is_rrset_selector(self) -> bool:
        return self.data is None


def normalize_zone(zone: str) -> str:
    """Lowercase a zone and drop the trailing dot.  Zone files carry the dot; nothing else does."""
    return zone.strip().rstrip(".").lower()


def normalize_name(name: str, zone: str = "") -> str:
    """Validate and canonicalize a zone-relative record name.

    Fully-qualified input is rejected rather than fixed up: a zone file reads ``www.example.com``
    inside zone ``example.com`` as ``www.example.com.example.com``, so quietly accepting it would
    point the record at a name the caller did not intend.
    """
    n = name.strip().lower()
    if not n:
        raise InvalidRecord(f"record name is empty (use {APEX!r} for the zone apex)")
    if n == APEX:
        return APEX
    if n.endswith("."):
        raise InvalidRecord(
            f"record name {name!r} is fully qualified; names are relative to the zone (use {APEX!r} for the apex)"
        )
    z = normalize_zone(zone)
    if z and (n == z or n.endswith("." + z)):
        suggestion = n[: -len(z)].rstrip(".") or APEX
        raise InvalidRecord(
            f"record name {name!r} already includes the zone {z!r}; names are relative (did you mean {suggestion!r}?)"
        )
    for label in n.split("."):
        if not label:
            raise InvalidRecord(f"record name {name!r} has an empty label")
        if len(label) > 63:
            raise InvalidRecord(f"record name {name!r} has a label longer than 63 characters")
        # "*" is a real DNS wildcard label, not a pattern, so it stays literal.
        if label != "*" and not _LABEL_RE.match(label):
            raise InvalidRecord(f"record name {name!r} contains an invalid label {label!r}")
    return n


def normalize_type(rrtype: str, *, writable: bool = True) -> str:
    """Uppercase an RR type, optionally checking it against the write allowlist."""
    t = rrtype.strip().upper()
    if not t:
        raise InvalidRecord("record type is empty")
    if writable and t not in WRITABLE_TYPES:
        raise InvalidRecord(f"record type {t!r} is not writable (supported: {', '.join(sorted(WRITABLE_TYPES))})")
    return t


def normalize_record(record: DnsRecord, zone: str = "", *, allow_rrset_selector: bool = False) -> DnsRecord:
    """Canonicalize a record's name and type, and require data unless a selector is allowed.

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

    The apex is only reserved for the records the template writes there (SOA, NS, A/AAAA); an app
    adding an apex MX or TXT — SPF and mail setups need exactly that — is left alone.
    """
    name = name.lower()
    rrtype = rrtype.upper()
    if name == APEX:
        return rrtype in ROUTER_OWNED_APEX_TYPES or rrtype in ("A", "AAAA")
    if name in ROUTER_OWNED_NAMES:
        return rrtype in ("A", "AAAA")
    return False


def reject_router_owned(records: Iterable[DnsRecord]) -> None:
    """Raise if any record is one the router maintains.  Applied to service calls only; the
    router's own backend calls go straight to the backend and bypass this."""
    for rec in records:
        if is_router_owned(rec.name, rec.type):
            raise ReservedRecord(
                f"{rec.type} record {rec.name!r} is maintained by OpenHost and cannot be changed "
                f"through the DNS service"
            )
