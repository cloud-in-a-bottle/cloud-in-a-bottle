"""Services the router provides itself, rather than proxying to an app.

The v2 proxy resolves a consumer's shortname to a provider app and forwards the request.  Some
services have no app behind them — a service the router implements directly, or one an app *may*
provide but hasn't been installed for yet — so they run in-process instead.

Registering one is an entry here plus an ASGI app: the same thing an app provider is, minus the
socket, so nothing here defines a request/response contract of its own.  ``service_client`` and
the proxy both consult ``builtin_for``, so which provider serves a service is decided in exactly
one place.

The registry is empty for now; the first entry arrives with the ``dns`` service.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import attr
from litestar.types import ASGIApp

# Permission entries stay in wire form (``{"grant": ..., "scope": ...}``) everywhere outside the
# service that issued them: a grant payload is defined by that service, so only it can read one.
Permissions = list[dict[str, Any]]


@attr.s(auto_attribs=True, frozen=True)
class BuiltinService:
    url: str
    version: str
    app: ASGIApp
    # Sentinel provider id when an app can provide the service too, so an owner can point the
    # service default elsewhere.  None means the router is the only possible provider.
    provider_id: str | None = None


BUILTIN_SERVICES: tuple[BuiltinService, ...] = ()


def builtin_for(
    service_url: str, db: sqlite3.Connection, provider_override: str | None = None
) -> BuiltinService | None:
    """The router-provided implementation of ``service_url``, if it should serve this call.

    A service with a ``provider_id`` yields to an app the owner has made the default, so installing
    a provider app switches the space over with no further configuration.
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
