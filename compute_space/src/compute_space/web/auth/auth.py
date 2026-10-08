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


# The opaque-origin token get_connection_origin returns for a present-but-hostless Origin (notably
# ``Origin: null``).  Never equals a real host[:port], so origin-match checks fail closed on it.
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


# Sec-Fetch-Site values that mean "this request did not come from another origin": the app or the
# router calling itself, or a user-initiated load (typed URL, bookmark, no initiator at all).
_SELF_INITIATED_FETCH_SITES = frozenset({"same-origin", "none"})


def _is_same_origin_http(connection: AnyConnection) -> bool:
    """Judge an HTTP request primarily on Fetch-Metadata, with ``Origin`` as a hard veto.

    ``Sec-Fetch-Site`` is strictly more informative than ``Origin`` for this decision: the browser
    derives both from the true initiator and JS can forge neither (they are forbidden header names),
    but only Sec-Fetch-Site distinguishes ``same-site`` from ``cross-site``.  That distinction is the
    whole game for us, because the session cookie is scoped to the zone (see build_session_cookie), so
    every app subdomain and the router are one *site* with many *origins*.  ``Origin`` also can't help
    on the requests that matter most: it is absent on all GET/HEAD navigations and subresource loads.

    ``same-site`` means another app (or the router UI) initiated this request and the owner's cookie
    rode along.  We allow it only for a genuine top-level document load, keyed on ``Sec-Fetch-Dest:
    document`` *and* an absent Origin.  It must be Dest and not ``Sec-Fetch-Mode: navigate``: loading
    an ``<iframe>`` is also a navigation and reports ``Mode: navigate``, so a Mode-keyed check would
    still admit a hidden ``<iframe src="https://other-app.zone/delete?id=1">`` — silent, repeatable,
    and leaving the attacking page running.  ``Dest: document`` is only ever a top-level browsing
    context, which is user-visible.  The absent-Origin half narrows it to GET/HEAD, since browsers
    send an Origin on every other method; without it a cross-app top-level *form POST* would also
    report ``Dest: document`` and ride straight through.  What is left is the cross-app link we want
    to keep working (e.g. one app linking the user into another to grant a permission), at the cost
    of a loud, visible navigation forgery staying possible.

    A missing Sec-Fetch-Site fails closed.  Every browser that ships Fetch Metadata sends it on every
    HTTP request, so absence means a pre-2023 browser or a non-browser client; falling back to Origin
    there would reopen exactly the Origin-less GET hole this check exists to close.
    """
    origin = get_connection_origin(connection)
    site = connection.headers.get("Sec-Fetch-Site")

    # A concrete Origin naming another host is a hard reject whatever the Fetch-Metadata says: a
    # browser never pairs a mismatched Origin with same-origin metadata, so the combination is a
    # forgery by something that isn't a browser.  This is also what keeps a cross-app *form POST*
    # out of the same-site carve-out below, since every non-GET/HEAD request carries an Origin.
    if origin is not None and origin != _OPAQUE_ORIGIN_TOKEN and origin != connection.base_url.netloc:
        return False

    if site in _SELF_INITIATED_FETCH_SITES:
        return True
    if site == "same-site":
        # Another app initiated this.  Allow only a link the user visibly clicked: a top-level
        # document load with no Origin at all.  Browsers omit Origin exactly on GET/HEAD navigations,
        # so requiring its absence admits the cross-app link we want while excluding every
        # state-changing shape — including an ``Origin: null`` form POST from a no-referrer app.
        return origin is None and connection.headers.get("Sec-Fetch-Dest") == "document"
    return False


