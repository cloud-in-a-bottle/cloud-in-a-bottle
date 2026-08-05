"""Tests for core.email.proxy_client.EmailProxyClient.

The client POSTs/GETs the email API's ``/api/email/identity`` endpoint with a
Keycloak bearer and parses the response into an ``IdentityResult``.  HTTP is
mocked with ``httpx.MockTransport`` so no network is touched; the client is built
directly (bypassing ``create``) so the mock transport can be injected, with a
``StaticTokenProvider`` supplying the bearer.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from compute_space.core.email.proxy_client import DkimRecord
from compute_space.core.email.proxy_client import EmailProxyClient
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.email.proxy_client import IdentityResult
from compute_space.core.tls.keycloak import StaticTokenProvider

_Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: _Handler, token: str = "test-token") -> EmailProxyClient:
    return EmailProxyClient(
        base_url="https://proxy.test",
        token_provider=StaticTokenProvider(token=token),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _identity_body(domain: str = "alice.example.com", verified: bool = False) -> dict[str, object]:
    return {
        "domain": domain,
        "verified": verified,
        "dkim_records": [
            {"name": "a._domainkey.alice.example.com", "value": "a.dkim.amazonses.com"},
            {"name": "b._domainkey.alice.example.com", "value": "b.dkim.amazonses.com"},
        ],
    }


# --- ensure_identity: parsing ------------------------------------------------


def test_ensure_identity_parses_200() -> None:
    client = _client(lambda req: httpx.Response(200, json=_identity_body()))
    result = client.ensure_identity()
    assert isinstance(result, IdentityResult)
    assert result.verified is False
    assert result.dkim_records == (
        DkimRecord(name="a._domainkey.alice.example.com", value="a.dkim.amazonses.com"),
        DkimRecord(name="b._domainkey.alice.example.com", value="b.dkim.amazonses.com"),
    )


def test_ensure_identity_accepts_201_created() -> None:
    # 201 (identity just created) carries the same body as 200 and must NOT be
    # treated as an error, or a freshly-created identity skips publishing records.
    client = _client(lambda req: httpx.Response(201, json=_identity_body()))
    result = client.ensure_identity()
    assert len(result.dkim_records) == 2


def test_ensure_identity_verified_true() -> None:
    client = _client(lambda req: httpx.Response(200, json=_identity_body(verified=True)))
    assert client.ensure_identity().verified is True


def test_ensure_identity_verified_truthy_coerced_to_bool() -> None:
    # verified is coerced with bool(); a truthy non-bool becomes True.
    body = _identity_body()
    body["verified"] = 1
    client = _client(lambda req: httpx.Response(200, json=body))
    result = client.ensure_identity()
    assert result.verified is True
    assert isinstance(result.verified, bool)


def test_ensure_identity_missing_verified_defaults_false() -> None:
    body = {"domain": "alice.example.com", "dkim_records": []}
    client = _client(lambda req: httpx.Response(200, json=body))
    assert client.ensure_identity().verified is False


def test_ensure_identity_missing_dkim_records_defaults_empty() -> None:
    body = {"domain": "alice.example.com", "verified": True}
    client = _client(lambda req: httpx.Response(200, json=body))
    assert client.ensure_identity().dkim_records == ()


def test_ensure_identity_ignores_extra_fields() -> None:
    body = _identity_body()
    body["unexpected"] = "ignored"
    client = _client(lambda req: httpx.Response(200, json=body))
    assert len(client.ensure_identity().dkim_records) == 2


def test_ensure_identity_malformed_dkim_record_raises_email_proxy_error() -> None:
    body = {"domain": "d.com", "verified": False, "dkim_records": [{"name": "x"}]}
    client = _client(lambda req: httpx.Response(200, json=body))
    with pytest.raises(EmailProxyError, match="malformed"):
        client.ensure_identity()


# --- ensure_identity: request shape ------------------------------------------


def test_ensure_identity_uses_post_to_identity_path() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json=_identity_body())

    _client(handler).ensure_identity()
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/email/identity"


def test_ensure_identity_sends_bearer_header() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_identity_body())

    _client(handler, token="s3cr3t").ensure_identity()
    assert seen["auth"] == "Bearer s3cr3t"


def test_ensure_identity_no_domain_sends_empty_body() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json=_identity_body())

    _client(handler).ensure_identity()
    # No domain -> {} body (empty JSON object).
    assert seen["json"] == {}


def test_ensure_identity_passes_domain_in_body() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json=_identity_body(domain="mail.mydomain.com"))

    _client(handler).ensure_identity(domain="mail.mydomain.com")
    assert seen["json"] == {"domain": "mail.mydomain.com"}


# --- ensure_identity: errors -------------------------------------------------


def test_ensure_identity_non_2xx_raises_email_proxy_error() -> None:
    client = _client(lambda req: httpx.Response(502, text="upstream boom"))
    with pytest.raises(EmailProxyError, match="HTTP 502"):
        client.ensure_identity()


def test_ensure_identity_error_includes_response_text() -> None:
    client = _client(lambda req: httpx.Response(400, text="bad domain"))
    with pytest.raises(EmailProxyError, match="bad domain"):
        client.ensure_identity()


def test_ensure_identity_network_error_raises_email_proxy_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(EmailProxyError, match="email API unreachable"):
        client.ensure_identity()


# --- identity_status ---------------------------------------------------------


def test_identity_status_uses_get() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json=_identity_body(verified=True))

    result = _client(handler).identity_status()
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/email/identity"
    assert result.verified is True


def test_identity_status_sends_bearer_header() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_identity_body())

    _client(handler, token="tok2").identity_status()
    assert seen["auth"] == "Bearer tok2"


def test_identity_status_passes_domain_as_query_param() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["query"] = dict(req.url.params)
        return httpx.Response(200, json=_identity_body(domain="mail.mydomain.com"))

    _client(handler).identity_status(domain="mail.mydomain.com")
    assert seen["query"] == {"domain": "mail.mydomain.com"}


def test_identity_status_no_domain_sends_no_query() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["query"] = dict(req.url.params)
        return httpx.Response(200, json=_identity_body())

    _client(handler).identity_status()
    assert seen["query"] == {}


def test_identity_status_non_2xx_raises() -> None:
    client = _client(lambda req: httpx.Response(404, text="not found"))
    with pytest.raises(EmailProxyError, match="HTTP 404"):
        client.identity_status()


def test_identity_status_network_error_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = _client(handler)
    with pytest.raises(EmailProxyError, match="email API unreachable"):
        client.identity_status()


# --- create() / context manager ----------------------------------------------


def test_create_strips_trailing_slash_from_base_url() -> None:
    client = EmailProxyClient.create("https://proxy.test/", StaticTokenProvider(token="t"))
    try:
        assert client.base_url == "https://proxy.test"
    finally:
        client.http_client.close()


def test_context_manager_closes_http_client() -> None:
    client = _client(lambda req: httpx.Response(200, json=_identity_body()))
    with client as c:
        assert c is client
    assert client.http_client.is_closed
