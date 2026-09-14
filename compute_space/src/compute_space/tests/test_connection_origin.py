"""Unit tests for get_connection_origin's Origin-header parsing.

This helper underpins the codebase's CSRF-equivalent origin checks
(verify_owner_auth / verify_app_auth / is_same_origin_request). The security-load-bearing
property is that a *present* Origin header is never collapsed to None ("no header"):
an opaque/unparseable Origin such as ``null`` (sent by sandboxed iframes) must return a
non-None token so origin-match checks fail closed rather than waving the request through.
"""

from __future__ import annotations

from typing import Any

from compute_space.web.auth.auth import get_connection_origin


class _FakeHeaders:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, name: str, default: Any = None) -> Any:
        if name.lower() == "origin":
            return self._value if self._value is not None else default
        return default


class _FakeConnection:
    def __init__(self, origin: str | None) -> None:
        self.headers = _FakeHeaders(origin)


def test_absent_origin_returns_none() -> None:
    assert get_connection_origin(_FakeConnection(None)) is None


def test_simple_origin_returns_host() -> None:
    assert get_connection_origin(_FakeConnection("https://host.example.com")) == "host.example.com"


def test_origin_with_nondefault_port_includes_port() -> None:
    assert get_connection_origin(_FakeConnection("https://host.example.com:8443")) == "host.example.com:8443"


def test_default_https_port_is_omitted() -> None:
    # urlparse only reports an explicit port; 443 with no explicit ":443" yields no port.
    assert get_connection_origin(_FakeConnection("https://host.example.com")) == "host.example.com"


def test_null_origin_is_not_none() -> None:
    """The critical case: a sandboxed-iframe ``Origin: null`` must NOT look like an absent header."""
    result = get_connection_origin(_FakeConnection("null"))
    assert result is not None
    assert result == "null"


def test_opaque_unparseable_origin_is_not_none() -> None:
    """Any present-but-hostless Origin returns a non-None token that can't match a real netloc."""
    for raw in ["null", "NULL", "garbage-no-scheme"]:
        result = get_connection_origin(_FakeConnection(raw))
        assert result is not None, f"{raw!r} collapsed to None"
        assert result != "host.example.com"
