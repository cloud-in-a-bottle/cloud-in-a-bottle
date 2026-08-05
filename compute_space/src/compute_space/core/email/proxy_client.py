"""HTTP client for the email API.

Presents a Keycloak bearer (via the shared KeycloakTokenProvider) and calls the
email API's ``/api/email/*`` endpoints. The instance uses it at startup to create
its SES domain identity and learn the DKIM CNAME records to publish in CoreDNS.
"""

from __future__ import annotations

from types import TracebackType

import attr
import httpx

from compute_space.core.tls.keycloak import TokenProvider


@attr.s(auto_attribs=True, frozen=True)
class DkimRecord:
    name: str
    value: str


@attr.s(auto_attribs=True, frozen=True)
class IdentityResult:
    verified: bool
    dkim_records: tuple[DkimRecord, ...]


class EmailProxyError(RuntimeError):
    """The email proxy returned an error or was unreachable."""


@attr.s(auto_attribs=True)
class EmailProxyClient:
    base_url: str
    token_provider: TokenProvider
    http_client: httpx.Client

    @classmethod
    def create(cls, base_url: str, token_provider: TokenProvider, timeout: float = 30.0) -> EmailProxyClient:
        return cls(
            base_url=base_url.rstrip("/"),
            token_provider=token_provider,
            http_client=httpx.Client(timeout=timeout),
        )

    def __enter__(self) -> EmailProxyClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.http_client.close()

    def _auth_headers(self) -> dict[str, str]:
        # Fetch fresh per call so the token refreshes transparently.
        return {"Authorization": f"Bearer {self.token_provider.get_token()}"}

    def ensure_identity(self, domain: str | None = None) -> IdentityResult:
        """Create the SES domain identity for the instance's zone (or a delegated
        subdomain) and return its DKIM records + verification status."""
        body = {"domain": domain} if domain else {}
        try:
            resp = self.http_client.post(
                f"{self.base_url}/api/email/identity", json=body, headers=self._auth_headers()
            )
        except httpx.HTTPError as e:
            raise EmailProxyError(f"email API unreachable: {e}") from e
        return _parse_identity(resp)

    def identity_status(self, domain: str | None = None) -> IdentityResult:
        params = {"domain": domain} if domain else {}
        try:
            resp = self.http_client.get(
                f"{self.base_url}/api/email/identity", params=params, headers=self._auth_headers()
            )
        except httpx.HTTPError as e:
            raise EmailProxyError(f"email API unreachable: {e}") from e
        return _parse_identity(resp)


def _parse_identity(resp: httpx.Response) -> IdentityResult:
    # The frontend returns 200 for an already-existing identity and 201 when it
    # creates one, both with the same body (domain + DKIM records). Accept any 2xx
    # so a freshly-created identity (201) is not mistaken for an error.
    if not (200 <= resp.status_code < 300):
        raise EmailProxyError(f"email API returned HTTP {resp.status_code}: {resp.text}")
    # A 2xx with a malformed body is still a failure — surface it as EmailProxyError
    # (not a raw KeyError/ValueError) so callers handle it uniformly.
    try:
        body = resp.json()
        records = tuple(DkimRecord(name=r["name"], value=r["value"]) for r in body.get("dkim_records", []))
        return IdentityResult(
            verified=bool(body.get("verified")),
            dkim_records=records,
        )
    except (ValueError, KeyError, TypeError) as e:
        raise EmailProxyError(f"email API returned a malformed identity response: {e}") from e
