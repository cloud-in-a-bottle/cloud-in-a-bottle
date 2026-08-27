"""The router's own implementation of the ``dns`` service, for apps that want to write records.

Returns ``(status, body)`` rather than framework responses; the litestar wiring lives in
``web.routes.services_v2``.  Grant semantics are kept byte-identical to the connector app's
(``internal/grants/match.go``) — two providers of one service disagreeing about what a grant means
would be worse than any bug in either.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.dns.backend import LocalZoneFileBackend
from compute_space.core.dns.backend import UnknownZone
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.records import InvalidRecord
from compute_space.core.dns.records import ReservedRecord
from compute_space.core.dns.records import normalize_record
from compute_space.core.dns.records import normalize_zone
from compute_space.core.dns.records import reject_router_owned
from compute_space.core.logging import logger

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
) -> tuple[int, dict[str, Any]]:
    """Dispatch one call; ``path`` is the sub-path after the service endpoint."""
    backend = LocalZoneFileBackend.create(config, db)
    route = "/" + path.strip("/")
    if route == "/zones":
        # Which domains the owner runs is not something an app with no DNS access should learn.
        if not grants:
            return _permission_required(WILDCARD, WILDCARD, "r")
        return 200, {"zones": backend.zones()}
    if route == "/records/get":
        return _handle_get(backend, grants, payload)
    if route in ("/records/set", "/records/append", "/records/delete"):
        return _handle_write(backend, grants, payload, route.rsplit("/", 1)[1])
    return _error("invalid_request", f"unknown DNS service path {route!r}")


def _resolve_zones(backend: LocalZoneFileBackend, requested: str) -> list[str]:
    if requested == ALL_ZONES:
        return backend.zones()
    wanted = normalize_zone(requested)
    if wanted not in backend.zones():
        raise UnknownZone(f"{requested!r} is not a zone this instance serves")
    return [wanted]


def _handle_get(
    backend: LocalZoneFileBackend, grants: list[Grant], payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    # An app with no grants can read nothing, so resolving the zone first would only tell it which
    # zones exist.
    if not grants:
        return _results([])
    try:
        zones = _resolve_zones(backend, str(payload.get("zone") or "").strip() or ALL_ZONES)
    except UnknownZone as e:
        return _error("unknown_zone", str(e))

    name_filter = str(payload.get("name") or "").strip().lower() or None
    type_filter = str(payload.get("type") or "").strip().upper() or None

    results: list[dict[str, Any]] = []
    for zone in zones:
        try:
            records = backend.get_records(zone, name_filter, type_filter)
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
    backend: LocalZoneFileBackend, grants: list[Grant], payload: dict[str, Any], op: str
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

    try:
        reject_router_owned(records)
    except ReservedRecord as e:
        return _error("reserved_record", str(e))

    try:
        zones = _resolve_zones(backend, requested)
    except UnknownZone as e:
        return _error("unknown_zone", str(e))
    if not zones:
        return _error("no_zones_configured", "no DNS zones are configured on this instance")

    method = {"set": backend.set_records, "append": backend.append_records, "delete": backend.delete_records}[op]
    results: list[dict[str, Any]] = []
    for zone in zones:
        try:
            applied = method(zone, records)
        except Exception as e:
            logger.warning(f"DNS service {op} failed for {zone}: {e}")
            results.append({"zone": zone, "ok": False, "records": [], "error": str(e)})
            continue
        results.append({"zone": zone, "ok": True, "records": [_wire(r) for r in applied]})
    return _results(results)


def _wire(record: DnsRecord) -> dict[str, Any]:
    return {"name": record.name, "type": record.type, "ttl": record.ttl, "data": record.data}
