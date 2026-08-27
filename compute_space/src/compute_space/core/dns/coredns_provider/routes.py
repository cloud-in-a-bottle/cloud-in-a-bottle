"""The ``dns`` service as an ASGI app: HTTP in, HTTP out, nothing else.

A real Litestar app rather than a bespoke handler signature — the other provider of this service
is an app serving HTTP routes, so this one is too, and routing, body parsing, status codes and
threading come from the framework instead of being reinvented.  No server is involved: the app
object is an ASGI callable that ``core.service_client`` mounts on an httpx transport.

Everything here is translation.  The work is in ``operations``, which raises rather than returning
status codes; the exception handlers below decide what each failure looks like on the wire.  The
payload types are the wire contract in ``services/dns/openapi.yaml``, expressed in Python.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import attr
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import post
from litestar.di import NamedDependency
from litestar.di import Provide
from litestar.exceptions import ValidationException
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


# ─── payloads ───


@attr.s(auto_attribs=True, frozen=True)
class RecordPayload:
    """One record, as it appears on the wire.  ``data`` is absent on a delete that clears a whole
    RRset."""

    name: str
    type: str
    ttl: int = 300
    data: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class GetRequest:
    # Reads default to every zone: a caller is filtered to its own grants anyway, so the broad
    # default costs it nothing and saves it having to know the owner's domain names.
    zone: str = ALL_ZONES
    name: str | None = None
    type: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class WriteRequest:
    # Both default to empty rather than being required, so a caller that omits them gets this
    # service's own error code instead of the framework's validation shape.
    zone: str = ""
    records: list[RecordPayload] = attr.ib(factory=list)


@attr.s(auto_attribs=True, frozen=True)
class ZonesResponse:
    zones: list[str]


@attr.s(auto_attribs=True, frozen=True)
class ZoneResultPayload:
    zone: str
    ok: bool
    records: list[RecordPayload] = attr.ib(factory=list)
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class ResultsResponse:
    ok: bool
    results: list[ZoneResultPayload]


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class GrantPayload:
    name: str
    type: str
    access: str


@attr.s(auto_attribs=True, frozen=True)
class RequiredGrant:
    grant: GrantPayload
    scope: str = "global"


@attr.s(auto_attribs=True, frozen=True)
class PermissionRequiredResponse:
    error: str
    message: str
    required_grant: RequiredGrant


# ─── dependencies ───


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


# ─── routes ───


@post("/zones", status_code=200, sync_to_thread=True)
def list_zones(grants: NamedDependency[list[Grant]], zones: NamedDependency[dict[str, DnsZone]]) -> ZonesResponse:
    # Which domains the owner runs is not something an app with no DNS access should learn.
    if not grants:
        raise operations.PermissionDenied(WILDCARD, WILDCARD, "r")
    return ZonesResponse(zones=sorted(zones))


@post("/records/get", status_code=200, sync_to_thread=True)
def get_records(
    data: GetRequest,
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, DnsZone]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[ResultsResponse]:
    results = operations.read(
        zones,
        grants,
        data.zone or ALL_ZONES,
        (data.name or "").strip().lower() or None,
        (data.type or "").strip().upper() or None,
        db,
    )
    return _results(results)


@post("/records/{op:str}", status_code=200, sync_to_thread=True)
def write_records(
    op: FromPath[str],
    data: WriteRequest,
    grants: NamedDependency[list[Grant]],
    zones: NamedDependency[dict[str, DnsZone]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[ResultsResponse]:
    if op not in operations.WRITE_OPS:
        raise _Rejected("invalid_request", f"unknown DNS service operation {op!r}")
    # Unlike reads, a missing zone is an error rather than a fan-out: defaulting to every zone
    # would let a caller that forgot the field rewrite records across all of them.
    if not data.zone.strip():
        raise _Rejected("zone_required", f"writes must name a zone, or {ALL_ZONES!r} for all of them")
    if not data.records:
        raise _Rejected("invalid_request", "no records given")

    records = [DnsRecord(name=r.name, type=r.type, ttl=r.ttl, data=r.data) for r in data.records]
    results = operations.write(zones, grants, data.zone.strip(), op, records, get_config(), db)
    return _results(results)


# ─── failures ───


@attr.s(auto_attribs=True, frozen=True)
class _Rejected(Exception):
    """A request this service refuses on its own terms, with the error code the API documents."""

    code: str
    message: str


def _rejected(request: Request[Any, Any, Any], exc: _Rejected) -> Response[ErrorResponse]:
    return Response(ErrorResponse(error=exc.code, message=exc.message), status_code=400)


def _bad_record(request: Request[Any, Any, Any], exc: Exception) -> Response[ErrorResponse]:
    """Covers both our own validation and the framework's body parsing, so a malformed record
    reads the same either way."""
    return Response(ErrorResponse(error="invalid_record", message=str(exc)), status_code=400)


def _unknown_zone(request: Request[Any, Any, Any], exc: Exception) -> Response[ErrorResponse]:
    return Response(ErrorResponse(error="unknown_zone", message=str(exc)), status_code=400)


def _no_zones(request: Request[Any, Any, Any], exc: Exception) -> Response[ErrorResponse]:
    return Response(ErrorResponse(error="no_zones_configured", message=str(exc)), status_code=400)


def _permission_required(
    request: Request[Any, Any, Any], exc: operations.PermissionDenied
) -> Response[PermissionRequiredResponse]:
    """The 403 shape the service proxy understands; being global-scoped, it gets a ``grant_url``
    added on the way out."""
    return Response(
        PermissionRequiredResponse(
            error="permission_required",
            message=f"this app has no grant covering {exc.type} records named {exc.name!r}",
            required_grant=RequiredGrant(grant=GrantPayload(name=exc.name, type=exc.type, access=exc.access)),
        ),
        status_code=403,
    )


def _results(results: list[operations.ZoneResult]) -> Response[ResultsResponse]:
    """Per-zone outcomes, with the status reflecting the whole so a caller can't read a total
    failure as success."""
    ok = sum(1 for r in results if r.ok)
    status = 200 if not results or ok == len(results) else (502 if ok == 0 else 207)
    return Response(
        ResultsResponse(
            ok=ok == len(results),
            results=[
                ZoneResultPayload(
                    zone=r.zone,
                    ok=r.ok,
                    records=[RecordPayload(name=x.name, type=x.type, ttl=x.ttl, data=x.data) for x in r.records],
                    error=r.error,
                )
                for r in results
            ],
        ),
        status_code=status,
    )


# Constructed once at import: ~20ms, and it holds no per-call state.  OpenAPI is off — the spec is
# checked in at services/dns/openapi.yaml, and this app is never exposed on a socket.
dns_service_app = Litestar(
    route_handlers=[list_zones, get_records, write_records],
    dependencies={
        "db": Provide(provide_db),
        "grants": Provide(provide_grants, sync_to_thread=False),
        "zones": Provide(provide_zones, sync_to_thread=True),
    },
    exception_handlers={
        _Rejected: _rejected,
        InvalidRecord: _bad_record,
        ValidationException: _bad_record,
        operations.UnknownZone: _unknown_zone,
        operations.NoZonesConfigured: _no_zones,
        operations.PermissionDenied: _permission_required,
    },
    openapi_config=None,
)
