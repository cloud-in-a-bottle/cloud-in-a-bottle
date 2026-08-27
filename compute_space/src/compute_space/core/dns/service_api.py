"""The ``dns`` service contract: who provides it, who is calling, and the permission vocabulary.

Shared by both sides — ``client`` builds grants and addresses a provider, ``coredns_provider``
enforces them — so neither has to import the other to agree on what a grant means.

Grant semantics are byte-identical to the connector app's (``internal/grants/match.go``).  Two
providers of one service disagreeing about what a grant means would be worse than any bug in
either, so this is copied deliberately rather than improved on.
"""

from __future__ import annotations

import json

import attr

# The router and a connector app are interchangeable providers; which one applies is the ordinary
# service default.
DNS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/dns"
DNS_SERVICE_VERSION = "0.1.0"

# Provider id for the router's own implementation.  It has no row in ``apps``, and
# ``service_defaults.app_id`` is a foreign key into that table, so the router is the *implicit*
# provider: it is what you get when no app has claimed the service.
ROUTER_DNS_PROVIDER_ID = "_openhost_router_dns"

# The router's consumer identity when it calls the service itself.  App names are DNS-label-like
# (see core.app_id), so the leading underscore cannot collide with a real one.
ROUTER_CONSUMER_ID = "_openhost_router"
ROUTER_CONSUMER_NAME = "OpenHost Router"

# "**" matches any run of characters.  A single "*" is deliberately literal: it is a real DNS
# wildcard label, so a grant naming "*.app" means that record and nothing else.
WILDCARD = "**"

# Fan-out marker for the `zone` field.
ALL_ZONES = "*"


@attr.s(auto_attribs=True, frozen=True)
class Grant:
    name: str
    type: str
    access: str

    def matches(self, name: str, rrtype: str) -> bool:
        return match(self.name, name) and match(self.type, rrtype)

    @property
    def writable(self) -> bool:
        return self.access == "rw"

    def as_permission(self) -> dict[str, object]:
        """The ``X-OpenHost-Permissions`` entry shape."""
        return {"grant": {"name": self.name, "type": self.type, "access": self.access}, "scope": "global"}


def match(pattern: str, value: str) -> bool:
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


def parse_grants(header: str | None) -> list[Grant]:
    """Read the router-injected permissions header.  Malformed entries are skipped rather than
    failing the request: a bad grant should narrow access, never widen it or break a valid call."""
    if not header or not header.strip():
        return []
    try:
        entries = json.loads(header)
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    out: list[Grant] = []
    for entry in entries:
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
    return any(g.writable and g.matches(name, rrtype) for g in grants)
