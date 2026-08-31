"""Tests for the Keycloak client-credentials token provider.

Runs against an in-process httpx.MockTransport — no real Keycloak is contacted —
and drives expiry with a fake monotonic clock so caching/refresh is deterministic.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import attr
import httpx
import pytest

from compute_space.core.tls.keycloak import KeycloakAuthError
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider

ISSUER = "https://keycloak.example.com/realms/openhost-customers"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"

CREDENTIALS = KeycloakClientCredentials(
    issuer_url=ISSUER,
    client_id="instance-alice",
    client_secret="s3cr3t",
)


@attr.s(auto_attribs=True)
class FakeMonotonic:
    """A monotonic clock whose value only moves when the test advances it."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@attr.s(auto_attribs=True)
class _TokenEndpoint:
    """Records calls and the last posted form so tests can assert on them."""

    calls: int = 0
    last_form: dict[str, list[str]] = attr.Factory(dict)
    expires_in: int = 300

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_form = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": f"token-{self.calls}", "expires_in": self.expires_in, "token_type": "Bearer"},
        )


def _provider(handler: object, monotonic: FakeMonotonic) -> KeycloakTokenProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(transport=transport)
    return KeycloakTokenProvider(credentials=CREDENTIALS, http_client=http_client, monotonic=monotonic)


def test_token_endpoint_composition() -> None:
    assert CREDENTIALS.token_endpoint == TOKEN_ENDPOINT
    # A trailing slash on the issuer must not double up.
    trailing = attr.evolve(CREDENTIALS, issuer_url=ISSUER + "/")
    assert trailing.token_endpoint == TOKEN_ENDPOINT


@pytest.mark.asyncio
async def test_fetches_token_with_client_credentials_grant() -> None:
    endpoint = _TokenEndpoint()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type")
        return endpoint.handler(request)

    async with _provider(handler, FakeMonotonic()) as provider:
        token = await provider.get_token()

    assert token == "token-1"
    assert captured["url"] == TOKEN_ENDPOINT
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    assert endpoint.last_form == {
        "grant_type": ["client_credentials"],
        "client_id": ["instance-alice"],
        "client_secret": ["s3cr3t"],
    }


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry() -> None:
    endpoint = _TokenEndpoint(expires_in=300)
    clock = FakeMonotonic()

    async with _provider(endpoint.handler, clock) as provider:
        assert await provider.get_token() == "token-1"
        assert endpoint.calls == 1

        # 30s skew before a 300s token => still cached at t=269.
        clock.now = 269.0
        assert await provider.get_token() == "token-1"
        assert endpoint.calls == 1

        # Past the skew-adjusted expiry (270s) => refetch.
        clock.now = 271.0
        assert await provider.get_token() == "token-2"
        assert endpoint.calls == 2


@pytest.mark.asyncio
async def test_error_response_raises_with_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized_client", "error_description": "bad secret"})

    async with _provider(handler, FakeMonotonic()) as provider:
        with pytest.raises(KeycloakAuthError) as exc_info:
            await provider.get_token()

    message = str(exc_info.value)
    assert "401" in message
    assert "unauthorized_client" in message
    assert "bad secret" in message


@pytest.mark.asyncio
async def test_missing_access_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 300})

    async with _provider(handler, FakeMonotonic()) as provider:
        with pytest.raises(KeycloakAuthError):
            await provider.get_token()
