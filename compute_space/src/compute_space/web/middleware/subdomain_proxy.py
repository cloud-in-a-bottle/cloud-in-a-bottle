from contextlib import closing
from typing import Any

from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar.connection import ASGIConnection
from litestar.enums import ScopeType
from litestar.exceptions import NotAuthorizedException
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types.asgi_types import HTTPResponseBodyEvent
from litestar.types.asgi_types import HTTPResponseStartEvent
from litestar.types.asgi_types import WebSocketCloseEvent

from compute_space.core.apps import get_app_from_hostname
from compute_space.core.apps import is_public_path
from compute_space.core.containers import ROUTER_INTERNAL_HOSTS
from compute_space.core.domains import Domain
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.auth import login_required_redirect
from compute_space.web.auth.auth import verify_owner_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY

IS_OWNER_HEADER = ("X-OpenHost-Is-Owner", "true")

# Caddy (our front proxy) reaches hypercorn over loopback and, by default,
# strips client-spoofed X-Forwarded-* before forwarding.  So we trust the
# X-Forwarded-For it sets; any other peer (e.g. a container reaching us via the
# 10.200.0.1 gateway) is untrusted and can't dictate the chain.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def _resolve_forwarded_for(connection: ASGIConnection[Any, Any, Any, Any]) -> str | None:
    """The X-Forwarded-For value to pass to the backend app.

    When the peer is the loopback front proxy (Caddy), forward the
    X-Forwarded-For it set — it carries the real client IP.  For any other peer,
    use the peer's own address so an untrusted source can't spoof the chain.
    """
    if connection.client is None:
        return None
    if connection.client.host in _LOOPBACK_HOSTS:
        # the real client IP, as recorded by Caddy. port should not be included.
        if inbound := connection.headers.get("x-forwarded-for"):
            return inbound
    return connection.client.host


async def _send_bad_request(scope: Scope, send: Send) -> None:
    """Best-effort 400/close for malformed requests where Litestar's response
    machinery isn't safe to use (e.g. URL parsing already failed)."""
    try:
        if scope["type"] == ScopeType.HTTP:
            start: HTTPResponseStartEvent = {"type": "http.response.start", "status": 400, "headers": []}
            body: HTTPResponseBodyEvent = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(start)
            await send(body)
        elif scope["type"] == ScopeType.WEBSOCKET:
            await send(WebSocketCloseEvent(type="websocket.close", code=1002, reason="bad request"))
    except Exception:  # noqa: BLE001
        pass


async def _send_internal_error(scope: Scope, send: Send) -> None:
    """Best-effort 500/close for use from the outer ASGI layer where Litestar's
    response machinery may not be safe to invoke (e.g. URL parsing already failed).

    If the response has already started, ``send`` will raise — swallow that so
    we don't compound one error with another."""
    try:
        if scope["type"] == ScopeType.HTTP:
            start: HTTPResponseStartEvent = {"type": "http.response.start", "status": 500, "headers": []}
            body: HTTPResponseBodyEvent = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(start)
            await send(body)
        elif scope["type"] == ScopeType.WEBSOCKET:
            await send(WebSocketCloseEvent(type="websocket.close", code=1011, reason="internal error"))
    except Exception:  # noqa: BLE001
        pass


async def _send_not_found(scope: Scope, receive: Receive, send: Send) -> None:
    """404 (HTTP) / close (WS) — for a host or app-subdomain this router doesn't serve."""
    if scope["type"] == ScopeType.HTTP:
        request: Request[Any, Any, Any] = Request(scope, receive, send)
        response: Response[Any] = Response(content=None, status_code=404)
        await response.to_asgi_response(app=None, request=request)(scope, receive, send)
    else:
        await send(WebSocketCloseEvent(type="websocket.close", code=4404, reason="not found"))


