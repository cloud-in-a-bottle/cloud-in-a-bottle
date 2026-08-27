"""Services the router provides itself, rather than proxying to an app.

The v2 proxy resolves a consumer's shortname to a provider app and forwards the request.  Some
services have no app behind them — the installer is the router by definition, and the router serves
``dns`` until an owner installs a connector app — so they run in-process instead.

Registering one is an entry here plus an ASGI app — the same thing an app provider is, minus the
socket — so nothing here defines a request/response contract of its own.  ``service_client`` and
the proxy both consult ``builtin_for``, so which provider serves a service is decided in exactly
one place.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import attr
from litestar.types import ASGIApp

from compute_space.core.dns.coredns_provider.routes import dns_service_app
from compute_space.core.dns.service_api import DNS_SERVICE_URL
from compute_space.core.dns.service_api import DNS_SERVICE_VERSION
from compute_space.core.dns.service_api import ROUTER_DNS_PROVIDER_ID

# Permission entries stay in wire form (``{"grant": ..., "scope": ...}``) everywhere outside the
# service that issued them: a grant payload is defined by that service, so only it can read one.
Permissions = list[dict[str, Any]]

# A handler takes the sub-path after the service root, the JSON body, and the caller's permission
# entries, and returns an HTTP status and a JSON body — the same contract an app provider answers


@attr.s(auto_attribs=True, frozen=True)
class BuiltinService:
    url: str
    version: str
    app: ASGIApp
    # Sentinel provider id when an app can provide the service too, so an owner can point the
    # service default elsewhere.  None means the router is the only possible provider.
    provider_id: str | None = None


BUILTIN_SERVICES: tuple[BuiltinService, ...] = (
    BuiltinService(
        url=DNS_SERVICE_URL,
        version=DNS_SERVICE_VERSION,
        app=dns_service_app,
        provider_id=ROUTER_DNS_PROVIDER_ID,
    ),
)


def builtin_for(
    service_url: str, db: sqlite3.Connection, provider_override: str | None = None
) -> BuiltinService | None:
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
