"""The ``dns`` service contract: what a record is, who may touch it, and who provides the service.

Shared by both sides — ``client`` builds requests, ``coredns_provider`` serves them — so neither
has to import the other to agree on terms.  Record and grant semantics match the connector app
(``libdns`` shapes and ``internal/grants/match.go``); two providers of one service disagreeing
about what a grant means would be worse than any bug in either.
"""

from __future__ import annotations

import json
import re

import attr
import dns.rdata
import dns.rdataclass
import dns.rdatatype
from dns.exception import DNSException

DNS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/dns"
DNS_SERVICE_VERSION = "0.1.0"

# Provider id for the router's own implementation, so an owner can point the service default at a
# connector app instead.  It has no row in ``apps``, so the router is the implicit provider.
ROUTER_DNS_PROVIDER_ID = "_openhost_router_dns"

APEX = "@"
ALL_ZONES = "*"

# "**" matches any run of characters.  A single "*" is deliberately literal: it is a real DNS
# wildcard label, so a grant naming "*.app" means that record and nothing else.
WILDCARD = "**"

WRITABLE_TYPES = frozenset({"A", "AAAA", "CAA", "CNAME", "MX", "NS", "SRV", "TXT"})

_LABEL_RE = re.compile(r"^[a-z0-9_*-]+$")


class InvalidRecord(ValueError):
    """A record that can't be written as given."""


@attr.s(auto_attribs=True, frozen=True)
class DnsRecord:
    """``data`` is None only in a delete, where it selects the whole ``(name, type)`` RRset."""

    name: str
    type: str
    ttl: int = 300
    data: str | None = None


def normalize_zone(zone: str) -> str:
    """Zone files carry a trailing dot; nothing else does."""
    return zone.strip().rstrip(".").lower()


def normalize_record(record: DnsRecord, zone: str = "", *, allow_rrset_selector: bool = False) -> DnsRecord:
    """Canonicalize and validate a record.

    RDATA is parsed here and nowhere else.  Zone files are generated from stored records, so an
    unparseable value would make CoreDNS reject the entire zone and take the domain down; catching
    it at the API boundary turns that into a 400 for one caller.
    """
    name = _normalize_name(record.name, zone)
    rrtype = record.type.strip().upper()
    if rrtype not in WRITABLE_TYPES:
        raise InvalidRecord(f"record type {rrtype!r} is not writable (supported: {', '.join(sorted(WRITABLE_TYPES))})")

    data = (record.data or "").strip() or None
    if data is None:
        if not allow_rrset_selector:
            # Omitting data means "whatever is there now", which only makes sense when removing.
            raise InvalidRecord(f"record {name} {rrtype} has no data")
        return DnsRecord(name=name, type=rrtype, ttl=record.ttl)

    try:
        rdata = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rrtype), data)
    except DNSException as e:
        raise InvalidRecord(f"invalid {rrtype} data {data!r}: {e}") from e
    # from_text derives A vs AAAA from the address family, so an "A" holding an IPv6 literal comes
    # back as AAAA.  Without this an app granted A could write an AAAA.
    if dns.rdatatype.to_text(rdata.rdtype) != rrtype:
        raise InvalidRecord(f"{data!r} is not valid {rrtype} data")
    # Store the canonical form so the rendered zone file is always parseable.
    return DnsRecord(name=name, type=rrtype, ttl=record.ttl, data=rdata.to_text())


def _normalize_name(name: str, zone: str) -> str:
    """Fully-qualified input is rejected rather than fixed up: a zone file reads
    ``www.example.com`` inside zone ``example.com`` as ``www.example.com.example.com``."""
    n = name.strip().lower()
    if not n:
        raise InvalidRecord(f"record name is empty (use {APEX!r} for the zone apex)")
    if n == APEX:
        return APEX
    if n.endswith("."):
        raise InvalidRecord(f"record name {name!r} is fully qualified; names are relative to the zone")
    z = normalize_zone(zone)
    if z and (n == z or n.endswith("." + z)):
        raise InvalidRecord(f"record name {name!r} already includes the zone {z!r}; names are relative")
    for label in n.split("."):
        if not label or len(label) > 63 or not _LABEL_RE.match(label):
            raise InvalidRecord(f"record name {name!r} has an invalid label {label!r}")
    return n


@attr.s(auto_attribs=True, frozen=True)
class Grant:
    name: str
    type: str
    access: str

    def matches(self, name: str, rrtype: str) -> bool:
        return _match(self.name, name) and _match(self.type, rrtype)

    def as_permission(self) -> dict[str, object]:
        """The ``X-OpenHost-Permissions`` entry shape."""
        return {"grant": {"name": self.name, "type": self.type, "access": self.access}, "scope": "global"}


def _match(pattern: str, value: str) -> bool:
    pattern, value = pattern.lower(), value.lower()
    segments = pattern.split(WILDCARD)
    if len(segments) == 1:
        return pattern == value
    prefix, suffix = segments[0], segments[-1]
    if not value.startswith(prefix) or not value.endswith(suffix) or len(prefix) + len(suffix) > len(value):
        return False
    rest = value[len(prefix) : len(value) - len(suffix)]
    for segment in segments[1:-1]:
        if segment:
            i = rest.find(segment)
            if i < 0:
                return False
            rest = rest[i + len(segment) :]
    return True


def parse_grants(permissions: list[dict[str, object]]) -> list[Grant]:
    """Read the grants the router says apply.  Malformed entries are skipped rather than failing
    the request: a bad grant should narrow access, never widen it or break a valid call."""
    out: list[Grant] = []
    for entry in permissions:
        if not isinstance(entry, dict) or entry.get("scope") != "global":
            continue
        grant = entry.get("grant")
        if not isinstance(grant, dict):
            continue
        name, rrtype, access = grant.get("name"), grant.get("type"), grant.get("access")
        if isinstance(name, str) and isinstance(rrtype, str) and access in ("r", "rw"):
            out.append(Grant(name=name.lower(), type=rrtype.upper(), access=access))
    return out


def parse_grants_header(header: str | None) -> list[Grant]:
    if not header or not header.strip():
        return []
    try:
        entries = json.loads(header)
    except ValueError:
        return []
    return parse_grants(entries) if isinstance(entries, list) else []


def can_read(grants: list[Grant], name: str, rrtype: str) -> bool:
    return any(g.matches(name, rrtype) for g in grants)


def can_write(grants: list[Grant], name: str, rrtype: str) -> bool:
    return any(g.access == "rw" and g.matches(name, rrtype) for g in grants)
