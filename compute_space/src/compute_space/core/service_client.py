"""Call a service from inside the router, wherever that service happens to run.

Router-side code — cert acquisition, dynamic DNS, anything later — should not care whether a
service is provided by an app or by the router itself.  A builtin is an ASGI app, so httpx's
ASGITransport serves it without a socket and every call takes the same path either way.  The
in-process provider is held to the same wire contract as a real one, so the two cannot drift.

``call_service`` is a plain function holding nothing: the provider is resolved and the HTTP client
built per call.  DNS calls are rare enough that losing connection reuse costs nothing, and it means
no handle to open, close, or thread through a call stack.

Calling ourselves over actual loopback would not work regardless: the router acquires its first
TLS cert before hypercorn is listening (see ``web.start``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any
from typing import cast

import httpx
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space.config import Config
from compute_space.core.builtin_services import BuiltinService
from compute_space.core.builtin_services import Permissions
from compute_space.core.builtin_services import builtin_for
from compute_space.core.services_v2 import resolve_provider

# The router's own consumer identity.  App names are DNS-label-like (see core.app_id), so the
# leading underscore cannot collide with a real one.
ROUTER_CONSUMER_ID = "_openhost_router"
ROUTER_CONSUMER_NAME = "OpenHost Router"

PERMISSIONS_HEADER = "X-OpenHost-Permissions"
_REQUEST_TIMEOUT_SECONDS = 60.0

# Host for in-process calls.  Never resolved — the transport answers before any lookup — but httpx
# needs a valid absolute URL.
_BUILTIN_HOST = "http://builtin.openhost.internal"


class ServiceCallError(RuntimeError):
    """A service could not be reached, or answered with something unusable."""

    def __init__(self, message: str, status: int | None = None, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


def _client_for(
    service_url: str, config: Config, db: sqlite3.Connection, version: str
) -> tuple[httpx.AsyncClient, str]:
    """An httpx client and base URL for whichever provider currently serves ``service_url``."""
    builtin = builtin_for(service_url, db)
    if builtin is not None:
        if Version(builtin.version) not in SpecifierSet(version):
            raise ServiceCallError(f"{service_url} version {builtin.version} does not match {version}")
        # cast: litestar types its ASGIApp with its own scope classes, httpx with the raw
        # MutableMappings; they are the same protocol.
        transport: httpx.AsyncBaseTransport | None = httpx.ASGITransport(app=cast(Any, builtin.app))
        base_url = _BUILTIN_HOST
    else:
        try:
            _, port, _, endpoint = resolve_provider(service_url, version, db)
        except RuntimeError as e:
            raise ServiceCallError(f"no usable provider for {service_url}: {e}") from e
        transport, base_url = None, f"http://127.0.0.1:{port}/{endpoint.strip('/')}"
    return httpx.AsyncClient(transport=transport, timeout=_REQUEST_TIMEOUT_SECONDS), base_url


async def acall_service(
    service_url: str,
    path: str,
    payload: dict[str, Any],
    permissions: Permissions,
    config: Config,
    db: sqlite3.Connection,
    version: str = ">=0",
) -> dict[str, Any]:
    """POST to a service and return its JSON body, raising on anything unusable.

    The router has no app token, but it is the sole authority for the ``X-OpenHost-*`` identity
    headers in the first place, so it asserts the same ones the proxy would have injected for a
    consumer app.
    """
    http, base_url = _client_for(service_url, config, db, version)
    url = base_url + path
    async with http:
        try:
            response = await http.post(
                url,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": ROUTER_CONSUMER_ID,
                    "X-OpenHost-Consumer-Name": ROUTER_CONSUMER_NAME,
                    PERMISSIONS_HEADER: json.dumps(permissions),
                },
            )
            body = response.json()
        except httpx.HTTPError as e:
            raise ServiceCallError(f"service unreachable at {url}: {e}") from e
        except ValueError as e:
            raise ServiceCallError(f"service returned non-JSON ({response.status_code})") from e

    if not isinstance(body, dict):
        raise ServiceCallError(f"service returned unexpected body: {body!r}")
    if not 200 <= response.status_code < 300:
        raise ServiceCallError(
            f"{path} failed ({response.status_code}): {body.get('error', 'unknown_error')} {body.get('message', '')}",
            status=response.status_code,
            body=body,
        )
    return body


def call_service(
    service_url: str,
    path: str,
    payload: dict[str, Any],
    permissions: Permissions,
    config: Config,
    db: sqlite3.Connection,
    version: str = ">=0",
) -> dict[str, Any]:
    """Blocking form, for the router's sync paths — cert acquisition and the dynamic-DNS watcher,
    which run in threads with no event loop of their own.

    ASGI needs a loop, so one is spun up per call.  Callers already inside a loop must await
    ``acall_service`` instead; doing otherwise would deadlock, so it fails loudly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(acall_service(service_url, path, payload, permissions, config, db, version))
    raise RuntimeError(f"call_service({service_url}) called from a running event loop; await acall_service instead")


def builtin_client(service: BuiltinService) -> tuple[httpx.AsyncClient, str]:
    """A client that serves ``service`` in-process, for callers that need the raw response — the
    proxy passes a 403 through after decorating it, rather than treating it as a failure."""
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=cast(Any, service.app))),
        _BUILTIN_HOST,
    )


def provider_is_builtin(service_url: str, db: sqlite3.Connection) -> bool:
    """Whether the router serves this itself — which callers occasionally need, e.g. to know how
    slow a write is to become visible."""
    return builtin_for(service_url, db) is not None
