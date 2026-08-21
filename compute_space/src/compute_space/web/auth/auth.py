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
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.helpers.zone import zone_for_request

AnyConnection = ASGIConnection[Any, Any, Any, Any]


def _get_bearer_token_if_set(connection: AnyConnection) -> str | None:
    if auth_header := connection.headers.get("Authorization", ""):
        if auth_header.lower().startswith("bearer "):
            if token := auth_header[7:].strip():
                return token
    return None


def bearer_is_openhost_credential(connection: AnyConnection) -> bool:
    """True iff the Authorization bearer is a valid OpenHost API token or app token.

    Such a token is a credential the router consumes, so it must be stripped before forwarding — an
    owner API token grants full API access and could be replayed by the app. A bearer the router
    doesn't recognise (e.g. a JWT an app issued to its own SPA) is the app's own and is left alone.

    Fails safe: any DB error answers "not ours" (don't strip) rather than 500-ing the proxied
    request. The check only touches the DB when a bearer is actually present.
    """
    token = _get_bearer_token_if_set(connection)
    if not token:
        return False
    try:
        with closing(get_db()) as db:
            return validate_api_token(token, db) is not None or validate_app_token(token, db) is not None
    except Exception:
        logger.exception("bearer_is_openhost_credential: token check failed; not stripping")
        return False


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
    """
    raw = connection.headers.get("Origin")
    if raw is None:
        return None
    parsed = urlparse(raw)
    host, port = parsed.hostname, parsed.port
    if not host:
        # present but opaque/unparseable (e.g. "null"): return a non-matching token, not None.
        return raw.strip().lower() or "null"
    return f"{host}:{port}" if port else host


def verify_same_origin(connection: AnyConnection) -> None:
    """Reject cross-origin requests to unauthenticated state-changing endpoints (e.g. /logout).

    The ``Origin`` header is set by browsers on all cross-origin requests (including from subdomains
    and from sandboxed/opaque contexts, which send ``Origin: null``) and cannot be forged by js. So
    if an Origin header is present at all, it must match the target host; otherwise the request is
    cross-site and is rejected. This stops a hostile page (including a sandboxed iframe sending
    ``Origin: null``) from cross-site POSTing to endpoints like /logout, which has no owner-auth
    guard of its own (it must work for any session state).

    A genuinely same-origin top-level form post either omits Origin or sends the matching host, so
    legitimate logout still works.

    raises NotAuthorizedException on a cross-origin request.
    """
    origin = get_connection_origin(connection)
    if origin is not None and origin != connection.base_url.netloc:
        raise NotAuthorizedException(detail="cross-origin request not allowed")


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
    origin = get_connection_origin(connection)

    if isinstance(accessor, AuthenticatedUser):
        if origin is not None and origin != connection.base_url.netloc:
            # if origin is set (it is set on all browser cross-origin requests and cannot be forged by js),
            # it must match the target URL. either router-to-router or same-app-origin is fine.
            # in theory router->app is also fine but idk if this happens in practice.
            # we never allow cross-origin requests with user auth, even from other subdomains, as these could be forged by untrusted app js.
            raise NotAuthorizedException(detail="user authentication only valid for router-origin requests")
        # origin is not set on normal same-origin GETs, for example, so we allow these.
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


def require_same_origin(connection: AnyConnection, _route_handler: BaseRouteHandler) -> None:
    """Adapt verify_same_origin to be used as a route guard (for unauthenticated state-changing
    endpoints like /logout that still need cross-origin/CSRF protection)."""
    verify_same_origin(connection)


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
    return f"{proto}://{zone.name}/login?next={quote(next_url, safe='')}"


def login_required_redirect(request: Request[Any, Any, Any]) -> Response[Any]:
    """Return a 302 redirecting the user to the login page, with ?next= set to the originally requested URL.

    This should only be called for non-API HTTP requests.
    In general you should just raise a NotAuthorizedException and let litestar call this for you.
    """
    zone = zone_for_request(request)
    return Redirect(path=build_login_url(zone, request.url.netloc, request.url.path, request.url.query))
