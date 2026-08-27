"""The router's own implementation of the ``dns`` service, backed by its CoreDNS zone files.

This is the only thing that reads or writes zone-file *records* — the router's own cert and
dynamic-DNS writes come through here too, via ``client.DnsClient``, so there is one code path
regardless of caller.  (``coredns.py`` still creates and re-points the files themselves; that is
file lifecycle, below the service, and has to work before a zone is servable.)

Returns ``(status, body)`` rather than framework responses; the litestar wiring lives in
``web.routes.services_v2``.  Grant semantics are kept byte-identical to the connector app's
(``internal/grants/match.go``) — two providers of one service disagreeing about what a grant means
would be worse than any bug in either.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.dns import zonefile
from compute_space.core.dns.coredns import public_dns_zones
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.dns.records import ReservedRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.dns.records import normalize_zone
from compute_space.core.dns.records import reject_router_owned
from compute_space.core.logging import logger

# Service identity.  The router and a connector app are interchangeable providers; which one
# applies is the ordinary service default.
DNS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/dns"
DNS_SERVICE_VERSION = "0.1.0"
ROUTER_DNS_PROVIDER_ID = "_openhost_router_dns"

# The router's own consumer identity.  App names are DNS-label-like (see core.app_id), so the
# leading underscore cannot collide with a real one.
ROUTER_CONSUMER_ID = "_openhost_router"
ROUTER_CONSUMER_NAME = "OpenHost Router"

# "**" matches any run of characters.  A single "*" is deliberately literal: it is a real DNS
# wildcard label, so a grant naming "*.app" means that record and nothing else.
WILDCARD = "**"
ALL_ZONES = "*"


@attr.s(auto_attribs=True, frozen=True)
class Grant:
    name: str
    type: str
    access: str

    def matches(self, name: str, rrtype: str) -> bool:
        return _match(self.name, name) and _match(self.type, rrtype)

    @property
    def writable(self) -> bool:
        return self.access == "rw"


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


# ─── zone access ───


class UnknownZone(ValueError):
    """A zone this instance does not serve."""


@attr.s(auto_attribs=True, frozen=True)
class _Zones:
    """The instance's zone files, snapshotted per call so a stale map can't outlive a domain change."""

    paths: dict[str, Path]

    @classmethod
    def load(cls, config: Config, db: sqlite3.Connection) -> _Zones:
        return cls(paths={z.domain: z.zonefile_path for z in public_dns_zones(config, db)})

    def names(self) -> list[str]:
        return sorted(self.paths)

    def resolve(self, requested: str) -> list[str]:
        if requested == ALL_ZONES:
            return self.names()
        wanted = normalize_zone(requested)
        if wanted not in self.paths:
            raise UnknownZone(f"{requested!r} is not a zone this instance serves")
        return [wanted]

    def path(self, zone: str) -> Path:
        path = self.paths[normalize_zone(zone)]
        if not path.exists():
            raise FileNotFoundError(f"zone file for {zone} has not been created yet")
        return path

    def read(self, zone: str, name: str | None, rrtype: str | None) -> list[DnsRecord]:
        records = zonefile.read_records(self.path(zone), zone)
        if name is not None:
            records = [r for r in records if r.name == name]
        if rrtype is not None:
            records = [r for r in records if r.type == rrtype]
        return records

    def write(self, zone: str, op: str, records: list[DnsRecord]) -> list[DnsRecord]:
        path = self.path(zone)
        write = {"set": zonefile.set_records, "append": zonefile.append_records, "delete": zonefile.delete_records}
        logger.info(f"DNS service: {op} {len(records)} record(s) in zone {zone}")
        return write[op](path, zone, records)


# ─── request handling ───

_ERROR_STATUS = {"reserved_record": 403, "permission_required": 403}


def _error(code: str, message: str) -> tuple[int, dict[str, Any]]:
    return _ERROR_STATUS.get(code, 400), {"error": code, "message": message}


def _permission_required(name: str, rrtype: str, access: str) -> tuple[int, dict[str, Any]]:
    """The 403 shape the service proxy understands; being global-scoped, it gets a ``grant_url``
    added on the way out."""
    return 403, {
        "error": "permission_required",
        "message": f"this app has no grant covering {rrtype} records named {name!r}",
        "required_grant": {"grant": {"name": name, "type": rrtype, "access": access}, "scope": "global"},
    }


def _results(results: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """200 when every zone succeeded, 207 when some did, 502 when none did.  A blanket 200 would
    let a caller read a total failure as success."""
    ok = sum(1 for r in results if r["ok"])
    if not results or ok == len(results):
        status = 200
    elif ok == 0:
        status = 502
    else:
        status = 207
    return status, {"ok": ok == len(results), "results": results}


def handle_dns_service_call(
    path: str,
    payload: dict[str, Any],
    grants: list[Grant],
    config: Config,
    db: sqlite3.Connection,
    consumer_id: str = "",
) -> tuple[int, dict[str, Any]]:
    """Dispatch one call; ``path`` is the sub-path after the service endpoint.

    ``consumer_id`` identifies the calling app, and exempts the router from the reserved-record
    rule: those records are reserved *from apps*, and the router is the thing that maintains them.
    """
    zones = _Zones.load(config, db)
    route = "/" + path.strip("/")
    if route == "/zones":
        # Which domains the owner runs is not something an app with no DNS access should learn.
        if not grants:
            return _permission_required(WILDCARD, WILDCARD, "r")
        return 200, {"zones": zones.names()}
    if route == "/records/get":
        return _handle_get(zones, grants, payload)
    if route in ("/records/set", "/records/append", "/records/delete"):
        return _handle_write(zones, grants, payload, route.rsplit("/", 1)[1], consumer_id)
    return _error("invalid_request", f"unknown DNS service path {route!r}")


def _handle_get(zones: _Zones, grants: list[Grant], payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    # An app with no grants can read nothing, so resolving the zone first would only tell it which
    # zones exist.
    if not grants:
        return _results([])
    try:
        targets = zones.resolve(str(payload.get("zone") or "").strip() or ALL_ZONES)
    except UnknownZone as e:
        return _error("unknown_zone", str(e))

    name_filter = str(payload.get("name") or "").strip().lower() or None
    type_filter = str(payload.get("type") or "").strip().upper() or None

    results: list[dict[str, Any]] = []
    for zone in targets:
        try:
            records = zones.read(zone, name_filter, type_filter)
        except Exception as e:  # a broken zone file must not take down the others
            logger.warning(f"DNS service read failed for {zone}: {e}")
            results.append({"zone": zone, "ok": False, "records": [], "error": str(e)})
            continue
        # Ungranted records are omitted rather than refused, so a narrowly scoped app sees a zone
        # containing just its own.
        visible = [_wire(r) for r in records if can_read(grants, r.name, r.type)]
        results.append({"zone": zone, "ok": True, "records": visible})
    return _results(results)


def _handle_write(
    zones: _Zones, grants: list[Grant], payload: dict[str, Any], op: str, consumer_id: str
) -> tuple[int, dict[str, Any]]:
    # Unlike reads, a missing zone is an error rather than a fan-out: defaulting to every zone
    # would let a caller that forgot the field rewrite records across all of them.
    requested = str(payload.get("zone") or "").strip()
    if not requested:
        return _error("zone_required", f"writes must name a zone, or {ALL_ZONES!r} for all configured zones")
    raw = payload.get("records")
    if not isinstance(raw, list) or not raw:
        return _error("invalid_request", "no records given")

    # Authorize and validate the whole batch before resolving the zone or touching a file: the
    # other order would let an ungranted app learn which zones exist from the error it gets back,
    # and would let a partially-permitted request apply its permitted half.
    records: list[DnsRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            return _error("invalid_record", "each record must be an object")
        try:
            record = normalize_record(
                DnsRecord(
                    name=str(item.get("name", "")),
                    type=str(item.get("type", "")),
                    ttl=int(item.get("ttl", 300)),
                    data=item.get("data"),
                ),
                allow_rrset_selector=op == "delete",
            )
        except InvalidRecord as e:
            return _error("invalid_record", str(e))
        except (TypeError, ValueError) as e:
            return _error("invalid_record", f"invalid record: {e}")
        if not can_write(grants, record.name, record.type):
            return _permission_required(record.name, record.type, "rw")
        records.append(record)

    if consumer_id != ROUTER_CONSUMER_ID:
        try:
            reject_router_owned(records)
        except ReservedRecord as e:
            return _error("reserved_record", str(e))

    try:
        targets = zones.resolve(requested)
    except UnknownZone as e:
        return _error("unknown_zone", str(e))
    if not targets:
        return _error("no_zones_configured", "no DNS zones are configured on this instance")

    results: list[dict[str, Any]] = []
    for zone in targets:
        try:
            applied = zones.write(zone, op, records)
        except Exception as e:
            logger.warning(f"DNS service {op} failed for {zone}: {e}")
            results.append({"zone": zone, "ok": False, "records": [], "error": str(e)})
            continue
        results.append({"zone": zone, "ok": True, "records": [_wire(r) for r in applied]})
    return _results(results)


def _wire(record: DnsRecord) -> dict[str, Any]:
    return {"name": record.name, "type": record.type, "ttl": record.ttl, "data": record.data}
