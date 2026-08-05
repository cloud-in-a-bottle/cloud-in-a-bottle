"""Fetch this instance's SMTP relay credential from the frontend at runtime.

The relay credential (host/port + username/password) is deliberately NOT baked
into the instance's config. Instead the router fetches it from the Imbue email
frontend using the same per-instance shared Imbue identity the instance already
holds for cert-api/email, and the frontend has the backend derive
``HMAC(RELAY_SECRET, zone)``. This means:

  * nothing email-specific (no relay password) is stored in per-instance config,
    so enabling email needs no secret injection and upgrades never touch it;
  * rotating ``RELAY_SECRET`` (which lives only on the backend) rotates every
    instance's credential automatically: the instance just refetches.

The result is cached in-process with a short TTL so the mailbox app's
relay-config calls and the inbound-auth check don't hit the frontend on every
request, while still picking up a rotated secret within the TTL. The cache is
keyed on the identity/zone the credential depends on, so distinct instances (or
tests) never bleed into each other.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import attr
import httpx

from compute_space.config import Config
from compute_space.core.domains import primary_domain
from compute_space.core.email.enablement import email_enabled
from compute_space.core.email.enablement import resolve_email_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider

# How long a fetched credential is trusted before we refetch. Short enough that a
# rotated RELAY_SECRET propagates quickly; long enough to avoid per-request calls.
_CACHE_TTL_SECONDS = 300.0


@attr.s(auto_attribs=True, frozen=True)
class RelayCredential:
    """The SMTP smarthost login the router needs to relay outbound mail.

    Just the four SMTP-connect fields; From-scope is enforced centrally at the
    proxy, so the instance-side relay never needs the zone/custom-domain here.
    """

    smtp_relay_host: str
    smtp_relay_port: int
    smtp_relay_user: str
    smtp_relay_password: str


class RelayCredentialError(RuntimeError):
    pass


@attr.s(auto_attribs=True)
class RelayCredentialProvider:
    """Fetches + caches this instance's relay credential from the frontend.

    ``get`` takes the DB because the credential's inputs (the shared Imbue identity
    and the primary zone) are sourced live from the DB, not from the frozen Config.
    """

    config: Config
    monotonic: object = time.monotonic
    _cached: RelayCredential | None = attr.ib(default=None, init=False)
    _expires_at: float = attr.ib(default=0.0, init=False)
    _cache_key: tuple[object, ...] | None = attr.ib(default=None, init=False)
    _lock: threading.Lock = attr.ib(factory=threading.Lock, init=False)

    def get(self, db: sqlite3.Connection) -> RelayCredential | None:
        """Return the current relay credential, or None when email isn't configured.

        Raises RelayCredentialError only on an unexpected fetch failure while email
        IS configured (so callers can distinguish "off" from "temporarily broken").
        """
        if not email_enabled(self.config, db):
            return None
        credentials = resolve_email_identity(self.config, db)
        assert credentials is not None  # guaranteed by email_enabled
        zone = primary_domain(db).name_no_port
        custom_domain = self.config.email_custom_domain_normalized
        # Key the cache on the inputs the credential depends on so a rotated
        # identity/zone/custom-domain doesn't serve a stale entry.
        key: tuple[object, ...] = (
            credentials.issuer_url,
            credentials.client_id,
            credentials.client_secret,
            zone,
            custom_domain,
        )
        with self._lock:
            now = self.monotonic()  # type: ignore[operator]
            if self._cached is not None and self._cache_key == key and now < self._expires_at:
                return self._cached
            cred = self._fetch(credentials)
            self._cached = cred
            self._cache_key = key
            self._expires_at = now + _CACHE_TTL_SECONDS
            return cred

    def _fetch(self, credentials: KeycloakClientCredentials) -> RelayCredential:
        base_url = self.config.email_proxy_base_url
        assert base_url is not None  # guaranteed by email_enabled
        url = f"{base_url.rstrip('/')}/api/email/relay-config"
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
            )
        except (KeyError, TypeError, ValueError) as e:
            raise RelayCredentialError(f"relay-config response malformed: {e}") from e


# Process-wide relay-credential providers, keyed on the config fields the
# credential depends on (NOT id(config): provide_config() hands each request a
# freshly-built Config, so an id()-based key would never hit the TTL cache and
# would leak a provider per request). The identity/zone the credential also
# depends on are sourced live from the DB and keyed inside the provider itself.
_relay_providers: dict[tuple[object, ...], RelayCredentialProvider] = {}
_relay_providers_lock = threading.Lock()


def _relay_provider_key(config: Config) -> tuple[object, ...]:
    """A stable key over the config fields the relay credential depends on."""
    return (config.email_proxy_base_url, config.email_custom_domain_normalized)


def get_relay_credential_provider(config: Config) -> RelayCredentialProvider:
    """Return the shared, cached relay-credential provider for this config.

    Both the router SMTP email service and any owner-facing route share one
    provider per distinct config so the frontend fetch is cached (short TTL)
    across all callers rather than refetched per request.
    """
    key = _relay_provider_key(config)
    with _relay_providers_lock:
        provider = _relay_providers.get(key)
        if provider is None:
            provider = RelayCredentialProvider(config=config)
            _relay_providers[key] = provider
        return provider
