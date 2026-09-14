from __future__ import annotations

from enum import StrEnum

import attr

# The zone apex, as a zone-relative name.
APEX = "@"

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LOCAL_ONLY_SUFFIXES = ("local", "lvh.me")


def normalize_record_name(name: str) -> str:
    """Fold DNS owner-name case without changing apex, relative-name or escape syntax."""
    return name.translate(_ASCII_LOWER)


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


def is_local_only_zone(zone: str) -> bool:
    """Names handled by mDNS/loopback outside containers, served only in the gateway DNS view."""
    name = normalize_zone(zone)
    return any(name == suffix or name.endswith(f".{suffix}") for suffix in _LOCAL_ONLY_SUFFIXES)
