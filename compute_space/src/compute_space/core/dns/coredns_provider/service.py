"""The router's implementation of the ``dns`` service.

Records go into the DB and the zone file is regenerated from them, so an operation here is a few
lines of SQL plus a re-render.  Returns ``(status, body)``; the HTTP wiring is generic and lives
in ``web.routes.builtin_services``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from compute_space.config import Config
from compute_space.core.dns.coredns_provider import store
from compute_space.core.dns.coredns_provider.coredns import public_dns_zones
from compute_space.core.dns.coredns_provider.coredns import write_zone_file
from compute_space.core.dns.coredns_provider.grants import Grant
from compute_space.core.dns.coredns_provider.grants import can_read
from compute_space.core.dns.coredns_provider.grants import can_write
from compute_space.core.dns.coredns_provider.grants import parse as parse_grants
from compute_space.core.dns.coredns_provider.records import InvalidRecord
from compute_space.core.dns.coredns_provider.records import normalize as normalize_record
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import WILDCARD
from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import normalize_zone
from compute_space.core.logging import logger

_OPS = {"set": store.set_records, "append": store.append_records, "delete": store.delete_records}


def handle_dns_call(
    path: str, payload: dict[str, Any], permissions: list[dict[str, Any]], config: Config, db: sqlite3.Connection
) -> tuple[int, dict[str, Any]]:
    """Serve one ``dns`` call.  Permissions arrive in wire form; only this service knows how to
    read its own grant shape."""
    grants = parse_grants(permissions)
    zones = {z.domain: z for z in public_dns_zones(config, db)}
    route = "/" + path.strip("/")
    if route == "/zones":
        # Which domains the owner runs is not something an app with no DNS access should learn.
        if not grants:
            return _permission_required(WILDCARD, WILDCARD, "r")
        return 200, {"zones": sorted(zones)}
    if route == "/records/get":
        return _get(zones, grants, payload, db)
    if route.startswith("/records/") and route.rsplit("/", 1)[1] in _OPS:
        return _write(zones, grants, payload, route.rsplit("/", 1)[1], config, db)
    return _error(400, "invalid_request", f"unknown DNS service path {route!r}")


def _get(
    zones: dict[str, Any], grants: list[Grant], payload: dict[str, Any], db: sqlite3.Connection
) -> tuple[int, dict[str, Any]]:
    # An app with no grants can read nothing, so resolving the zone first would only tell it which
    # zones exist.
    if not grants:
        return _results([])
    try:
        targets = _resolve(zones, str(payload.get("zone") or "").strip() or ALL_ZONES)
    except KeyError as e:
        return _error(400, "unknown_zone", str(e))

    name = str(payload.get("name") or "").strip().lower() or None
    rrtype = str(payload.get("type") or "").strip().upper() or None
    results = []
    for zone in targets:
        # Ungranted records are omitted rather than refused, so a narrowly scoped app sees a zone
        # containing just its own.
        visible = [
            _wire(r)
            for r in store.records_for(db, zone)
            if (name is None or r.name == name)
            and (rrtype is None or r.type == rrtype)
            and can_read(grants, r.name, r.type)
        ]
        results.append({"zone": zone, "ok": True, "records": visible})
    return _results(results)


def _write(
    zones: dict[str, Any],
    grants: list[Grant],
    payload: dict[str, Any],
    op: str,
    config: Config,
    db: sqlite3.Connection,
) -> tuple[int, dict[str, Any]]:
    # Unlike reads, a missing zone is an error rather than a fan-out: defaulting to every zone
    # would let a caller that forgot the field rewrite records across all of them.
    requested = str(payload.get("zone") or "").strip()
    if not requested:
        return _error(400, "zone_required", f"writes must name a zone, or {ALL_ZONES!r} for all of them")
    raw = payload.get("records")
    if not isinstance(raw, list) or not raw:
        return _error(400, "invalid_request", "no records given")

    # Authorize and validate the whole batch before resolving the zone or writing anything: the
    # other order would let an ungranted app learn which zones exist from the error it gets back,
    # and would let a partially-permitted request apply its permitted half.
    records: list[DnsRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            return _error(400, "invalid_record", "each record must be an object")
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
        except (InvalidRecord, TypeError, ValueError) as e:
            return _error(400, "invalid_record", str(e))
        if not can_write(grants, record.name, record.type):
            return _permission_required(record.name, record.type, "rw")
        records.append(record)

    try:
        targets = _resolve(zones, requested)
    except KeyError as e:
        return _error(400, "unknown_zone", str(e))
    if not targets:
        return _error(400, "no_zones_configured", "no DNS zones are configured on this instance")

    public_ip = effective_public_ip(config, db)
    results = []
    for zone in targets:
        applied = _OPS[op](db, zone, records)
        if public_ip:
            write_zone_file(zones[zone], public_ip, db)
        logger.info(f"DNS service: {op} {len(applied)} record(s) in zone {zone}")
        results.append({"zone": zone, "ok": True, "records": [_wire(r) for r in applied]})
    return _results(results)


def _resolve(zones: dict[str, Any], requested: str) -> list[str]:
    if requested == ALL_ZONES:
        return sorted(zones)
    wanted = normalize_zone(requested)
    if wanted not in zones:
        raise KeyError(f"{requested!r} is not a zone this instance serves")
    return [wanted]


def _wire(record: DnsRecord) -> dict[str, Any]:
    return {"name": record.name, "type": record.type, "ttl": record.ttl, "data": record.data}


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": code, "message": message}


def _permission_required(name: str, rrtype: str, access: str) -> tuple[int, dict[str, Any]]:
    return 403, {
        "error": "permission_required",
        "message": f"this app has no grant covering {rrtype} records named {name!r}",
        "required_grant": {"grant": {"name": name, "type": rrtype, "access": access}, "scope": "global"},
    }


def _results(results: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """Per-zone outcomes, with the status reflecting the whole so a caller can't read a total
    failure as success."""
    ok = sum(1 for r in results if r["ok"])
    status = 200 if not results or ok == len(results) else (502 if ok == 0 else 207)
    return status, {"ok": ok == len(results), "results": results}
