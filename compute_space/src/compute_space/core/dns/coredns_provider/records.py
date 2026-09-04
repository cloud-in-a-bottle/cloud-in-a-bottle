from __future__ import annotations

from enum import StrEnum

import attr

# The zone apex, as a zone-relative name.
APEX = "@"


class RecordType(StrEnum):
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
