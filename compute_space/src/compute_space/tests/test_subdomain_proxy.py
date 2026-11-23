"""Unit tests for the subdomain proxy middleware's forwarding helpers."""

from typing import Any

from litestar.connection import ASGIConnection

from compute_space.web.middleware.subdomain_proxy import resolve_forwarded_for


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
    assert resolve_forwarded_for(conn) == "203.0.113.7"


def test_loopback_peer_without_inbound_falls_back_to_peer() -> None:
    conn = _connection("127.0.0.1")
    assert resolve_forwarded_for(conn) == "127.0.0.1"


def test_non_loopback_peer_ignores_spoofed_xff() -> None:
    """A container reaching us via the gateway can't spoof the client IP."""
    conn = _connection("10.200.0.5", xff="203.0.113.7")
    assert resolve_forwarded_for(conn) == "10.200.0.5"


def test_ipv6_loopback_peer_trusts_inbound_xff() -> None:
    conn = _connection("::1", xff="203.0.113.7")
    assert resolve_forwarded_for(conn) == "203.0.113.7"


def test_no_client_returns_none() -> None:
    conn = _connection(None)
    assert resolve_forwarded_for(conn) is None
