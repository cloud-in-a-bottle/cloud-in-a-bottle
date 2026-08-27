"""HTTP client for the openhost-cert-api broker.

The broker holds the ACME account and issues certs for a CSR after the instance
proves DNS control.  The instance never sees ACME account credentials — it only
sends a CSR and publishes the DNS-01 TXT records the broker hands back.

Requests are authenticated with a per-instance bearer token from a TokenProvider
(Keycloak client-credentials in production); the header is set per-request so a
token can refresh mid-flow during the long finalize-poll loop.

See the broker contract: github.com/cloud-in-a-bottle/cert-api.
"""

from __future__ import annotations

from types import TracebackType

import attr
import httpx

from compute_space.core.tls.keycloak import TokenProvider

# finalize_order status values returned by the broker.
FINALIZE_STATUS_VALID = "valid"
FINALIZE_STATUS_PENDING = "pending"


class CertApiError(RuntimeError):
    """Base error for any unexpected broker response."""


class CertApiBadRequest(CertApiError):
    """The broker rejected the request body (HTTP 400)."""


class CertApiUnauthorized(CertApiError):
    """The per-instance bearer token was missing or rejected (HTTP 401)."""


class CertApiNotFound(CertApiError):
    """The referenced order does not exist (HTTP 404)."""


class CertApiOrderFailed(CertApiError):
    """The order failed validation/issuance (HTTP 409).

    Terminal, not retryable: the broker drove the ACME order to a failed state
    (e.g. DNS-01 validation or CAA failed).  The response body carries the ACME
    error detail, which is surfaced in the exception message for debugging.
    """


@attr.s(auto_attribs=True, frozen=True)
class CertChallenge:
    """A single DNS-01 challenge the instance must publish, verbatim, via CoreDNS."""

    domain: str
    # FQDN to create the TXT record at, e.g. "_acme-challenge.app.example.com".
    record_name: str
    # TXT value to publish.  Computed by the broker against ITS account key, so
    # it must be used exactly as given.
    record_value: str


@attr.s(auto_attribs=True, frozen=True)
class CreateOrderResult:
    order_id: str
    challenges: list[CertChallenge]


@attr.s(auto_attribs=True, frozen=True)
class FinalizeResult:
    # FINALIZE_STATUS_VALID or FINALIZE_STATUS_PENDING.
    status: str
    # PEM certificate chain when status is valid, otherwise None (still pending).
    certificate: str | None


_STATUS_TO_ERROR: dict[int, type[CertApiError]] = {
    400: CertApiBadRequest,
    401: CertApiUnauthorized,
    404: CertApiNotFound,
    409: CertApiOrderFailed,
}


def _raise_for_unexpected(response: httpx.Response, expected: set[int]) -> None:
    """Raise a mapped CertApiError if the response status is not one we expect."""
    if response.status_code in expected:
        return
    message = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        # Accept either the broker's own {error, message} shape or a passed-through
        # ACME problem document ({type, detail, ...}); fall back to the raw body.
        detail = body.get("message") or body.get("detail") or response.text
        message = f"{body.get('error', 'error')}: {detail}"
    error_cls = _STATUS_TO_ERROR.get(response.status_code, CertApiError)
    raise error_cls(message)


@attr.s(auto_attribs=True, frozen=True)
class CertApiClient:
    """Thin synchronous client over the broker's REST API.

    Construct via ``CertApiClient.create(base_url, token_provider)`` in production;
    tests inject an ``httpx.AsyncClient`` backed by a MockTransport plus a StaticTokenProvider.
    """

    http_client: httpx.AsyncClient
    token_provider: TokenProvider

    @classmethod
    def create(cls, base_url: str, token_provider: TokenProvider, timeout: float = 30.0) -> CertApiClient:
        http_client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )
        return cls(http_client=http_client, token_provider=token_provider)

    async def __aenter__(self) -> CertApiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.http_client.aclose()

    async def _auth_headers(self) -> dict[str, str]:
        """Bearer header built fresh per request so the token can refresh mid-flow."""
        return {"Authorization": f"Bearer {await self.token_provider.get_token()}"}

    async def health(self) -> bool:
        """Return True if the broker reports healthy.  Does not require auth."""
        response = await self.http_client.get("/health")
        if response.status_code != 200:
            return False
        body = response.json()
        return isinstance(body, dict) and body.get("status") == "ok"

    async def create_order(self, csr_pem: str) -> CreateOrderResult:
        """Submit a CSR and receive the DNS-01 challenge record(s) to publish."""
        response = await self.http_client.post("/v1/orders", json={"csr": csr_pem}, headers=await self._auth_headers())
        _raise_for_unexpected(response, {200})
        body = response.json()
        challenges = [
            CertChallenge(
                domain=challenge["domain"],
                record_name=challenge["record_name"],
                record_value=challenge["record_value"],
            )
            for challenge in body["challenges"]
        ]
        return CreateOrderResult(order_id=body["order_id"], challenges=challenges)

    async def finalize_order(self, order_id: str) -> FinalizeResult:
        """Poll the order.

        200 -> issued (certificate present); 202 -> still pending; 409 -> the order
        failed terminally (raises CertApiOrderFailed with the ACME error detail).
        """
        response = await self.http_client.post(f"/v1/orders/{order_id}/finalize", headers=await self._auth_headers())
        _raise_for_unexpected(response, {200, 202})
        if response.status_code == 202:
            return FinalizeResult(status=FINALIZE_STATUS_PENDING, certificate=None)
        body = response.json()
        return FinalizeResult(status=body["status"], certificate=body.get("certificate"))
