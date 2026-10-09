# note: we don't need to handle CORS in the main auth path because cross-origin requests are not allowed.
# the only allowed cross-origin requests go thru the services interface which handles its own CORS.
import sqlite3
from contextlib import closing
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.enums import MediaType
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler
from litestar.response import Redirect

from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAPIKey
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import AuthenticatedApp
from compute_space.core.auth.auth import AuthenticatedUser
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.auth import validate_app_token
from compute_space.core.auth.auth import validate_session_token
from compute_space.core.domains import Domain
from compute_space.core.domains import host_with_request_port
from compute_space.db import get_db
from compute_space.web.helpers.zone import zone_for_request

AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _get_bearer_token_if_set(connection: AnyConnection) -> str | None:
    if auth_header := connection.headers.get("Authorization", ""):
        if auth_header.lower().startswith("bearer "):
            if token := auth_header[7:].strip():
                return token
    return None


# what get_connection_origin returns for a present but hostless Origin, e.g. ``Origin: null``.
_OPAQUE_ORIGIN_TOKEN = "null"


def get_connection_origin(connection: AnyConnection) -> str | None:
    """gets and formats the origin header as "sub.example.com" or "sub.example.com:1234", no protocol or path, if set.
    port is included if non-default.
    returns None only if no Origin header is set at all.

    A *present* Origin that has no parseable host (notably ``Origin: null``, which browsers send from
    sandboxed/opaque contexts like ``<iframe sandbox>``) returns the literal token (e.g. ``"null"``)
    rather than None, so callers can't mistake a present cross-origin request for an absent header and
    wave it through. Such a token never equals a real ``host[:port]``, so origin-match checks fail
    closed.

    browsers have specific behaviors around the Origin header, which we rely on here.
    - it is set on all cross-origin requests, including from subdomains.
    - it is not set on all same-origin requests though
    - it includes the port if non-default (not 80 or 443).

    we don't use the Referer header, as it's not intended for use in CORS type origin validation.

    note sec-fetch-site is potentially preferred to Origin for some uses.
    """
    raw = connection.headers.get("Origin")
    if raw is None:
        return None
    parsed = urlparse(raw)
    host, port = parsed.hostname, parsed.port
    if not host:
        # present but opaque/unparseable (e.g. "null"): return a non-matching token, not None.
        return raw.strip().lower() or _OPAQUE_ORIGIN_TOKEN
    return f"{host}:{port}" if port else host


def _is_safe_for_cookie_auth_http(connection: Request[Any, Any, Any]) -> bool:
    """HTTP path for `is_safe_for_cookie_auth`.

    Just using `Origin` is insufficient; Origin isn't set on some valid same-origin requests.
    Instead we use Sec-Fetch-Site:
    - `same-origin` (exact same host, ie same-app)
    - `same-site` (another app)
    - `cross-site` (another site)
    - `none` (typing URL in address bar, using bookmark, reloading the page.)

    We want to allow links to be clicked from elsewhere in the instance (dashboard, other apps).
    So we allow `same-site` GET/HEADs if Sec-Fetch-Dest == "document", which is only set on full-page
    navigations.
    """
    site = connection.headers.get("Sec-Fetch-Site")
    if site in {"same-origin", "none"}:
        return True
    if (
        site == "same-site"
        and connection.scope["method"] in ("GET", "HEAD")
        and connection.headers.get("Sec-Fetch-Dest") == "document"
    ):
        return True
    return False


def _is_safe_for_cookie_auth_websocket(connection: AnyConnection) -> bool:
    """Websocket-path for `is_safe_for_cookie_auth`

    Browsers always send a concrete Origin, so we just check it directly.
    """
    return get_connection_origin(connection) == connection.base_url.netloc


def is_safe_for_cookie_auth(connection: AnyConnection) -> bool:
    """Whether the request is safe to allow cookie auth on.

    A malicious app could make a request from the owner's browser to another app on their instance,
    and by default their browser would send the user's instance auth cookies with this request,
    because both the origin and target are on the same site.

    We want to prevent such cross-app requests in general, with some narrow exceptions:
    - a full-page navigation/redirect is allowed - one app show a link to eg approve a permission in another app,
    and the user should be able to click this.
    """
    if connection.scope["type"] == ScopeType.WEBSOCKET:
        return _is_safe_for_cookie_auth_websocket(connection)
    if connection.scope["type"] == ScopeType.HTTP:
        return _is_safe_for_cookie_auth_http(
            Request(connection.scope, receive=connection.receive, send=connection.send)
        )
    return False


