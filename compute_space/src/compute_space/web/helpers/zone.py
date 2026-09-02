from contextvars import ContextVar

import attr

from compute_space.core.domains import Domain

# Lives in core (next to ``Domain``) so link builders in the core layer — notably
# ``approve_grant_url`` — can carry the request's access port without importing web.
# Re-exported here because ``RequestOrigin`` (and web-layer callers/tests) reach for it.
from compute_space.core.domains import host_with_request_port

__all__ = [
    "RequestOrigin",
    "host_with_request_port",
    "request_origin",
    "require_request_origin",
    "set_request_origin",
    "zone_for_request",
]


@attr.s(auto_attribs=True, frozen=True)
class RequestOrigin:
    """The domain and authority a request arrived on — the single per-request source
    for building absolute links back onto the same domain and access port.

    ``SubdomainProxyMiddleware`` records one of these per request (see
    ``set_request_origin``); everything that builds an absolute URL on the arriving
    domain reads it via ``request_origin()``.  It lives in a ``ContextVar`` rather
    than the request scope because link builders also run *outside* the Jinja request
    context — notably ``app_url()`` called inside an *imported* macro (``app_row``,
    ``nav_menu``), which Jinja renders without ``request`` — and those still need the
    arriving domain and access port.
    """

    zone: Domain
    # host:port the request arrived on, e.g. "lvh.me:8088" or "app.lvh.me:8088".
    netloc: str

    @property
    def scheme(self) -> str:
        return self.zone.scheme

    @property
    def host(self) -> str:
        """The zone's bare host carrying the request's access port (e.g. ``lvh.me:8088``)."""
        return host_with_request_port(self.zone.name_no_port, self.netloc)

    def subdomain_host(self, label: str) -> str:
        """An ``<label>.<zone>`` host carrying the request's access port."""
        return host_with_request_port(f"{label}.{self.zone.name_no_port}", self.netloc)


# The origin the current request arrived on, or None if none was recorded (no request
# in flight, or a request that never matched a configured domain).  Set once per
# request by SubdomainProxyMiddleware.
_request_origin: ContextVar[RequestOrigin | None] = ContextVar("openhost_request_origin", default=None)


def set_request_origin(origin: RequestOrigin | None) -> None:
    """Record the origin the current request arrived on (called by the middleware)."""
    _request_origin.set(origin)


def request_origin() -> RequestOrigin | None:
    """The origin the current request arrived on, or None if none was recorded."""
    return _request_origin.get()


def require_request_origin() -> RequestOrigin:
    """The origin the current request arrived on.

    Raises if absent — ``SubdomainProxyMiddleware`` records one on every request that
    reaches a handler, so a missing origin means the middleware was bypassed (a bug),
    not a case to paper over."""
    origin = _request_origin.get()
    if origin is None:
        raise RuntimeError("require_request_origin: no origin recorded (SubdomainProxyMiddleware is required)")
    return origin


def zone_for_request() -> Domain:
    """The Domain the current request arrived on — the single source of truth for
    per-request scheme, link-building, and cookie scoping.  Raises if the middleware
    was bypassed (see ``require_request_origin``)."""
    return require_request_origin().zone
