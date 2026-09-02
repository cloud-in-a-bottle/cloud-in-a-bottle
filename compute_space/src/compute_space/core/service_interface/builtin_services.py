"""Services the router provides itself, rather than proxying to an app.

A builtin is an ASGI app: the same thing an app provider is, minus the socket, so nothing here
defines a request/response contract of its own.
"""

from __future__ import annotations

from typing import Any

import attr

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.app_id import ROUTER_APP_NAME
from compute_space.core.proxy_target import AsgiApp
from compute_space.core.service_interface.provider import ServiceProvider

# Permission entries stay in wire form (``{"grant": ..., "scope": ...}``) everywhere outside the
# service that issued them: a grant payload is defined by that service, so only it can read one.
Permissions = list[dict[str, Any]]


@attr.s(auto_attribs=True, frozen=True)
class BuiltinService:
    service_url: str
    version: str
    app: AsgiApp


BUILTIN_SERVICES: tuple[BuiltinService, ...] = ()


def builtin_by_url(service_url: str) -> BuiltinService | None:
    """The router's own implementation of a service, if it has one.

    A registry lookup and nothing more — whether it *should* serve a given call is
    ``default_provider_id_for_service``'s question, so the two can't answer it differently.
    """
    return next((s for s in BUILTIN_SERVICES if s.service_url == service_url), None)


def builtin_as_provider(builtin: BuiltinService, is_default: bool) -> ServiceProvider:
    """A builtin as the owner sees it.  Always running — it is us — and served from the root."""
    return ServiceProvider(
        service_url=builtin.service_url,
        app_id=ROUTER_APP_ID,
        app_name=ROUTER_APP_NAME,
        service_version=builtin.version,
        endpoint="/",
        status="running",
        is_default=is_default,
    )
