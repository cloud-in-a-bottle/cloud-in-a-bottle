import asyncio
import sqlite3
from pathlib import Path

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import Config
from compute_space.core.dns.client import DnsClient
from compute_space.core.dns.client import dns_client
from compute_space.core.domains import primary_domain
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.acquire_cert_broker import acquire_tls_cert_via_broker
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.keycloak import KeycloakTokenProvider


def acquire_cert_for_domain(
    config: Config, domain: str, cert_path: Path, key_path: Path, db: sqlite3.Connection
) -> None:
    """Acquire a TLS cert (apex + wildcard) for ``domain`` with the configured provider and
    install it at ``cert_path``/``key_path``.

    The provider dispatch (BYO-ACME vs the openhost-cert-api broker) and DNS-01 mechanics are
    identical for every domain; only the domain name and output paths vary.  The challenge records
    are published through the ``dns`` service — backed by our own CoreDNS zone files, or an
    external provider via the ``dns`` service — so a secondary domain is answerable either way.
    Caller must ensure the service can serve ``domain`` and that ``cert_path``'s parent exists.

    The cert_provider value and its required settings are validated when the Config is constructed
    (Config.__attrs_post_init__), so here we only narrow the optional fields for the type checker.
    """
    with dns_client(config, db) as dns:
        _acquire_with_dns(config, domain, cert_path, key_path, db, dns)


def _acquire_with_dns(
    config: Config,
    domain: str,
    cert_path: Path,
    key_path: Path,
    db: sqlite3.Connection,
    dns: DnsClient,
) -> None:
    if config.cert_provider == CERT_PROVIDER_ACME:
        if not config.acme_account_key_path:
            raise RuntimeError("ACME account key path must be set in config to acquire TLS cert")
        asyncio.run(
            acquire_tls_cert(
                domain=domain,
                cert_path=cert_path,
                key_path=key_path,
                acme_account_key_path=Path(config.acme_account_key_path),
                dns=dns,
                acme_email=config.acme_email,
                directory_url=config.acme_directory_url,
            )
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
        with KeycloakTokenProvider.create(credentials) as token_provider:
            with CertApiClient.create(config.cert_api_base_url, token_provider) as client:
                acquire_tls_cert_via_broker(
                    domain=domain,
                    cert_path=cert_path,
                    key_path=key_path,
                    dns=dns,
                    client=client,
                )


def provision_cert(config: Config, db: sqlite3.Connection) -> None:
    """Acquire the primary domain's TLS cert and install it at the config's cert/key paths.

    Used both for the initial acquisition at startup and for renewals.  Thin wrapper over
    ``acquire_cert_for_domain`` for the primary domain."""
    primary = primary_domain(db)
    acquire_cert_for_domain(config, primary.name, config.tls_cert_path, config.tls_key_path, db)
