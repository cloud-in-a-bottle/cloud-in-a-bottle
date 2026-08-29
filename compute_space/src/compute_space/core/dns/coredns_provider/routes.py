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
from collections.abc import Callable
from collections.abc import Generator
from typing import Any

import attr
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import post
from litestar.di import NamedDependency
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import ValidationException
from litestar.params import FromPath

from compute_space.core.dns.coredns_provider import operations
from compute_space.core.dns.coredns_provider.coredns import DnsZone
from compute_space.core.dns.coredns_provider.grants import Grant
from compute_space.core.dns.coredns_provider.grants import parse as parse_grants
from compute_space.core.dns.coredns_provider.records import InvalidRecord
from compute_space.core.dns.coredns_provider.settings import DnsSettings
from compute_space.core.dns.service_api import ALL_ZONES
from compute_space.core.dns.service_api import DnsRecord

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


# ─── routes ───


@post("/records/get", status_code=200, sync_to_thread=True)
def get_records(
    data: GetRequest,
    grants: NamedDependency[list[Grant]],
    db: NamedDependency[sqlite3.Connection],
) -> Response[ResultsResponse]:
    results = operations.read(
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
    zones: NamedDependency[tuple[DnsZone, ...]],
    settings: NamedDependency[DnsSettings],
    db: NamedDependency[sqlite3.Connection],
) -> Response[ResultsResponse]:
    if op not in operations.WRITE_OPS:
        raise InvalidRequest(detail=f"unknown DNS service operation {op!r}")
    # Unlike reads, a missing zone is an error rather than a fan-out: defaulting to every zone
    # would let a caller that forgot the field rewrite records across all of them.
    if not data.zone.strip():
        raise ZoneRequired(detail=f"writes must name a zone, or {ALL_ZONES!r} for all of them")
    if not data.records:
        raise InvalidRequest(detail="no records given")

    records = [DnsRecord(name=r.name, type=r.type, ttl=r.ttl, data=r.data) for r in data.records]
    results = operations.write(grants, data.zone.strip(), op, records, zones, settings, db)
    return _results(results)


# ─── failures ───


class DnsServiceException(HTTPException):
    """A request this service refuses on its own terms.

    A Litestar exception, so a route just raises and the framework unwinds; ``error_code`` is what
    distinguishes it on the wire.
    """

    error_code = "invalid_request"


class ZoneRequired(DnsServiceException):
    status_code = 400
    error_code = "zone_required"


class InvalidRequest(DnsServiceException):
    status_code = 400
    error_code = "invalid_request"


def _render_error(request: Request[Any, Any, Any], exc: Exception) -> Response[Any]:
    """Render any of this service's failures in the shape the API documents.

    One renderer rather than a handler per failure, and it exists at all only because
    ``{"error", "message"}`` is the service's contract — shared with the connector app — whereas
    Litestar's default body is ``{"status_code", "detail", "extra"}``.
    """
    # Litestar's own parse failure carries no code of ours, and always means a record it could
    # not read.
    default = "invalid_record" if isinstance(exc, ValidationException) else "invalid_request"
    code = getattr(exc, "error_code", default)
    message = getattr(exc, "detail", None) or str(exc)
    status = getattr(exc, "status_code", 400)

    if isinstance(exc, operations.PermissionDenied):
        return Response(
            PermissionRequiredResponse(
                error=code,
                message=f"this app has no grant covering {exc.type} records named {exc.name!r}",
                required_grant=RequiredGrant(grant=GrantPayload(name=exc.name, type=exc.type, access=exc.access)),
            ),
            status_code=403,
        )
    return Response(ErrorResponse(error=code, message=message), status_code=status)


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


DbProvider = Callable[[], Generator[sqlite3.Connection, None, None]]
ZoneSupplier = Callable[[], tuple[DnsZone, ...]]


def build_coredns_service_app(provide_db: DbProvider, settings: DnsSettings, zones: ZoneSupplier) -> Litestar:
    """The router's own implementation of the ``dns`` service, which it serves until an owner
    installs a connector app and makes it the default.

    The DB dependency and the settings are passed in rather than imported: both are the compute
    space's, and this package should need nothing from the application around it to be built.
    ``zones`` is called per request rather than captured, so a zone added after the app was built
    is one a write still reaches.

    Built once and reused — ~20ms, and it holds no per-call state.  OpenAPI is off: the spec is
    checked in at services/dns/openapi.yaml, and this app is never exposed on a socket.
    """
    return Litestar(
        route_handlers=[get_records, write_records],
        dependencies={
            "db": Provide(provide_db),
            "grants": Provide(provide_grants, sync_to_thread=False),
            "settings": Provide(lambda: settings, sync_to_thread=False, use_cache=True),
            "zones": Provide(lambda: zones(), sync_to_thread=False),
        },
        exception_handlers={
            DnsServiceException: _render_error,
            operations.DnsOperationError: _render_error,
            InvalidRecord: _render_error,
            # Litestar rejects a malformed body before the route runs; render it like any bad record
            # rather than leaking the framework's error shape.
            ValidationException: _render_error,
        },
        openapi_config=None,
    )
