"""Call a service from inside the router, wherever that service happens to run.

Router-side code — cert acquisition, dynamic DNS, anything later — should not care whether a
service is provided by an app or by the router itself.  So a builtin is mounted on an httpx
transport that answers without a socket, and every caller gets the same ``httpx``-shaped client
either way.  One code path, and the in-process provider is held to the same wire contract as a
real one, so the two cannot drift.

Calling ourselves over actual loopback would not work regardless: the router acquires its first
TLS cert before hypercorn is listening (see ``web.start``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import attr
import httpx
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space.config import Config
from compute_space.core.builtin_services import BuiltinService
from compute_space.core.builtin_services import builtin_for
from compute_space.core.dns.service_api import Grant
from compute_space.core.dns.service_api import parse_grants
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


@attr.s(auto_attribs=True, frozen=True)
class _BuiltinTransport(httpx.BaseTransport):
    """Answers requests from a builtin handler, without a socket."""

    service: BuiltinService
    config: Config
    db: sqlite3.Connection

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        grants = parse_grants(json.loads(request.headers.get(PERMISSIONS_HEADER) or "[]"))
        payload = json.loads(request.content or b"{}")
        status, body = self.service.handler(request.url.path, payload, grants, self.config, self.db)
        return httpx.Response(status, json=body)


@attr.s(auto_attribs=True, frozen=True)
class ServiceEndpoint:
    """A service, ready to call, with no indication of where it runs."""

    http: httpx.Client
    base_url: str
    is_builtin: bool

    def call(self, path: str, payload: dict[str, Any], grants: list[Grant]) -> dict[str, Any]:
        """POST to the service and return its JSON body, raising on anything unusable.

        The router has no app token, but it is the sole authority for the ``X-OpenHost-*`` identity
        headers in the first place, so it asserts the same ones the proxy would have injected for a
        consumer app.
        """
        url = self.base_url + path
        try:
            response = self.http.post(
                url,
                json=payload,
                headers={
                    "X-OpenHost-Consumer-Id": ROUTER_CONSUMER_ID,
                    "X-OpenHost-Consumer-Name": ROUTER_CONSUMER_NAME,
                    PERMISSIONS_HEADER: json.dumps([g.as_permission() for g in grants]),
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
                f"{path} failed ({response.status_code}): "
                f"{body.get('error', 'unknown_error')} {body.get('message', '')}",
                status=response.status_code,
                body=body,
            )
        return body


@contextmanager
def service_client(
    service_url: str, config: Config, db: sqlite3.Connection, version: str = ">=0"
) -> Iterator[ServiceEndpoint]:
    """Open a client for ``service_url``, whichever provider currently serves it."""
    builtin = builtin_for(service_url, db)
    if builtin is not None:
        if Version(builtin.version) not in SpecifierSet(version):
            raise ServiceCallError(f"{service_url} version {builtin.version} does not match {version}")
        transport: httpx.BaseTransport | None = _BuiltinTransport(builtin, config, db)
        base_url = _BUILTIN_HOST
    else:
        try:
            _, port, _, endpoint = resolve_provider(service_url, version, db)
        except RuntimeError as e:
            raise ServiceCallError(f"no usable provider for {service_url}: {e}") from e
        transport, base_url = None, f"http://127.0.0.1:{port}/{endpoint.strip('/')}"

    with httpx.Client(transport=transport, timeout=_REQUEST_TIMEOUT_SECONDS) as http:
        yield ServiceEndpoint(http=http, base_url=base_url, is_builtin=builtin is not None)
