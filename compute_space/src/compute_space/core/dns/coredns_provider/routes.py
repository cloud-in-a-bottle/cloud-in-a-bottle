"""The ``dns`` service as an ASGI app: HTTP in, HTTP out, nothing else.

A real Litestar app rather than a bespoke handler signature — the other provider of this service
is an app serving HTTP routes, so this one is too, and routing, body parsing, status codes and
threading come from the framework instead of being reinvented.  No server is involved: the app
object is an ASGI callable that ``core.service_client`` mounts on an httpx transport.

Everything here is translation.  The work is in ``operations``, which raises rather than returning
status codes; this module decides what each failure looks like on the wire.
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

from compute_space.config import get_config
from compute_space.core.dns.coredns_provider import operations
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.grants import Grant
from compute_space.core.dns.coredns_provider.grants import parse as parse_grants
from compute_space.core.dns.coredns_provider.records import InvalidRecord
from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import WILDCARD
from compute_space.core.dns.service_api import DnsRecord
from compute_space.db import provide_db

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


def provide_zones(db: NamedDependency[sqlite3.Connection]) -> dict[str, DnsZone]:
    return operations.zone_map(get_config(), db)


@post("/zones", status_code=200, sync_to_thread=True)
def list_zones(grants: NamedDependency[list[Grant]], zones: NamedDependency[dict[str, DnsZone]]) -> Response[Any]:
    # Which domains the owner runs is not something an app with no DNS access should learn.
    if not grants:
        return _permission_required(operations.PermissionDenied(WILDCARD, WILDCARD, "r"))
    return Response({"zones": sorted(zones)})


@post("/records/get", status_code=200, sync_to_thread=True)
def get_records(
    data: dict[str, Any],
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, DnsZone]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    try:
        results = operations.read(
            zones,
            grants,
            str(data.get("zone") or "").strip() or ALL_ZONES,
            str(data.get("name") or "").strip().lower() or None,
            str(data.get("type") or "").strip().upper() or None,
            db,
        )
    except operations.UnknownZone as e:
        return _error(400, "unknown_zone", str(e))
    return _results(results)


@post("/records/{op:str}", status_code=200, sync_to_thread=True)
def write_records(
    op: FromPath[str],
    data: dict[str, Any],
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, DnsZone]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    if op not in operations.WRITE_OPS:
        return _error(400, "invalid_request", f"unknown DNS service operation {op!r}")
    # Unlike reads, a missing zone is an error rather than a fan-out: defaulting to every zone
    # would let a caller that forgot the field rewrite records across all of them.
    requested = str(data.get("zone") or "").strip()
    if not requested:
        return _error(400, "zone_required", f"writes must name a zone, or {ALL_ZONES!r} for all of them")
    raw = data.get("records")
    if not isinstance(raw, list) or not raw:
        return _error(400, "invalid_request", "no records given")
    try:
        results = operations.write(zones, grants, requested, op, _records_from(raw), get_config(), db)
    except (InvalidRecord, TypeError, ValueError) as e:
        return _error(400, "invalid_record", str(e))
    except operations.PermissionDenied as e:
        return _permission_required(e)
    except operations.UnknownZone as e:
        return _error(400, "unknown_zone", str(e))
    except operations.NoZonesConfigured as e:
        return _error(400, "no_zones_configured", str(e))
    return _results(results)


# Constructed once at import: ~20ms, and it holds no per-call state.  OpenAPI is off — the spec is
# checked in at services/dns/openapi.yaml, and this app is never exposed on a socket.
dns_service_app = Litestar(
    route_handlers=[list_zones, get_records, write_records],
    dependencies={
        "db": Provide(provide_db),
        "grants": Provide(provide_grants, sync_to_thread=False),
        "zones": Provide(provide_zones, sync_to_thread=True),
    },
    openapi_config=None,
)


def _records_from(raw: list[Any]) -> list[DnsRecord]:
    """Wire dicts to records.  Shape only — validating the values is ``records.normalize``."""
    records = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each record must be an object")
        records.append(
            DnsRecord(
                name=str(item.get("name", "")),
                type=str(item.get("type", "")),
                ttl=int(item.get("ttl", 300)),
                data=item.get("data"),
            )
        )
    return records


def _wire(record: DnsRecord) -> dict[str, Any]:
    return {"name": record.name, "type": record.type, "ttl": record.ttl, "data": record.data}


def _error(status: int, code: str, message: str) -> Response[Any]:
    return Response({"error": code, "message": message}, status_code=status)


def _permission_required(denial: operations.PermissionDenied) -> Response[Any]:
    """The 403 shape the service proxy understands; being global-scoped, it gets a ``grant_url``
    added on the way out."""
    return Response(
        {
            "error": "permission_required",
            "message": f"this app has no grant covering {denial.type} records named {denial.name!r}",
            "required_grant": {
                "grant": {"name": denial.name, "type": denial.type, "access": denial.access},
                "scope": "global",
            },
        },
        status_code=403,
    )


def _results(results: list[operations.ZoneResult]) -> Response[Any]:
    """Per-zone outcomes, with the status reflecting the whole so a caller can't read a total
    failure as success."""
    ok = sum(1 for r in results if r.ok)
    status = 200 if not results or ok == len(results) else (502 if ok == 0 else 207)
    body = [
        {
            "zone": r.zone,
            "ok": r.ok,
            "records": [_wire(rec) for rec in r.records],
            **({"error": r.error} if r.error else {}),
        }
        for r in results
    ]
    return Response({"ok": ok == len(results), "results": body}, status_code=status)
