"""Cookie provenance for HTTP domains where browsers omit Fetch Metadata."""

from html import escape
from typing import Any
from urllib.parse import quote
from urllib.parse import quote_from_bytes
from urllib.parse import urlsplit

from litestar import Response
from litestar.connection import ASGIConnection
from litestar.enums import MediaType
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template
from litestar.types import ASGIApp

from compute_space.core.domains import Domain
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY

Connection = ASGIConnection[Any, Any, Any, Any]
Origin = tuple[str, str, int]
_FETCH_HEADERS = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user")


class HttpCookieConfirmationRequired(NotAuthorizedException):
    """A valid owner cookie needs an explicit same-origin navigation first."""


def _origin(value: str, *, referer: bool = False) -> Origin | None:
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if "," in parsed.netloc or "\\" in parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None or "#" in value:
            return None
        if not referer and (parsed.path or "?" in value):
            return None
        port = parsed.port
    except ValueError:
        return None
    return (
        parsed.scheme,
        parsed.hostname.lower(),
        port if port is not None else (80 if parsed.scheme == "http" else 443),
    )


def _values(connection: Connection, name: str) -> list[str]:
    return [value for key, value in connection.headers.multi_items() if key.lower() == name]


def _target(connection: Connection) -> Origin | None:
    # Caddy speaks HTTP upstream even for TLS domains. Only server-owned domain
    # configuration can enable the fallback; never trust X-Forwarded-Proto.
    if connection.scope.get("type") != ScopeType.HTTP:
        return None
    zone = connection.scope.get(ZONE_SCOPE_KEY)
    if not isinstance(zone, Domain) or zone.tls:
        return None
    if any(name in connection.headers for name in _FETCH_HEADERS):
        return None
    # A browser may speculatively follow the confirmation link before a click.
    if any(name in connection.headers for name in ("purpose", "sec-purpose", "x-moz")):
        return None
    hosts = _values(connection, "host")
    if len(hosts) != 1 or not hosts[0]:
        return None
    try:
        target = _origin(f"http://{hosts[0]}")
        return target if target is not None and target == _origin(f"http://{connection.url.netloc}") else None
    except ValueError:
        return None


def _provenance(connection: Connection, target: Origin) -> tuple[bool, bool]:
    """Return (same-origin proof, eligible for an inert navigation landing)."""
    origins = _values(connection, "origin")
    referers = _values(connection, "referer")
    if len(origins) > 1 or len(referers) > 1:
        return False, False
    source = _origin(origins[0]) if origins else None
    referring = _origin(referers[0], referer=True) if referers else None
    if origins and source != target:
        return False, False
    if referers and referring is None:
        return False, False
    if source == target:
        return not referers or referring == target, False
    if referring == target:
        return True, False
    # Neither absence nor a foreign Referer proves navigation. A resource-free
    # document can request a deliberate click without executing the target.
    return False, True


def is_same_origin_http_cookie_request(connection: Connection) -> bool:
    target = _target(connection)
    return target is not None and _provenance(connection, target)[0]


def needs_http_cookie_confirmation(connection: Connection) -> bool:
    if connection.scope.get("method") not in ("GET", "HEAD"):
        return False
    accept = connection.headers.get("accept", "")
    if "text/html" not in accept or "application/json" in accept:
        return False
    target = _target(connection)
    return target is not None and _provenance(connection, target)[1]


def http_cookie_confirmation(connection: Connection) -> Response[str]:
    """Return no private content and make no request until the user clicks."""
    raw_path = connection.scope.get("raw_path")
    path = (
        quote_from_bytes(raw_path.split(b"?", 1)[0], safe="/%:@!$&'()*+,;=-._~")
        if raw_path is not None
        else quote(connection.url.path or "/", safe="/:@!$&'()*+,;=-._~")
    )
    query = quote(connection.url.query, safe="!$&'()*+,;=:/?@-._~%")
    destination = f"http://{connection.url.netloc}{path}" + (f"?{query}" if query else "")
    # Keep the link on the exact authority that supplied the landing. A path,
    # query, or forwarded header must never turn it into an external redirect.
    assert _origin(destination, referer=True) == _target(connection)
    body = (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Continue to Cloud in a Bottle</title>
<style>body{font:16px system-ui;max-width:36rem;margin:4rem auto;padding:1rem}
a{display:inline-block;padding:.7rem 1rem;border:1px solid}</style>
<h1>Continue to this page</h1>
<p>Continuing opens the requested URL with your owner access.</p>
<a href="""
        + '"'
        + escape(destination, quote=True)
        + '"'
        + ">Continue</a></html>"
    )
    return Response(
        content="" if connection.scope.get("method") == "HEAD" else body,
        media_type=MediaType.HTML,
        status_code=200,
        headers={
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "same-origin",
            "cache-control": "no-store",
        },
    )


def protect_owner_html[ResponseT: Response[Any] | ASGIApp](response: ResponseT) -> ResponseT:
    """Owner documents may be framed only by their own origin, never apps."""
    if isinstance(response, Response) and (isinstance(response, Template) or response.media_type == MediaType.HTML):
        response.headers.setdefault("content-security-policy", "frame-ancestors 'self'")
        response.headers.setdefault("x-frame-options", "SAMEORIGIN")
        response.headers.setdefault("referrer-policy", "same-origin")
    return response
