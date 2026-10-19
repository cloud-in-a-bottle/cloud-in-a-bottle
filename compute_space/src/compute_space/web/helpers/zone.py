from contextlib import closing
from typing import Any

from litestar.connection import ASGIConnection

from compute_space.core.domain_root import load_domain_root_setting
from compute_space.core.domains import Domain
from compute_space.core.domains import host_with_request_port
from compute_space.db import get_db

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
    """``scheme://host[:port]`` of the router on the domain (and access port) the request arrived on: the bare
    domain, or the subdomain the owner moved the dashboard to (see ``core.domain_root``)."""
    zone = zone_for_request(connection)
    with closing(get_db()) as db:
        router_host = load_domain_root_setting(db).router_host(zone)
    return f"{zone.scheme}://{host_with_request_port(router_host, connection.url.netloc)}"
