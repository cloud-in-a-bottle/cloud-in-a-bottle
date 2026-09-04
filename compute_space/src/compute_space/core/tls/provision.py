import asyncio
import sqlite3
import weakref
from pathlib import Path

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import Config
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.domains import primary_domain
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.acquire_cert_broker import acquire_tls_cert_via_broker
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.keycloak import KeycloakTokenProvider

# Note: this is messy and should get cleaned up when we cleanup the cert aquisition stuff.
# Serializes cert issuance; see acquire_cert_for_domain.  One lock per event loop rather than one
# module-level lock, because an asyncio.Lock binds to the first loop that contends it and then
# raises on any other -- fine for the router, which has exactly one loop for its whole life, but a
# module-level lock would fail the second test that contends it.
_ISSUANCE_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _issuance_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _ISSUANCE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _ISSUANCE_LOCKS[loop] = lock
    return lock


async def acquire_cert_for_domain(
    config: Config,
    domain: str,
    cert_path: Path,
    key_path: Path,
    db: sqlite3.Connection,
    dns_provider: InternalDnsProvider,
) -> None:
    """Acquire a TLS cert (apex + wildcard) for ``domain`` with the configured provider and
    install it at ``cert_path``/``key_path``.

    The provider dispatch (BYO-ACME vs the openhost-cert-api broker) and DNS-01 mechanics are
    identical for every domain; only the domain name and output paths vary.  The DNS-01 challenge
    TXT records go into every zone ``dns_provider`` serves, so the challenge is answerable for secondary
    domains too.  Caller must ensure ``dns_provider`` is authoritative for ``domain`` and that
    ``cert_path``'s parent directory exists.

    The cert_provider value and its required settings are validated when the Config is constructed
    (Config.__attrs_post_init__), so here we only narrow the optional fields for the type checker.

    One acquisition at a time, instance-wide: every DNS-01 challenge is answered from the same
    ``_acme-challenge`` name, so a second one starting mid-flight would overwrite the first one's
    tokens and then clear them on its way out.  Acquisitions are rare and no request waits on one
    (the add-domain route runs it in the background), so queueing them costs nothing.
    """
    # Fail fast and legibly: DNS-01 answers the challenge from a zone this instance serves, so
    # without one the CA would just time out.  The message reaches the domain's error_message.
    if not dns_provider.serves_public_zones:
        raise RuntimeError("CoreDNS must be enabled to acquire a TLS cert via DNS-01 challenge")
    async with _issuance_lock():
        await _acquire_cert_for_domain_locked(config, domain, cert_path, key_path, db, dns_provider)


async def _acquire_cert_for_domain_locked(
    config: Config,
    domain: str,
    cert_path: Path,
    key_path: Path,
    db: sqlite3.Connection,
    dns_provider: InternalDnsProvider,
) -> None:
    """The acquisition itself.  Caller holds ``dns_provider.challenge_lock``."""
    if config.cert_provider == CERT_PROVIDER_ACME:
        if not config.acme_account_key_path:
            raise RuntimeError("ACME account key path must be set in config to acquire TLS cert")
        await acquire_tls_cert(
            domain=domain,
            cert_path=cert_path,
            key_path=key_path,
            acme_account_key_path=Path(config.acme_account_key_path),
            dns_provider=dns_provider,
            acme_email=config.acme_email,
            directory_url=config.acme_directory_url,
        )
    else:
        # cert_provider is guaranteed to be CERT_PROVIDER_CERT_API with the broker
        # URL set (validated in Config.__attrs_post_init__). The credential is the
        # shared per-instance Imbue identity, read live from the settings table
        # (falling back to the deprecated cert_api_keycloak_* config fields).
        assert config.cert_api_base_url is not None
        credentials = get_instance_identity(db, config)
        assert credentials is not None
        # The token provider fetches a bearer from Keycloak (client-credentials) and
        # refreshes it transparently across the broker's finalize-poll loop.
        async with KeycloakTokenProvider.create(credentials) as token_provider:
            async with CertApiClient.create(config.cert_api_base_url, token_provider) as client:
                await acquire_tls_cert_via_broker(
                    domain=domain,
                    cert_path=cert_path,
                    key_path=key_path,
                    dns_provider=dns_provider,
                    client=client,
                )


async def provision_cert(config: Config, db: sqlite3.Connection, dns_provider: InternalDnsProvider) -> None:
    """Acquire the primary domain's TLS cert and install it at the config's cert/key paths.

    Used both for the initial acquisition at startup and for renewals.  Thin wrapper over
    ``acquire_cert_for_domain`` for the primary domain."""
    primary = primary_domain(db)
    await acquire_cert_for_domain(config, primary.name, config.tls_cert_path, config.tls_key_path, db, dns_provider)
