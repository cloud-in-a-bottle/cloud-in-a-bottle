"""Utilities for proxying HTTP and WebSocket requests to backend apps.

Used in both proxying inbound requests to apps, and proxying requests between apps on the service interface.

This also takes care of stripping openhost/auth relevant headers and cookies from forwarded requests.
"""

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Iterable
from collections.abc import Set
from contextlib import suppress
from typing import Any
from typing import cast

import httpx
import websockets
from litestar import Request
from litestar import WebSocket
from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.exceptions import WebSocketDisconnect
from litestar.response.base import ASGIResponse
from litestar.response.streaming import ASGIStreamingResponse
from litestar.types import Scope
from litestar.types.asgi_types import WebSocketDisconnectEvent
from litestar.types.asgi_types import WebSocketReceiveEvent
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import WebSocketException
from websockets.typing import Subprotocol

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.logging import logger
from compute_space.core.updates import wait_for_shutdown

# auth cookies must never reach a backend app
_STRIPPED_COOKIES = frozenset({SESSION_COOKIE_NAME})

# The router is the sole authority for X-OpenHost-* identity headers.
# Any inbound value would let a client spoof identity to the backend app.
_OPENHOST_HEADER_PREFIX = "x-openhost-"


def _sanitize_forwarded_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Filter inbound headers before forwarding to a backend app.

    Drops X-OpenHost-* headers (the router is their sole authority) and strips
    zone auth cookies from the Cookie header (apps must not see or replay the
    owner's session).  Protocol-level filtering (Host, Connection, etc.) is
    left to each caller.
    """
    cookie_prefixes = tuple(f"{name}=" for name in _STRIPPED_COOKIES)
    sanitized: list[tuple[str, str]] = []
    for key, value in headers:
        lower = key.lower()
        if lower.startswith(_OPENHOST_HEADER_PREFIX):
            continue
        if lower == "cookie":
            value = "; ".join(
                part.strip() for part in value.split(";") if not part.strip().startswith(cookie_prefixes)
            )
            if not value:
                continue
        sanitized.append((key, value))
    return sanitized


def _build_forwarded_request_headers(
    headers: Headers, proto_excluded_headers: Set[str], extra_headers: Iterable[tuple[str, str]]
) -> list[tuple[str, str]]:
    new_headers = _sanitize_forwarded_headers(headers.multi_items())
    new_headers = [(k, v) for k, v in new_headers if k.lower() not in proto_excluded_headers]
    new_headers.extend(extra_headers)
    return new_headers


def _format_proxy_request_url(
    scope: Scope, target_port: int, override_path: str | None = None, target_host: str = "127.0.0.1"
) -> str:
    if override_path:
        path = override_path
    else:
        # Use the raw (percent-encoded) path from the ASGI scope so that
        # URL-encoded characters (e.g. %3A, %40) are preserved exactly as
        # the client sent them.  This is critical for protocols like Matrix
        # federation where the sending server signs the original encoded URI.
        path = scope["raw_path"].decode("ascii")

    path = path.lstrip("/")
    # websockets.connect() rejects http/https URIs; use ws for WS scopes.
    scheme = "ws" if scope["type"] == ScopeType.WEBSOCKET else "http"
    target_url = f"{scheme}://{target_host}:{target_port}/{path}"
    query_string = scope["query_string"]
    if query_string:
        target_url += f"?{query_string.decode('utf-8')}"
    return target_url


_HTTP_REQUEST_EXCLUDED_HEADERS = frozenset(
    {
        # per-hop headers (these get read as we receive the incoming request, and automatically set on the outgoing)
        "host",
        "connection",
        # we choose to decode and (potentially) re-code, vs passing thru potentially compressed body as-is. this is more flexible.
        "transfer-encoding",
        "accept-encoding",
        # stripped before we re-add manually
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
)

_BUFFER_THRESHOLD = 512_000  # 500 KB — buffer small upstream responses to avoid chunked encoding

_HTTP_RESPONSE_EXCLUDED_HEADERS = frozenset(
    {
        # per-hop headers (these get read as we receive the incoming request, and automatically set on the outgoing)
        "content-encoding",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
    }
)


async def proxy_http_request(
    request: Request[Any, Any, Any],
    target_port: int,
    override_path: str | None = None,
    extra_headers: Iterable[tuple[str, str]] = (),
    timeout: float = 30,
    read_timeout: float | None = None,
    target_host: str = "127.0.0.1",
) -> ASGIResponse:
    """Forward an HTTP request to a local port and return the response.

    The request body is read eagerly from the ASGI ``receive`` channel and
    forwarded to the backend as bytes.  Eager reading keeps the ``receive``
    lifecycle clean (fully consumed before we touch the response side) and
    avoids httpx adding a spurious ``Transfer-Encoding: chunked`` header to
    bodyless requests.

    Responses with a known ``Content-Length`` up to ``_BUFFER_THRESHOLD`` are
    buffered into a plain ``ASGIResponse`` (which sets ``Content-Length`` and
    avoids chunked encoding).  Larger or unknown-length responses are streamed
    back via ``ASGIStreamingResponse`` so they don't have to fit in memory.

    ``timeout`` bounds connect/write/pool operations.  ``read_timeout`` bounds
    the wait for bytes from the backend and defaults to ``None`` (unbounded) so
    long-lived responses -- HTTP long-polls and streaming/SSE with long
    inter-chunk gaps -- are not killed by a blanket read deadline.  Callers that
    want to bound how long they'll wait on a backend (e.g. internal service
    calls) can pass an explicit ``read_timeout``.
    """
    target_url = _format_proxy_request_url(request.scope, target_port, override_path, target_host)
    new_request_headers = _build_forwarded_request_headers(
        request.headers, _HTTP_REQUEST_EXCLUDED_HEADERS, extra_headers
    )

    # Read the inbound body eagerly so we fully consume the ASGI receive
    # channel before we start proxying the response.  Streaming the body via
    # an async generator that called ``receive()`` inside httpx's send loop
    # created a race: if the ASGI server delivered an ``http.disconnect``
    # while the generator was suspended (between send and response), the
    # proxy would surface a 500 to the client.  Eager reading also avoids
    # httpx injecting ``Transfer-Encoding: chunked`` on bodyless GETs.
    body_parts: list[bytes] = []
    while True:
        msg = await request.receive()
        if msg["type"] == "http.request":
            if chunk := msg.get("body"):
                body_parts.append(chunk)
            if not msg.get("more_body", False):
                break
        elif msg["type"] == "http.disconnect":
            break
    request_body = b"".join(body_parts)

    # Pass content=None for truly bodyless requests (e.g. GET with no
    # Content-Length / Transfer-Encoding) so httpx omits body framing
    # entirely.  For requests that explicitly declared a body -- even an
    # empty one (Content-Length: 0) -- forward the bytes so the backend
    # sees the same semantics the client intended.
    has_declared_body = "content-length" in request.headers or "transfer-encoding" in request.headers
    content = request_body if has_declared_body else None

    # ``timeout`` bounds connect/write/pool so we fail fast when an app is down.
    # ``read_timeout`` is kept separate and defaults to None (no read deadline):
    # a single blanket read timeout would kill legitimate long-lived responses
    # -- HTTP long-polls (e.g. Matrix /sync) that stay quiet for tens of seconds,
    # and streaming/SSE responses with long inter-chunk gaps -- treating them as
    # a backend hang and returning 504 (or truncating the stream mid-flight).
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=read_timeout),
    )
    try:
        new_request = client.build_request(
            method=str(request.method),
            url=target_url,
            headers=new_request_headers,
            content=content,
        )
        try:
            upstream_response = await client.send(new_request, stream=True)
        except httpx.ConnectError:
            await client.aclose()
            return ASGIResponse(body=b"App is not responding", status_code=502, media_type="text/plain")
        except httpx.TimeoutException:
            await client.aclose()
            return ASGIResponse(body=b"App timed out", status_code=504, media_type="text/plain")
        except httpx.TransportError:
            await client.aclose()
            return ASGIResponse(body=b"App disconnected unexpectedly", status_code=502, media_type="text/plain")

        # we have to pull this out so we can set it on the ASGIResponse before we return it
        media_type = upstream_response.headers.get("Content-Type")

        new_response_headers = [
            (k, v)
            for k, v in upstream_response.headers.multi_items()
            if k.lower() not in _HTTP_RESPONSE_EXCLUDED_HEADERS
        ]

        # Buffer small responses instead of streaming.  Streaming forces
        # chunked transfer-encoding and uses an anyio task group with a
        # disconnect listener that can cancel the stream before the terminus
        # chunk is sent, causing intermittent ChunkedEncodingError.
        upstream_content_length = upstream_response.headers.get("content-length")
        should_buffer = upstream_content_length is not None and int(upstream_content_length) <= _BUFFER_THRESHOLD
        if should_buffer:
            body = await upstream_response.aread()
            await upstream_response.aclose()
            await client.aclose()
            return ASGIResponse(
                body=body,
                status_code=upstream_response.status_code,
                media_type=media_type,
                headers=new_response_headers,
            )

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                # aiter_bytes (not aiter_raw) so httpx decodes any content-encoding the backend applied
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await upstream_response.aclose()
                await client.aclose()

        return ASGIStreamingResponse(
            iterator=stream_body(),
            status_code=upstream_response.status_code,
            media_type=media_type,
            # the typing is wrong here? opened an issue.
            headers=new_response_headers,  # type: ignore
        )
    except BaseException:
        # If we never reached the return-with-iterator path we have to close
        # the client ourselves; otherwise the cleanup happens inside
        # stream_body's finally.
        await client.aclose()
        raise


# Close codes reserved for reporting only (RFC 6455 §7.4.1) — never valid in a close frame we send.
_UNSENDABLE_CLOSE_CODES = frozenset({1005, 1006, 1015})


_WS_REQUEST_EXCLUDED_HEADERS = frozenset(
    {
        # per-hop headers (these get read as we receive the incoming request, and automatically set on the outgoing)
        "host",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        # stripped before we re-add manually
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
)


async def proxy_websocket_request(
    connection: WebSocket[Any, Any, Any],
    target_port: int,
    extra_headers: Iterable[tuple[str, str]] = (),
    override_path: str | None = None,
    target_host: str = "127.0.0.1",
) -> None:
    """Bidirectionally proxy a WebSocket connection to a backend app.

    If ``override_path`` is set, use it instead of the client path.
    """
    target_url = _format_proxy_request_url(connection.scope, target_port, override_path, target_host)

    if subprotocols_str := connection.headers.get("Sec-WebSocket-Protocol"):
        subprotocols = [Subprotocol(s.strip()) for s in subprotocols_str.split(",")]
    else:
        # an empty list is not valid; it yields `Sec-WebSocket-Protocol:` with no value, which some backends reject.
        subprotocols = None

    new_request_headers = _build_forwarded_request_headers(
        connection.headers, _WS_REQUEST_EXCLUDED_HEADERS, extra_headers
    )

    try:
        async with websockets.connect(
            target_url,
            additional_headers=new_request_headers,
            # `max_size=None` lifts the default 1 MiB incoming-message cap: the proxy shouldn't impose its own size policy.
            max_size=None,
            open_timeout=10,
            close_timeout=5,
            subprotocols=subprotocols,
        ) as backend:
            await connection.accept(subprotocols=backend.subprotocol)

            async def backend_to_client() -> None:
                try:
                    async for msg in backend:
                        if isinstance(msg, bytes):
                            await connection.send_bytes(msg)
                        else:
                            await connection.send_text(msg)
                except (ConnectionClosed, WebSocketDisconnect):
                    pass

            async def client_to_backend() -> None:
                try:
                    while True:
                        # connection.receive() returns the raw ASGI message dict (untyped);
                        # narrow it to the only two events Litestar will deliver on a live socket.
                        msg: WebSocketReceiveEvent | WebSocketDisconnectEvent = cast(
                            "WebSocketReceiveEvent | WebSocketDisconnectEvent", await connection.receive()
                        )
                        if msg["type"] == "websocket.disconnect":
                            # Mirror the client's close code to the backend; otherwise the
                            # context-manager close reports a generic 1000.
                            if msg["code"] not in _UNSENDABLE_CLOSE_CODES:
                                with suppress(Exception):
                                    await backend.close(code=msg["code"])
                            return
                        if msg["bytes"] is not None:
                            await backend.send(msg["bytes"])
                        elif msg["text"] is not None:
                            await backend.send(msg["text"])
                except (ConnectionClosed, WebSocketDisconnect):
                    pass

            # Race both directions against the global shutdown event so that
            # long-lived websocket sessions don't hold hypercorn's
            # graceful_timeout open during a restart — without this the proxy
            # would sit on each open connection until either side closes
            # naturally, dragging out shutdown by minutes.
            tasks = [
                asyncio.ensure_future(backend_to_client()),
                asyncio.ensure_future(client_to_backend()),
                asyncio.ensure_future(wait_for_shutdown()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                # Drain any stored exceptions so cancelled tasks don't trigger
                # "Task exception was never retrieved" warnings at GC.
                await asyncio.gather(*tasks, return_exceptions=True)

            # If the backend closed the connection, mirror its close code/reason to the client.
            # Returning with the client socket still open would make the ASGI server close it
            # with a generic 1011, hiding application-level close codes from clients.
            if backend.close_code is not None:
                code = 1011 if backend.close_code in _UNSENDABLE_CLOSE_CODES else backend.close_code
                with suppress(Exception):  # the client may already be gone
                    await connection.close(code=code, reason=backend.close_reason or "")
    except (OSError, TimeoutError, WebSocketException):
        logger.exception(f"WebSocket backend connection failed: {target_url}")
        await connection.close(code=1011)
