"""The ``dns`` service contract: how a service is identified and how a record is spelled.

Only what both sides of the wire must agree on.  Enforcement lives with whoever enforces it —
grant matching and record validation are in ``coredns_provider``, since a connector app implements
its own — and nothing about ACME or routing belongs here at all.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import attr

DNS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/dns"
DNS_SERVICE_VERSION = "0.1.0"

# Provider id for the router's own implementation.  It has no row in ``apps``, and
# ``service_defaults.app_id`` is a foreign key into that table, so the router is the *implicit*
# provider: it is what you get when no app has claimed the service.
ROUTER_DNS_PROVIDER_ID = "_openhost_router_dns"

# The zone apex, as a zone-relative name.
APEX = "@"

# Fan-out marker for the `zone` field of a request.
ALL_ZONES = "*"

# The only metacharacter in a grant pattern.  A single "*" stays literal, being a real DNS
# wildcard label.
WILDCARD = "**"


class RecordType(StrEnum):
    """The record types this service will create, change, or delete.

    Matches the connector app's allowlist, so both providers accept the same requests: the basics
    plus what mail needs — MX for delivery, TXT for SPF/DKIM/DMARC, SRV for autodiscover, CNAME for
    delegated DKIM selectors.
    """

    A = "A"
    AAAA = "AAAA"
    CAA = "CAA"
    CNAME = "CNAME"
    MX = "MX"
    NS = "NS"
    SRV = "SRV"
    TXT = "TXT"


@attr.s(auto_attribs=True, frozen=True)
class DnsRecord:
    """One record, named relative to its zone.

    ``data`` is None only in a delete, where it selects the whole ``(name, type)`` RRset — the only
    thing a cleanup path can ask for when it doesn't know what is there.
    """

    name: str
    # Plain str, not RecordType: reads pass through whatever a provider reports, which can include
    # types outside the writable set.  Writes are validated against the enum on the way in.
    type: str
    ttl: int = 300
    data: str | None = None


def normalize_zone(zone: str) -> str:
    """Zone files carry a trailing dot; nothing else does."""
    return zone.strip().rstrip(".").lower()


def permission(name: str, rrtype: str, access: str = "rw") -> dict[str, Any]:
    """One entry of an ``X-OpenHost-Permissions`` header, in this service's grant shape.

    Global scope because these grants name a record pattern rather than anything in a provider's
    own data, so an owner can approve them at install time from a manifest.
    """
    return {"grant": {"name": name, "type": rrtype, "access": access}, "scope": "global"}
