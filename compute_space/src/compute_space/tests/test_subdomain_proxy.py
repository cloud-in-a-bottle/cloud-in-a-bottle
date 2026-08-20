"""Unit tests for the subdomain proxy middleware's forwarding helpers."""

from typing import Any

from litestar.connection import ASGIConnection
from litestar.datastructures import Headers

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.web.helpers.proxy import _HTTP_REQUEST_EXCLUDED_HEADERS
from compute_space.web.helpers.proxy import _build_forwarded_request_headers
from compute_space.web.helpers.proxy import _sanitize_forwarded_headers
from compute_space.web.middleware.subdomain_proxy import _resolve_forwarded_for


def _connection(client_host: str | None, xff: str | None = None) -> ASGIConnection[Any, Any, Any, Any]:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (client_host, 12345) if client_host is not None else None,
        "server": ("testzone.local", 80),
        "scheme": "http",
    }
    return ASGIConnection(scope)  # type: ignore[arg-type]


def test_loopback_peer_trusts_inbound_xff() -> None:
    """Caddy on loopback set X-Forwarded-For to the real client IP — trust it."""
    conn = _connection("127.0.0.1", xff="203.0.113.7")
    assert _resolve_forwarded_for(conn) == "203.0.113.7"


def test_loopback_peer_without_inbound_falls_back_to_peer() -> None:
    conn = _connection("127.0.0.1")
    assert _resolve_forwarded_for(conn) == "127.0.0.1"


def test_non_loopback_peer_ignores_spoofed_xff() -> None:
    """A container reaching us via the gateway can't spoof the client IP."""
    conn = _connection("10.200.0.5", xff="203.0.113.7")
    assert _resolve_forwarded_for(conn) == "10.200.0.5"


def test_ipv6_loopback_peer_trusts_inbound_xff() -> None:
    conn = _connection("::1", xff="203.0.113.7")
    assert _resolve_forwarded_for(conn) == "203.0.113.7"


def test_no_client_returns_none() -> None:
    conn = _connection(None)
    assert _resolve_forwarded_for(conn) is None


# --- header sanitization (shared by inbound app proxy and service proxy) ---


def _sanitized(headers: list[tuple[str, str]]) -> dict[str, str]:
    return {k.lower(): v for k, v in _sanitize_forwarded_headers(headers)}


def test_sanitize_strips_authorization() -> None:
    """The Authorization credential the router authenticated against must not
    reach the backend app (OH-231)."""
    out = _sanitized([("Authorization", "Bearer owner-api-token"), ("Accept", "*/*")])
    assert "authorization" not in out
    assert out["accept"] == "*/*"


def test_sanitize_strips_authorization_case_insensitively() -> None:
    out = _sanitized([("authorization", "Bearer x"), ("AUTHORIZATION", "Bearer y")])
    assert "authorization" not in out


def test_sanitize_strips_proxy_authorization() -> None:
    out = _sanitized([("Proxy-Authorization", "Basic abc")])
    assert "proxy-authorization" not in out


def test_sanitize_strips_openhost_headers() -> None:
    out = _sanitized([("X-OpenHost-Is-Owner", "true"), ("X-OpenHost-Identity", "spoofed")])
    assert not any(k.startswith("x-openhost-") for k in out)


def test_sanitize_strips_session_cookie_but_keeps_others() -> None:
    out = _sanitized([("Cookie", f"{SESSION_COOKIE_NAME}=secret; keep=1")])
    assert SESSION_COOKIE_NAME not in out.get("cookie", "")
    assert "keep=1" in out["cookie"]


def test_sanitize_preserves_unrelated_headers() -> None:
    out = _sanitized([("X-Custom", "keep"), ("Content-Type", "application/json")])
    assert out["x-custom"] == "keep"
    assert out["content-type"] == "application/json"


def test_build_forwarded_request_headers_drops_authorization() -> None:
    """End-to-end through the builder the router uses for the HTTP proxy path."""
    inbound = Headers({"authorization": "Bearer tok", "x-custom": "v", "host": "app.example.com"})
    built = _build_forwarded_request_headers(
        inbound, _HTTP_REQUEST_EXCLUDED_HEADERS, [("X-OpenHost-Is-Owner", "true")]
    )
    keys = {k.lower() for k, _ in built}
    assert "authorization" not in keys
    assert "x-custom" in keys
    # router-injected identity header still present
    assert ("X-OpenHost-Is-Owner", "true") in built