def _is_same_origin_websocket(connection: AnyConnection) -> bool:
    """Judge a WebSocket handshake on ``Origin`` alone.  Fetch-Metadata is deliberately unused here.

    Browsers send no ``Sec-Fetch-*`` headers at all on a WebSocket handshake — verified on Chromium
    151 over both ``ws://`` and ``wss://``, while an ordinary subresource from the very same page did
    carry all three.  (Fetch defines a ``websocket`` request mode, but Chromium does not emit the
    headers.)  Keying WS on Sec-Fetch-Site would therefore fail *open* on the dominant engine.

    Browsers do always send a concrete ``Origin`` on a handshake, so an exact match is the check.
    Unlike the HTTP case, ``Origin: null`` is refused outright rather than corroborated: a referrer
    policy does not null a WebSocket's Origin (verified — a ``Referrer-Policy: no-referrer`` page
    still sends its real origin on a handshake), so a null here only ever means a genuinely opaque
    initiator such as a sandboxed iframe.  An absent Origin is refused for the same reason: no browser
    omits it, and server-side callers authenticate with app/API tokens, which never reach this check.
    """
    # An absent Origin (None) and an opaque one (_OPAQUE_ORIGIN_TOKEN) both fall out of this
    # comparison on their own: neither can ever equal a real host[:port].
    return get_connection_origin(connection) == connection.base_url.netloc


def is_same_origin_request(connection: AnyConnection) -> bool:
    """Whether a browser request may carry owner (session-cookie) authority to this target.

    The canonical check, used for owner auth and to guard unauthenticated state-changing endpoints
    (e.g. /logout) against CSRF.  HTTP and WebSocket are judged on different headers because browsers
    populate different headers for them; see the two helpers for why.

    Rejected alternative: gate ``same-site`` on the Origin being the router's own domain rather than
    another app's, which would close cross-app forgery completely.  It is not implementable with the
    headers available — the requests it would have to discriminate are top-level GET navigations,
    which carry no Origin at all, and ``Referer`` is no substitute because any app can suppress it
    with ``Referrer-Policy: no-referrer`` (Miniflux already does).  It would also forbid app-to-app
    links outright, which we want to keep.  Making those links safe wants a capability token on the
    link itself, not a header check.
    """
    if connection.scope["type"] == ScopeType.WEBSOCKET:
        return _is_same_origin_websocket(connection)
    return _is_same_origin_http(connection)


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
        # User (session-cookie) auth is only valid for same-origin requests: we never trust a
        # cross-origin request bearing the owner's cookie, since it could be forged by untrusted app js.
        # See is_same_origin_request for how Origin + Fetch-Metadata decide this (incl. Origin: null).
        if not is_same_origin_request(connection):
            raise NotAuthorizedException(detail="user authentication only valid for router-origin requests")
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
    """Guard for unauthenticated state-changing endpoints (e.g. /logout) that have no owner-auth of
    their own (they must work for any session state) but still need CSRF protection: reject unless the
    request is same-origin with its target.  An ``Origin: null`` is honored only when
    ``Sec-Fetch-Site: same-origin`` corroborates it, so a sandboxed-iframe forced-logout forgery — which
    reports cross-site — stays blocked.  See is_same_origin_request.
    """
    if not is_same_origin_request(connection):
        raise NotAuthorizedException(detail="cross-origin request not allowed")


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


def login_required_redirect(request: Request[Any, Any, Any]) -> Response[Any]:
    """Return a 302 redirecting the user to the login page, with ?next= set to the originally requested URL.

    This should only be called for non-API HTTP requests.
    In general you should just raise a NotAuthorizedException and let litestar call this for you.
    """
    zone = zone_for_request(request)
    return Redirect(path=build_login_url(zone, request.url.netloc, request.url.path, request.url.query))


# Methods a browser re-issues as a plain navigation when it follows a 302. For unsafe methods a
# login redirect is lossy — the browser drops the method/body and re-requests as a bodyless GET —
# so we only send the redirect for these, and give unsafe methods an honest 403.
_LOGIN_REDIRECTABLE_METHODS = frozenset({"GET", "HEAD"})


def is_login_redirectable_method(method: str) -> bool:
    """True iff redirecting an unauthenticated request with this method to /login is non-lossy."""
    return method.upper() in _LOGIN_REDIRECTABLE_METHODS


def auth_required_response(request: Request[Any, Any, Any]) -> Response[Any]:
    """Response for an unauthenticated non-API HTTP request to a protected path.

    GET/HEAD redirect to /login (and back to ``next`` after signing in). Unsafe methods get a 403
    instead, since a login redirect would be re-issued as a bodyless GET and rejected with 405.
    """
    if is_login_redirectable_method(request.method):
        return login_required_redirect(request)
    return Response(content="Authentication required", status_code=403, media_type=MediaType.TEXT)
