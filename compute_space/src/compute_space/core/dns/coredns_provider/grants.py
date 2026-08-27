"""How this provider reads the grants a caller holds.

A grant payload is defined by the service that issues it (see ``core.auth.permissions_v2``); this
is the ``dns`` service's shape — a record name pattern, a record type, and an access level — and
the matching rules that go with it.  Enforcement is a provider concern, so it lives here rather
than in the shared contract: a connector app enforces the same rules in its own code.

Semantics are byte-identical to the connector app's ``internal/grants/match.go``.  Two providers of
one service disagreeing about what a grant means would be worse than any bug in either.
"""

from __future__ import annotations

from typing import Any

import attr

from compute_space.core.dns.service_api import WILDCARD


@attr.s(auto_attribs=True, frozen=True)
class Grant:
    name: str
    type: str
    access: str

    def matches(self, name: str, rrtype: str) -> bool:
        return _match(self.name, name) and _match(self.type, rrtype)


def _match(pattern: str, value: str) -> bool:
    """``**`` matches any run of characters; everything else is literal.

    A single ``*`` is deliberately not a metacharacter — it is a real DNS wildcard label, so a
    grant naming ``*.app`` means that record and nothing else.
    """
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


def parse(permissions: list[dict[str, Any]]) -> list[Grant]:
    """Read the router-supplied permission entries into grants this service understands.

    Only ``global`` scope is honored: these grants name a record pattern rather than anything in a
    provider's own data, so an owner can approve them at install time from the manifest.
    Malformed entries are skipped rather than failing the request — a bad grant should narrow
    access, never widen it or break an otherwise valid call.
    """
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


def can_read(grants: list[Grant], name: str, rrtype: str) -> bool:
    return any(g.matches(name, rrtype) for g in grants)


def can_write(grants: list[Grant], name: str, rrtype: str) -> bool:
    return any(g.access == "rw" and g.matches(name, rrtype) for g in grants)
