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

from compute_space.config import get_config
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.apps import is_public_path
from compute_space.core.logging import logger
from compute_space.web.auth.auth import login_required_redirect
from compute_space.web.auth.auth import verify_owner_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request

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


def _looks_like_app_subdomain(netloc: str) -> bool:
    """True iff ``netloc`` looks like ``<something>.<zone_domain>`` (i.e. the
    request hit what looks like an app subdomain of the configured zone).
    The router itself answers on ``zone_domain`` exactly, not on a subdomain.
    """
    host = netloc.split(":", 1)[0]
    zone = get_config().zone_domain
    return bool(zone) and host.endswith("." + zone)


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

        app = get_app_from_hostname(netloc)
        if not app:
            if _looks_like_app_subdomain(netloc):
                # The hostname looks like an app subdomain but no app is deployed
                # there — return 404 instead of falling through to the router
                if scope["type"] == ScopeType.HTTP:
                    request: Request[Any, Any, Any] = Request(scope, receive, send)
                    response: Response[Any] = Response(content=None, status_code=404)
                    await response.to_asgi_response(app=None, request=request)(scope, receive, send)
                else:
                    await send(WebSocketCloseEvent(type="websocket.close", code=4404, reason="no such app"))
                return
            # Router subdomain (or unrelated host) — defer to Litestar.
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
        extra_headers = [
            ("X-Forwarded-Host", netloc),
            ("X-Forwarded-Proto", "https" if get_config().tls_enabled else "http"),
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
                    request = Request(scope, receive, send)
                    response = login_required_redirect(request)
                    await response.to_asgi_response(app=None, request=request)(scope, receive, send)
                else:
                    await send(
                        WebSocketCloseEvent(type="websocket.close", code=4401, reason="authentication required")
                    )
                return

        if scope["type"] == ScopeType.HTTP:
            proxied = await proxy_http_request(
                Request(scope, receive, send),
                target_port=app.proxy_port,
                extra_headers=extra_headers,
                target_host=app.proxy_host,
            )
            await proxied(scope, receive, send)
        else:
            assert scope["type"] == ScopeType.WEBSOCKET
            await proxy_websocket_request(
                WebSocket(scope, receive, send),
                target_port=app.proxy_port,
                extra_headers=extra_headers,
                target_host=app.proxy_host,
            )
