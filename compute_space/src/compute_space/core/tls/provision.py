import asyncio
from pathlib import Path

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import Config
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.acquire_cert_broker import acquire_tls_cert_via_broker
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider


def acquire_cert_for_domain(config: Config, domain: str, cert_path: Path, key_path: Path) -> None:
    """Acquire a TLS cert (apex + wildcard) for ``domain`` with the configured provider and
    install it at ``cert_path``/``key_path``.

    The provider dispatch (BYO-ACME vs the openhost-cert-api broker) and DNS-01 mechanics are
    identical for every domain; only the domain name and output paths vary.  The DNS-01 challenge
    TXT records go into ``domain``'s own zone file (each public domain is a separate authoritative
    zone), so the challenge is answerable for secondary domains too.  Caller must ensure CoreDNS is
    running and authoritative for ``domain``, and that ``cert_path``'s parent directory exists.

    The cert_provider value and its required settings are validated when the Config is constructed
    (Config.__attrs_post_init__), so here we only narrow the optional fields for the type checker.
    """
    if config.cert_provider == CERT_PROVIDER_ACME:
        if not config.acme_account_key_path:
            raise RuntimeError("ACME account key path must be set in config to acquire TLS cert")
        asyncio.run(
            acquire_tls_cert(
                domain=domain,
                cert_path=cert_path,
                key_path=key_path,
                acme_account_key_path=Path(config.acme_account_key_path),
                coredns_zonefile_path=config.coredns_zonefile_path_for(domain),
                acme_email=config.acme_email,
                directory_url=config.acme_directory_url,
            )
        )
    else:
        # cert_provider is guaranteed to be CERT_PROVIDER_CERT_API with all of
        # the cert_api settings populated (validated in Config.__attrs_post_init__).
        assert config.cert_api_base_url is not None
        assert config.cert_api_keycloak_issuer_url is not None
        assert config.cert_api_keycloak_client_id is not None
        assert config.cert_api_keycloak_client_secret is not None
        credentials = KeycloakClientCredentials(
            issuer_url=config.cert_api_keycloak_issuer_url,
            client_id=config.cert_api_keycloak_client_id,
            client_secret=config.cert_api_keycloak_client_secret,
        )
        # The token provider fetches a bearer from Keycloak (client-credentials) and
        # refreshes it transparently across the broker's finalize-poll loop.
        with KeycloakTokenProvider.create(credentials) as token_provider:
            with CertApiClient.create(config.cert_api_base_url, token_provider) as client:
                acquire_tls_cert_via_broker(
                    domain=domain,
                    cert_path=cert_path,
                    key_path=key_path,
                    coredns_zonefile_path=config.coredns_zonefile_path_for(domain),
                    client=client,
                )


def provision_cert(config: Config) -> None:
    """Acquire the primary domain's TLS cert and install it at the config's cert/key paths.

    Used both for the initial acquisition at startup and for renewals.  Thin wrapper over
    ``acquire_cert_for_domain`` for the primary ``zone_domain`` (unchanged behavior).
    """
    acquire_cert_for_domain(config, config.primary_domain.name, config.tls_cert_path, config.tls_key_path)