def authenticate(connection: AnyConnection, db: sqlite3.Connection) -> AuthenticatedAccessor | None:
    """Resolve who is making this request, by trying each auth scheme in priority order.

    TODO: we should probs have some rate-limiting or other abuse mitigation here.
    """

    # session token in cookie
    if session_token := connection.cookies.get(SESSION_COOKIE_NAME):
        if authenticated_user := validate_session_token(session_token, db):
            return authenticated_user

    # api and app tokens are both set in Authorization: Bearer header
    if token := _get_bearer_token_if_set(connection):
        # api token
        if authenticated_api_token := validate_api_token(token, db):
            return authenticated_api_token

        # app token
        if authenticated_app := validate_app_token(token, db):
            return authenticated_app

    return None


def verify_owner_auth(connection: AnyConnection) -> None:
    """Verify that the request is authenticated as an "owner" (either a user or an API key, with valid Origin).

    returns if authed; raises NotAuthorizedException if not authenticated.
    """
    accessor = authenticate(connection, db=get_db())

    if isinstance(accessor, AuthenticatedUser):
        if not is_safe_for_cookie_auth(connection):
            raise NotAuthorizedException(detail="cookie authentication is only valid for safe-origin requests")
        return
    if isinstance(accessor, AuthenticatedAPIKey):
        # API key requests won't come from untrusted JS, so can be trusted regardless of origin.
        return
    raise NotAuthorizedException(detail="User or API key authentication required")


def verify_app_auth(connection: AnyConnection) -> str:
    """Verify that the request is authenticated as an "app" (either client-side, from app js, or server-side, from an app token).

    returns `app_id` if authed; raises NotAuthorizedException if not authenticated.
    """
    with closing(get_db()) as db:
        accessor = authenticate(connection, db=db)
        origin = get_connection_origin(connection)

        if isinstance(accessor, AuthenticatedUser):
            if origin is not None and (app := get_app_from_hostname(origin, db)) is not None:
                # requests from app js come from the user's browser with the user's auth.
                # Origin will always be set by the browser on these cross-origin requests.
                return app.app_id
        if isinstance(accessor, AuthenticatedApp):
            # server-side app requests.
            return accessor.app_id
    raise NotAuthorizedException(detail="app authentication required")


def require_owner_auth(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_owner_auth to be used as a route guard."""
    verify_owner_auth(connection)


async def verify_owner_ws(socket: WebSocket[Any, Any, Any]) -> bool:
    """Owner-auth a WebSocket; on success return True and leave accepting to the caller.

    Guards can't be used here: they signal failure by raising, which on a WS rejects the
    handshake before the client can read why. So on failure we accept and send a 4401 close
    frame the browser's ``onclose`` can see, then return False for the caller to bail on.
    """
    try:
        verify_owner_auth(socket)
        return True
    except NotAuthorizedException:
        await socket.accept()
        await socket.close(code=4401, reason="Missing or invalid authorization")
        return False


def require_app_auth(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_app_auth to be used as a route guard."""
    verify_app_auth(connection)


def require_owner_or_app_auth(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Guard that passes if the caller is either an owner (user/API key) or an app."""
    try:
        verify_owner_auth(connection)
        return
    except NotAuthorizedException:
        pass
    verify_app_auth(connection)


def build_login_url(zone: Domain, netloc: str, path: str, query: str) -> str:
    """Build an absolute ``/login?next=<original>`` URL on ``zone`` — the domain the
    request arrived on.

    Redirecting to the arriving domain (rather than always the canonical one) is what
    lets login happen on ``myhost.local`` when the user came in on ``myhost.local`` and
    on the public domain when they came in there — no forced bounce to a single domain.

    Caller passes URL parts so this works from either a Litestar ``Request`` or a raw ASGI
    scope.  The redirect target is absolute so it works from an app-subdomain request — a
    relative ``/login`` would otherwise resolve against the app's host, not the router's.
    """
    proto = zone.scheme
    # `request.url` always reports HTTP because Caddy terminated TLS before forwarding to
    # hypercorn — rebuild with the arriving domain's scheme.
    next_url = f"{proto}://{netloc}{path}"
    if query:
        next_url = f"{next_url}?{query}"
    return f"{proto}://{host_with_request_port(zone.name_no_port, netloc)}/login?next={quote(next_url, safe='')}"


def auth_required_response(request: Request[Any, Any, Any]) -> Response[Any]:
    """Response for an unauthenticated non-API HTTP request to a protected path.

    In general you should just raise a NotAuthorizedException and let litestar call this for you.

    GET/HEAD redirect to /login with ?next= set to the requested URL. Other methods get a 401.
    """
    if request.method not in ("GET", "HEAD"):
        return Response(content="Authentication required", status_code=401, media_type=MediaType.TEXT)
    zone = zone_for_request(request)
    return Redirect(path=build_login_url(zone, request.url.netloc, request.url.path, request.url.query))
