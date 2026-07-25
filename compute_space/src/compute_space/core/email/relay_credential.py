"""Fetch this instance's SMTP relay credential from the frontend at runtime.

The relay credential (host/port + username/password) is deliberately NOT baked
into the instance's config. Instead the router fetches it from the email frontend
(imbue-hosted-spaces) using the same per-instance Keycloak client the instance
already holds for cert-api/email, and the frontend has the backend derive
``HMAC(RELAY_SECRET, zone)``. This means:

  * nothing email-specific (no relay password) is stored in per-instance config,
    so enabling email needs no secret injection and upgrades never touch it;
  * rotating ``RELAY_SECRET`` (which lives only on the backend) rotates every
    instance's credential automatically — the instance just refetches.

The result is cached in-process with a short TTL so the mailbox app's
relay-config calls and the inbound-auth check don't hit the frontend on every
request, while still picking up a rotated secret within the TTL.
"""

from __future__ import annotations

import threading
import time

import attr
import httpx

from compute_space.config import Config
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider

# How long a fetched credential is trusted before we refetch. Short enough that a
# rotated RELAY_SECRET propagates quickly; long enough to avoid per-request calls.
_CACHE_TTL_SECONDS = 300.0


@attr.s(auto_attribs=True, frozen=True)
class RelayCredential:
    """The SMTP smarthost config the mailbox app needs to relay outbound mail."""

    smtp_relay_host: str
    smtp_relay_port: int
    smtp_relay_user: str
    smtp_relay_password: str
    zone_domain: str
    custom_domain: str | None


class RelayCredentialError(RuntimeError):
    pass


@attr.s(auto_attribs=True)
class RelayCredentialProvider:
    """Fetches + caches this instance's relay credential from the frontend."""

    config: Config
    monotonic: object = time.monotonic
    _cached: RelayCredential | None = attr.ib(default=None, init=False)
    _expires_at: float = attr.ib(default=0.0, init=False)
    _lock: threading.Lock = attr.ib(factory=threading.Lock, init=False)

    def get(self) -> RelayCredential | None:
        """Return the current relay credential, or None when email isn't configured.

        Raises RelayCredentialError only on an unexpected fetch failure while email
        IS configured (so callers can distinguish "off" from "temporarily broken").
        """
        if not self.config.email_enabled:
            return None
        with self._lock:
            now = self.monotonic()  # type: ignore[operator]
            if self._cached is not None and now < self._expires_at:
                return self._cached
            cred = self._fetch()
            self._cached = cred
            self._expires_at = now + _CACHE_TTL_SECONDS
            return cred

    def _fetch(self) -> RelayCredential:
        cfg = self.config
        assert cfg.email_proxy_base_url is not None
        # Keycloak creds resolve (email_keycloak_* with cert-api fallback);
        # email_enabled guarantees they are non-None here.
        assert cfg.email_keycloak_issuer_url_resolved is not None
        assert cfg.email_keycloak_client_id_resolved is not None
        assert cfg.email_keycloak_client_secret_resolved is not None
        credentials = KeycloakClientCredentials(
            issuer_url=cfg.email_keycloak_issuer_url_resolved,
            client_id=cfg.email_keycloak_client_id_resolved,
            client_secret=cfg.email_keycloak_client_secret_resolved,
        )
        url = f"{cfg.email_proxy_base_url.rstrip('/')}/api/email/relay-config"
        try:
            with KeycloakTokenProvider.create(credentials) as tokens:
                token = tokens.get_token()
                with httpx.Client(timeout=30.0) as client:
                    resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise RelayCredentialError(f"relay-config fetch failed: {e}") from e
        if resp.status_code != 200:
            raise RelayCredentialError(f"relay-config returned HTTP {resp.status_code}")
        body = resp.json()
        if not body.get("configured"):
            raise RelayCredentialError("frontend reports relay not configured")
        try:
            return RelayCredential(
                smtp_relay_host=body["smtp_relay_host"],
                smtp_relay_port=int(body["smtp_relay_port"]),
                smtp_relay_user=body["smtp_relay_user"],
                smtp_relay_password=body["smtp_relay_password"],
                zone_domain=body.get("zone_domain") or cfg.zone_domain_no_port,
                custom_domain=cfg.email_custom_domain_normalized,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise RelayCredentialError(f"relay-config response malformed: {e}") from e
