"""Tests for the openhost-cert-api broker HTTP client.

These run against an in-process httpx.MockTransport — no real broker is contacted.
"""

from __future__ import annotations

import json

import httpx
import pytest

from compute_space.core.tls.cert_api_client import FINALIZE_STATUS_PENDING
from compute_space.core.tls.cert_api_client import FINALIZE_STATUS_VALID
from compute_space.core.tls.cert_api_client import CertApiBadRequest
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.cert_api_client import CertApiError
from compute_space.core.tls.cert_api_client import CertApiNotFound
from compute_space.core.tls.cert_api_client import CertApiOrderFailed
from compute_space.core.tls.cert_api_client import CertApiUnauthorized
from compute_space.core.tls.keycloak import StaticTokenProvider

BASE_URL = "https://broker.test"
TOKEN = "per-instance-token"


def _make_client(handler: object) -> CertApiClient:
    """Build a CertApiClient backed by a MockTransport using the given request handler."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
    return CertApiClient(http_client=http_client, token_provider=StaticTokenProvider(TOKEN))


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    async with _make_client(handler) as client:
        assert await client.health() is True


@pytest.mark.asyncio
async def test_health_non_ok_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "down"})

    async with _make_client(handler) as client:
        assert await client.health() is False


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_sends_csr_and_auth() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "order_id": "order-123",
                "challenges": [
                    {
                        "domain": "app.example.com",
                        "record_name": "_acme-challenge.app.example.com",
                        "record_value": "base-value",
                    },
                    {
                        "domain": "*.app.example.com",
                        "record_name": "_acme-challenge.app.example.com",
                        "record_value": "wildcard-value",
                    },
                ],
            },
        )

    async with _make_client(handler) as client:
        result = await client.create_order(
            "-----BEGIN CERTIFICATE REQUEST-----\nMII...\n-----END CERTIFICATE REQUEST-----\n"
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/orders"
    assert captured["auth"] == f"Bearer {TOKEN}"
    assert captured["body"] == {
        "csr": "-----BEGIN CERTIFICATE REQUEST-----\nMII...\n-----END CERTIFICATE REQUEST-----\n"
    }

    assert result.order_id == "order-123"
    assert len(result.challenges) == 2
    assert result.challenges[0].domain == "app.example.com"
    assert result.challenges[0].record_name == "_acme-challenge.app.example.com"
    assert result.challenges[0].record_value == "base-value"
    assert result.challenges[1].record_value == "wildcard-value"


@pytest.mark.asyncio
async def test_create_order_bad_request_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_request", "message": "csr unparseable"})

    async with _make_client(handler) as client:
        with pytest.raises(CertApiBadRequest) as exc_info:
            await client.create_order("not-a-csr")
    assert "csr unparseable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_order_unauthorized_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized", "message": "bad token"})

    async with _make_client(handler) as client:
        with pytest.raises(CertApiUnauthorized):
            await client.create_order("csr")


# ---------------------------------------------------------------------------
# finalize_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/orders/order-123/finalize"
        return httpx.Response(202, json={"status": "pending"})

    async with _make_client(handler) as client:
        result = await client.finalize_order("order-123")
    assert result.status == FINALIZE_STATUS_PENDING
    assert result.certificate is None


@pytest.mark.asyncio
async def test_finalize_valid_returns_certificate() -> None:
    chain = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "valid", "certificate": chain})

    async with _make_client(handler) as client:
        result = await client.finalize_order("order-123")
    assert result.status == FINALIZE_STATUS_VALID
    assert result.certificate == chain


@pytest.mark.asyncio
async def test_finalize_unknown_order_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found", "message": "no such order"})

    async with _make_client(handler) as client:
        with pytest.raises(CertApiNotFound):
            await client.finalize_order("nope")


@pytest.mark.asyncio
async def test_finalize_failed_order_raises_with_acme_detail() -> None:
    # The broker passes the ACME problem document's `detail` through on a 409.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": "order_failed",
                "detail": "DNS problem: NXDOMAIN looking up TXT for _acme-challenge.app.example.com",
            },
        )

    async with _make_client(handler) as client:
        with pytest.raises(CertApiOrderFailed) as exc_info:
            await client.finalize_order("order-123")
    assert "order_failed" in str(exc_info.value)
    assert "NXDOMAIN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unexpected_status_raises_generic_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _make_client(handler) as client:
        with pytest.raises(CertApiError):
            await client.create_order("csr")


# ---------------------------------------------------------------------------
# auth header is rebuilt per request (so a refreshing token provider takes effect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_token_fetched_fresh_per_request() -> None:
    """The bearer is pulled from the provider on each call, not cached at construct time."""

    class _RotatingTokenProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def get_token(self) -> str:
            self.calls += 1
            return f"token-{self.calls}"

    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        if request.url.path == "/v1/orders":
            return httpx.Response(200, json={"order_id": "o1", "challenges": []})
        return httpx.Response(202, json={"status": "pending"})

    provider = _RotatingTokenProvider()
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
    async with CertApiClient(http_client=http_client, token_provider=provider) as client:
        await client.create_order("csr")
        await client.finalize_order("o1")

    # Each request asked the provider for a token, and the rotated value was used.
    assert seen_auth == ["Bearer token-1", "Bearer token-2"]
    # health() needs no auth, so the provider was consulted exactly twice.
    assert provider.calls == 2
