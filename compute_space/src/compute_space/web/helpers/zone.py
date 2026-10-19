from typing import Any

from litestar.connection import ASGIConnection

from compute_space.core.domains import Domain
from compute_space.core.domains import host_with_request_port

# Scope key under which SubdomainProxyMiddleware stashes the Domain a request
# arrived on.  Both the middleware and request handlers share it to avoid an
# import cycle.
ZONE_SCOPE_KEY = "openhost_zone"


def zone_for_request(connection: ASGIConnection[Any, Any, Any, Any]) -> Domain:
    """The Domain a request arrived on — the single source of truth for
    per-request scheme, link-building, and cookie scoping.

    ``SubdomainProxyMiddleware`` (required on every request) stashes the matched domain or the DB
    primary in the scope; this returns it.  Raises if absent — a request must not reach a handler
    without traversing the middleware."""
    stashed = connection.scope.get(ZONE_SCOPE_KEY)
    if isinstance(stashed, Domain):
        return stashed
    raise RuntimeError("zone_for_request: no Domain in scope (SubdomainProxyMiddleware is required)")


def router_base_url(connection: ASGIConnection[Any, Any, Any, Any]) -> str:
    """``scheme://host[:port]`` of the router on the domain (and access port) the request arrived on."""
    zone = zone_for_request(connection)
    return f"{zone.scheme}://{host_with_request_port(zone.name_no_port, connection.url.netloc)}"
