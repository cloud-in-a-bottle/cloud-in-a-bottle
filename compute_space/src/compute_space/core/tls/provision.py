import asyncio
from pathlib import Path

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import Config
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.acquire_cert_broker import acquire_tls_cert_via_broker
from compute_space.core.tls.cert_api_client import CertApiClient
from compute_space.core.tls.keycloak import KeycloakTokenProvider


def provision_cert(config: Config) -> None:
    """Acquire a TLS cert with the configured provider and install it at the config's cert/key paths.

    Used both for the initial acquisition at startup and for renewals.  The default "acme" provider
    is the BYO-ACME path; "cert_api" fetches from the openhost-cert-api broker.  Caller must ensure
    CoreDNS is running (both providers answer DNS-01 challenges through the local zone file).

    The cert_provider value and its required settings are validated when the Config is constructed
    (Config.__attrs_post_init__), so here we only narrow the optional fields for the type checker.
    """
    if config.cert_provider == CERT_PROVIDER_ACME:
        if not config.acme_account_key_path:
            raise RuntimeError("ACME account key path must be set in config to acquire TLS cert")
        asyncio.run(
            acquire_tls_cert(
                domain=config.zone_domain,
                cert_path=config.tls_cert_path,
                key_path=config.tls_key_path,
                acme_account_key_path=Path(config.acme_account_key_path),
                coredns_zonefile_path=config.coredns_zonefile_path,
                acme_email=config.acme_email,
                directory_url=config.acme_directory_url,
            )
        )
    else:
        # cert_provider is guaranteed to be CERT_PROVIDER_CERT_API with the broker
        # URL and a resolvable instance credential (validated in
        # Config.__attrs_post_init__). The credential resolves from the deprecated
        # cert_api_keycloak_* override or the shared imbue_identity_* fields.
        assert config.cert_api_base_url is not None
        credentials = config.instance_identity
        assert credentials is not None
        # The token provider fetches a bearer from Keycloak (client-credentials) and
        # refreshes it transparently across the broker's finalize-poll loop.
        with KeycloakTokenProvider.create(credentials) as token_provider:
            with CertApiClient.create(config.cert_api_base_url, token_provider) as client:
                acquire_tls_cert_via_broker(
                    domain=config.zone_domain,
                    cert_path=config.tls_cert_path,
                    key_path=config.tls_key_path,
                    coredns_zonefile_path=config.coredns_zonefile_path,
                    client=client,
                )
