"""Keycloak client-credentials auth for the openhost-cert-api broker.

Each instance gets its own Keycloak confidential client (a service account) in the
``openhost-customers`` realm.  The instance fetches an access token via the OAuth2
client-credentials grant and presents it as a bearer to cert-api, which validates
the JWT and enforces that the CSR's SANs fall within the instance's assigned
subdomain.  This replaces the old static shared bearer token.

Access tokens are short-lived (~300s) while a cert finalize-poll loop can run for
minutes, so the provider caches the token and refetches shortly before it expires.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import Protocol

import attr
import httpx

from compute_space.core.logging import logger

# Refetch this many seconds before the token actually expires so a request never
# goes out with a token that lapses in flight during the long finalize-poll loop.
_TOKEN_EXPIRY_SKEW_SECONDS = 30.0

# Fallback lifetime if the token endpoint omits expires_in (Keycloak always sends it).
_DEFAULT_TOKEN_LIFETIME_SECONDS = 300.0


class TokenProvider(Protocol):
    """Supplies a bearer token for each broker request, so it can refresh over time."""

    async def get_token(self) -> str: ...


@attr.s(auto_attribs=True, frozen=True)
class StaticTokenProvider:
    """A fixed bearer token — used by tests and any non-Keycloak deployment."""

    token: str

    async def get_token(self) -> str:
        return self.token


@attr.s(auto_attribs=True, frozen=True)
class KeycloakClientCredentials:
    """Per-instance Keycloak confidential-client credentials for cert-api auth."""

    # OIDC issuer, e.g. https://keycloak.<zone>/realms/openhost-customers
    issuer_url: str
    # Per-instance client, e.g. instance-<subdomain>.
    client_id: str
    # Per-instance secret — the only sensitive value of the three.
    client_secret: str

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer_url.rstrip('/')}/protocol/openid-connect/token"


class KeycloakAuthError(RuntimeError):
    """The Keycloak token endpoint rejected the client-credentials request."""


@attr.s(auto_attribs=True)
class KeycloakTokenProvider:
    """Fetches access tokens via the client-credentials grant, caching to ~expiry.

    Not frozen: it caches the most recent token and its expiry so repeated
    ``get_token()`` calls during a single cert acquisition reuse one token until
    it is about to lapse, then transparently refetch.
    """

    credentials: KeycloakClientCredentials
    http_client: httpx.AsyncClient
    # Injected so tests can drive expiry deterministically.
    monotonic: Callable[[], float] = time.monotonic
    _cached_token: str | None = attr.ib(default=None, init=False)
    _expires_at_monotonic: float = attr.ib(default=0.0, init=False)

    @classmethod
    def create(cls, credentials: KeycloakClientCredentials, timeout: float = 30.0) -> KeycloakTokenProvider:
        return cls(credentials=credentials, http_client=httpx.AsyncClient(timeout=timeout))

    async def __aenter__(self) -> KeycloakTokenProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.http_client.aclose()

    async def get_token(self) -> str:
        if self._cached_token is not None and self.monotonic() < self._expires_at_monotonic:
            return self._cached_token
        token, expires_in = await self._fetch_token()
        self._cached_token = token
        self._expires_at_monotonic = self.monotonic() + max(0.0, expires_in - _TOKEN_EXPIRY_SKEW_SECONDS)
        return token

    async def _fetch_token(self) -> tuple[str, float]:
        logger.info(
            f"Fetching cert-api access token for client {self.credentials.client_id!r} "
            f"from {self.credentials.token_endpoint}"
        )
        response = await self.http_client.post(
            self.credentials.token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            },
        )
        if response.status_code != 200:
            raise KeycloakAuthError(_token_error_message(response))
        body = response.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise KeycloakAuthError(f"Keycloak token response missing access_token: {body!r}")
        expires_in = body.get("expires_in", _DEFAULT_TOKEN_LIFETIME_SECONDS)
        return access_token, float(expires_in)


def _token_error_message(response: httpx.Response) -> str:
    """Build a readable error from a failed Keycloak token response."""
    message = f"Keycloak token request failed: HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return message
    if isinstance(body, dict):
        error = body.get("error", "error")
        description = body.get("error_description", response.text)
        return f"{message} ({error}: {description})"
    return message
