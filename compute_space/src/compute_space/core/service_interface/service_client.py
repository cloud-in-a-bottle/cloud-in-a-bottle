"""Call a service from inside the router, wherever that service happens to run.

Router-side code — cert acquisition, dynamic DNS, anything later — should not care whether a
service is provided by an app or by the router itself.  ``resolve_provider`` answers that, and the
two kinds of provider differ only in the transport, so every call takes the same path either way.

``call_service`` is a plain async function holding nothing: the provider is resolved and the HTTP
client built per call.  Calls are rare enough that losing connection reuse costs nothing, and it
means no handle to open, close, or thread through a call stack.

Async because a call crosses a process boundary or a registrar's API and will not return
immediately.  ``asyncio.run`` belongs at entry points — ``web.start``, thread launchers — not in
here.

Calling ourselves over actual loopback would not work regardless: the router acquires its first
TLS cert before hypercorn is listening (see ``web.start``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from compute_space.core.proxy_target import client_for
from compute_space.core.service_interface.builtin_services import Permissions
from compute_space.core.service_interface.headers import router_consumer_headers
from compute_space.core.service_interface.provider import ProviderUnavailable
from compute_space.core.service_interface.resolve import resolve_provider

_REQUEST_TIMEOUT_SECONDS = 60.0


class ServiceCallError(RuntimeError):
    """A service could not be reached, or answered with something unusable."""

    def __init__(self, message: str, status: int | None = None, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


async def call_service(
    service_url: str,
    path: str,
    payload: dict[str, Any],
    permissions: Permissions,
    db: sqlite3.Connection,
    version: str = ">=0",
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST to a service and return its JSON body, raising on anything unusable."""
    try:
        provider = resolve_provider(service_url, version, db)
    except ProviderUnavailable as e:
        raise ServiceCallError(f"no usable provider for {service_url}: {e}") from e

    http, base_url = client_for(provider.target, timeout)
    url = _service_url(base_url, provider.endpoint, path)
    async with http:
        try:
            response = await http.post(url, json=payload, headers=dict(router_consumer_headers(permissions)))
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


def _service_url(base_url: str, endpoint: str, path: str) -> str:
    """Fold the provider's endpoint prefix and the caller's path onto the base URL."""
    prefix = endpoint.strip("/")
    return f"{base_url}/{prefix}{path}" if prefix else f"{base_url}{path}"
