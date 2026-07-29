from typing import Any

from litestar.connection import ASGIConnection

from compute_space.config import Domain
from compute_space.config import get_config

# Scope key under which SubdomainProxyMiddleware stashes the Domain a request
# arrived on (a ``Domain`` or ``None``).  Lives here — a leaf helper importing
# only config + litestar — so both the middleware and request handlers can share
# it without an import cycle.
ZONE_SCOPE_KEY = "openhost_zone"


def zone_for_request(connection: ASGIConnection[Any, Any, Any, Any]) -> Domain:
    """The Domain a request arrived on — the single source of truth for
    per-request scheme, link-building, and cookie scoping.

    Prefers the Domain the ``SubdomainProxyMiddleware`` stashed in the scope; if
    it isn't present (e.g. a request that never traversed the middleware), it
    falls back to the primary domain.  Always returns a Domain so callers never
    have to special-case ``None``.
    """
    stashed = connection.scope.get(ZONE_SCOPE_KEY)
    if isinstance(stashed, Domain):
        return stashed
    return get_config().primary_domain