class SubdomainProxyMiddleware:
    """Outer ASGI middleware: intercepts requests on app subdomains and forwards
    them to the app's local container port.  Runs *before* Litestar so app-subdomain
    requests never go through Litestar's routing — which means we don't have a
    resolved route_handler in scope and Litestar's per-handler abstractions
    don't apply.  Non-app requests (router subdomain, etc.) pass through to
    the wrapped Litestar app unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._dispatch(scope, receive, send)
        except Exception:
            # We're the outermost ASGI layer wrapping Litestar; any exception
            # raised here (in middleware logic, in URL/header parsing, or from
            # the proxied request) escapes past Litestar's exception handlers
            # straight up to hypercorn, which would log a raw traceback and
            # drop the connection.  Log it ourselves and reply 5xx cleanly.
            path = scope.get("path", "?") if isinstance(scope, dict) else "?"
            method = scope.get("method", scope.get("type", "?")) if isinstance(scope, dict) else "?"
            logger.opt(exception=True).error("Unhandled exception in proxy middleware: {} {}", method, path)
            await _send_internal_error(scope, send)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in (ScopeType.HTTP, ScopeType.WEBSOCKET):
            # lifespan etc. — pass through.
            await self.app(scope, receive, send)
            return

        # note: we don't need to handle CORS here because cross-origin requests are not allowed (those go thru services which handles its own CORS).
        connection: ASGIConnection[Any, Any, Any, Any] = ASGIConnection(scope, receive, send)

        try:
            netloc = connection.url.netloc
        except ValueError:
            # Malformed Host / request target (e.g. open-proxy scanners sending
            # `CONNECT host:443:443`).  Reply 400 quietly — not worth a traceback.
            await _send_bad_request(scope, send)
            return

        # Resolve which configured Domain this request arrived on.
        with closing(get_db()) as db:
            zone = Domain.match(db, netloc)
            app = get_app_from_hostname(netloc, db) if zone is not None else None
            looks_like_app = zone is not None and app is None and zone.is_app_subdomain(netloc)

        if zone is None:
            if netloc.split(":")[0].lower() in ROUTER_INTERNAL_HOSTS:
                # App→router service-proxy calls arrive here via OPENHOST_ROUTER_URL (the container→host
                # gateway), which isn't a configured domain.  Defer to Litestar; those routes are auth-gated.
                await self.app(scope, receive, send)
                return
            # Unmatched host — not a configured domain or one of its subdomains.  Don't serve it (no
            # fallback to the primary).  Public traffic always arrives via Caddy with the original
            # domain Host, so this only rejects direct-by-IP / unknown-Host requests to the full app.
            await _send_not_found(scope, receive, send)
            return

        # Stash the arriving Domain so downstream handlers (login redirect, cookies, absolute-URL
        # building) stay on it rather than the single canonical domain.
        scope[ZONE_SCOPE_KEY] = zone  # type: ignore[literal-required]

        if app is None:
            if looks_like_app:
                # A subdomain of a configured domain, but no app is deployed there — 404 rather than
                # falling through to the router.
                await _send_not_found(scope, receive, send)
                return
            # The bare configured domain — the router itself; defer to Litestar.
            await self.app(scope, receive, send)
            return

        # TODO: maybe behave differently for apps that are not in running state. not sure

        # Forwarding headers so the app can tell where the request originated.
        # Caddy terminates TLS and speaks plain HTTP to us on loopback, so we
        # can't read the client's real proto or IP off this hop:
        #  - proto: scope["scheme"] is always "http"; derive it from config
        #    instead (the :80->:443 redirect means nothing is proxied in the
        #    clear when TLS is on), matching build_login_url.
        #  - client IP: connection.client is always Caddy; recover the real one
        #    from the X-Forwarded-For Caddy set (see _resolve_forwarded_for).
        # X-Forwarded-Host preserves the original Host so apps that build absolute URLs don't use the proxy's internal hostname.
        # (The Host header itself is only rewritten on the HTTP path below — not
        # here — because the WS client appends rather than replaces it; see there.)
        # Proto follows the domain the request arrived on (https for a TLS domain,
        # http for an mDNS `.local` domain), so an app served on both sees the
        # right scheme per request rather than one global value.  ``zone`` is the
        # matched Domain (unmatched hosts already 404'd above).
        extra_headers = [
            ("X-Forwarded-Host", netloc),
            ("X-Forwarded-Proto", zone.scheme),
        ]
        if forwarded_for := _resolve_forwarded_for(connection):
            extra_headers.append(("X-Forwarded-For", forwarded_for))

        try:
            verify_owner_auth(connection)
            extra_headers.append(IS_OWNER_HEADER)
        except NotAuthorizedException:
            if not is_public_path(app, scope["path"]):
                # We're outer ASGI middleware — a raised NotAuthorizedException
                # wouldn't reach Litestar's exception handlers, so produce the
                # equivalent response ourselves.  HTTP: same /login redirect the
                # exception handler would emit, dispatched via Litestar's
                # Redirect→ASGI machinery.  WS: refuse the handshake.
                if scope["type"] == ScopeType.HTTP:
                    request: Request[Any, Any, Any] = Request(scope, receive, send)
                    response: Response[Any] = login_required_redirect(request)
                    await response.to_asgi_response(None, request=request)(scope, receive, send)
                else:
                    await send(
                        WebSocketCloseEvent(type="websocket.close", code=4401, reason="authentication required")
                    )
                return

        if scope["type"] == ScopeType.HTTP:
            # Rewrite Host to the public hostname (instead of the 127.0.0.1:<port>
            # httpx would synthesize) so apps that only read Host, not
            # X-Forwarded-Host, behave.  HTTP only: httpx lets an explicit Host
            # replace the derived one, but the websockets client sets its own Host
            # from the dial URI and *appends* additional_headers rather than
            # replacing (Headers.__setitem__), so passing Host on the WS path
            # yields two Host headers and the backend 400s the handshake.  WS
            # backends validate Origin, not Host, so X-Forwarded-Host suffices
            # there; a proper WS-Host rewrite needs a different proxy approach.
            proxied = await proxy_http_request(
                Request(scope, receive, send),
                target_port=app.local_port,
                extra_headers=[*extra_headers, ("Host", netloc)],
            )
            await proxied(scope, receive, send)
        else:
            assert scope["type"] == ScopeType.WEBSOCKET
            await proxy_websocket_request(
                WebSocket(scope, receive, send),
                target_port=app.local_port,
                extra_headers=extra_headers,
            )
