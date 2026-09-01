"""What a record is, to this provider.

Records live in memory on the :class:`~compute_space.core.dns.coredns_provider.interface.
InternalDnsProvider` and are rendered into the zone files whenever they change.  Nothing persists
them: every record the space serves is re-published on each boot, so a copy on disk could only go
stale.

They carry no zone.  Every zone the provider serves is an alias for the same space, so a record
that existed in only some of them would make the zones disagree.
"""

from __future__ import annotations

from enum import StrEnum

import attr

# The zone apex, as a zone-relative name.
APEX = "@"


class RecordType(StrEnum):
    """The record types the router writes into its own zones.

    Only what we actually publish: addresses for the space itself, and TXT for DNS-01 challenge
    tokens.  Add to this when something needs another type — the rendering below is generic, the
    enum is the deliberate part.
    """

    A = "A"
    TXT = "TXT"


@attr.s(auto_attribs=True, frozen=True)
class DnsRecord:
    """One record, named relative to the zone it renders into."""

    name: str
    type: RecordType
    ttl: int
    data: str

    @property
    def rdata(self) -> str:
        """The data as a zone file spells it.

        TXT is the only type that needs quoting: its rdata is a character string, so an unquoted
        token with a space or a semicolon in it would be read as several strings or as a comment,
        and CoreDNS would refuse the whole zone.
        """
        if self.type is RecordType.TXT:
            escaped = self.data.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return self.data


def normalize_zone(zone: str) -> str:
    """Zone files carry a trailing dot; nothing else does."""
    return zone.strip().rstrip(".").lower()
