from typing import Any

from litestar.connection import ASGIConnection

from compute_space.core.domains import Domain

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
