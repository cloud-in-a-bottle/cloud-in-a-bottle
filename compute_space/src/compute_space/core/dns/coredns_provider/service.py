"""The router's implementation of the ``dns`` service, as an ASGI app.

Records go into the DB and the zone file is regenerated from them, so an operation here is a few
lines of SQL plus a re-render.

A real Litestar app rather than a bespoke handler signature: the other provider of this service is
an app serving HTTP routes, so this one is too, and routing, body parsing, status codes and
threading come from the framework instead of being reinvented.  No server is involved — the app
object is an ASGI callable that ``core.service_client`` mounts on an httpx transport.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import post
from litestar.di import NamedDependency
from litestar.di import Provide
from litestar.params import FromPath

from compute_space.config import Config
from compute_space.config import get_config
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
from compute_space.db import provide_db

_OPS = {"set": store.set_records, "append": store.append_records, "delete": store.delete_records}


PERMISSIONS_HEADER = "X-OpenHost-Permissions"


def provide_grants(request: Request[Any, Any, Any]) -> list[Grant]:
    """Read the caller's grants off the router-injected header.

    A grant payload is defined by the service that issues it, so only this service can read one;
    everything upstream passes it through untouched.
    """
    raw = request.headers.get(PERMISSIONS_HEADER)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        return []
    return parse_grants(entries) if isinstance(entries, list) else []


def provide_zones(db: NamedDependency[sqlite3.Connection]) -> dict[str, Any]:
    return {z.domain: z for z in public_dns_zones(get_config(), db)}


@post("/zones", status_code=200, sync_to_thread=True)
def list_zones(grants: NamedDependency[list[Grant]], zones: NamedDependency[dict[str, Any]]) -> Response[Any]:
    # Which domains the owner runs is not something an app with no DNS access should learn.
    if not grants:
        return _permission_required(WILDCARD, WILDCARD, "r")
    return Response({"zones": sorted(zones)})


@post("/records/get", status_code=200, sync_to_thread=True)
def get_records(
    data: dict[str, Any],
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, Any]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    return _get(zones, grants, data, db)


@post("/records/{op:str}", status_code=200, sync_to_thread=True)
def write_records(
    op: FromPath[str],
    data: dict[str, Any],
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, Any]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    if op not in _OPS:
        return _error(400, "invalid_request", f"unknown DNS service operation {op!r}")
    return _write(zones, grants, data, op, get_config(), db)


# The ASGI app.  Constructed once at import: ~20ms, and it holds no per-call state.  OpenAPI and
# the exception-handling middleware are off — the spec is checked in at services/dns/openapi.yaml
# and this app is never exposed on a socket.
dns_service_app = Litestar(
    route_handlers=[list_zones, get_records, write_records],
    dependencies={
        "db": Provide(provide_db),
        "grants": Provide(provide_grants, sync_to_thread=False),
        "zones": Provide(provide_zones, sync_to_thread=True),
    },
    openapi_config=None,
)


def _get(zones: dict[str, Any], grants: list[Grant], payload: dict[str, Any], db: sqlite3.Connection) -> Response[Any]:
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
) -> Response[Any]:
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


def _error(status: int, code: str, message: str) -> Response[Any]:
    return Response({"error": code, "message": message}, status_code=status)


def _permission_required(name: str, rrtype: str, access: str) -> Response[Any]:
    """The 403 shape the service proxy understands; being global-scoped, it gets a ``grant_url``
    added on the way out."""
    return Response(
        {
            "error": "permission_required",
            "message": f"this app has no grant covering {rrtype} records named {name!r}",
            "required_grant": {"grant": {"name": name, "type": rrtype, "access": access}, "scope": "global"},
        },
        status_code=403,
    )


def _results(results: list[dict[str, Any]]) -> Response[Any]:
    """Per-zone outcomes, with the status reflecting the whole so a caller can't read a total
    failure as success."""
    ok = sum(1 for r in results if r["ok"])
    status = 200 if not results or ok == len(results) else (502 if ok == 0 else 207)
    return Response({"ok": ok == len(results), "results": results}, status_code=status)
