"""Services the router provides itself, rather than proxying to an app.

A builtin is an ASGI app: the same thing an app provider is, minus the socket, so nothing here
defines a request/response contract of its own.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import attr

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.proxy_target import AsgiApp

# Permission entries stay in wire form (``{"grant": ..., "scope": ...}``) everywhere outside the
# service that issued them: a grant payload is defined by that service, so only it can read one.
Permissions = list[dict[str, Any]]


@attr.s(auto_attribs=True, frozen=True)
class BuiltinService:
    service_url: str
    version: str
    app: AsgiApp


BUILTIN_SERVICES: tuple[BuiltinService, ...] = ()


def builtin_for(
    service_url: str, db: sqlite3.Connection, provider_override: str | None = None
) -> BuiltinService | None:
    """The router-provided implementation of ``service_url``, if it should serve this call.

    A builtin yields to whichever app the owner has made the default, so installing a provider app
    switches the space over with no further configuration and clearing the default hands the
    service back.  ``service_defaults.app_id`` is a foreign key into ``apps``, so a default row
    always names an app and never the router.
    """
    service = next((s for s in BUILTIN_SERVICES if s.service_url == service_url), None)
    if service is None:
        return None
    if provider_override is not None:
        return service if provider_override == ROUTER_APP_ID else None
    row = db.execute("SELECT 1 FROM service_defaults WHERE service_url = ?", (service_url,)).fetchone()
    return None if row else service
