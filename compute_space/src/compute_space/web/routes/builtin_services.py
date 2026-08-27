"""Services the router provides itself, rather than proxying to an app.

The v2 service proxy resolves a consumer's shortname to a provider app and forwards the request.
Some services have no app behind them — the installer is the router by definition, and the router
serves ``dns`` until an owner installs a connector app — so they are dispatched in-process here
instead.

Adding one is a ``BuiltinService`` entry: everything a router-provided service needs in common
(version checking, body parsing, grant assembly, ``grant_url`` decoration on a 403) happens once
in ``dispatch``, so a handler is just ``(call) -> (status, body)``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import anyio
import attr
from litestar import Request

from compute_space.config import Config
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.dns.coredns_provider.service import handle_dns_call
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DNS_SERVICE_VERSION
from compute_space.core.dns.service_api import ROUTER_DNS_PROVIDER_ID
from compute_space.core.dns.service_api import parse_grants


@attr.s(auto_attribs=True, frozen=True)
class BuiltinCall:
    """One request to a router-provided service."""

    consumer_app_id: str
    method: str
    # Sub-path after the service root, e.g. "/records/set".
    path: str
    body: dict[str, Any]
    # The consumer's granted permissions for this service, as the router recorded them.
    permissions: list[dict[str, Any]]
    config: Config
    db: sqlite3.Connection


Handler = Callable[[BuiltinCall], Awaitable[tuple[int, dict[str, Any]]]]


@attr.s(auto_attribs=True, frozen=True)
class BuiltinService:
    url: str
    version: str
    handler: Handler
    # Sentinel provider id when the service can also be provided by an app, so an owner can point
    # the service default elsewhere.  None means the router is the only possible provider.
    provider_id: str | None = None


async def _dns(call: BuiltinCall) -> tuple[int, dict[str, Any]]:
    grants = parse_grants(call.permissions)
    # Off the event loop: the handler does SQLite work and rewrites a zone file.
    return await anyio.to_thread.run_sync(handle_dns_call, call.path, call.body, grants, call.config, call.db)


BUILTIN_SERVICES: tuple[BuiltinService, ...] = (
    BuiltinService(
        url=DNS_SERVICE_URL,
        version=DNS_SERVICE_VERSION,
        handler=_dns,
        provider_id=ROUTER_DNS_PROVIDER_ID,
    ),
)


def builtin_for(service_url: str, db: sqlite3.Connection, provider_override: str | None) -> BuiltinService | None:
    """The router-provided implementation of ``service_url``, if it should serve this call.

    A service with a ``provider_id`` yields to an app the owner has made the default, so installing
    a connector app switches the space over with no further configuration.
    """
    for service in BUILTIN_SERVICES:
        if service.url != service_url:
            continue
        if service.provider_id is None:
            return service
        if provider_override is not None:
            return service if provider_override == service.provider_id else None
        row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (service_url,)).fetchone()
        return service if row is None or row["app_id"] == service.provider_id else None
    return None


async def dispatch(
    service: BuiltinService,
    consumer_app_id: str,
    rest: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> tuple[int, dict[str, Any]]:
    """Run a builtin service call, doing the plumbing every one of them needs."""
    body: dict[str, Any] = {}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            parsed = await request.json()
        except Exception:
            parsed = None
        if parsed is not None and not isinstance(parsed, dict):
            return 400, {"error": "invalid_request", "message": "request body must be a JSON object"}
        body = parsed or {}

    permissions = [
        {"grant": g.grant, "scope": g.scope} for g in get_granted_permissions_v2(consumer_app_id, service.url)
    ]
    return await service.handler(
        BuiltinCall(
            consumer_app_id=consumer_app_id,
            method=str(request.method),
            path=rest,
            body=body,
            permissions=permissions,
            config=config,
            db=db,
        )
    )
