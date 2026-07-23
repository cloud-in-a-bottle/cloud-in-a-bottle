"""Provision the instance's email DNS records at startup.

When email is enabled, this:
  1. authenticates to the email proxy with the instance's Keycloak client,
  2. asks the proxy to create/ensure the SES domain identity and return the
     DKIM CNAME records SES requires,
  3. writes those DKIM records plus SPF/DMARC/MX into the CoreDNS zone.

This runs for the instance's built-in zone, and — when the owner has delegated a
custom mail domain (email_custom_domain) to this instance with an NS record —
also for that custom zone, so a single NS record is all it takes to send/receive
as the custom domain.

Runs after start_coredns on each boot (the zone file is regenerated from
template there, so the email records must be re-applied every time). Best-effort:
a proxy or SES hiccup logs and returns rather than blocking router startup —
mail is not load-bearing for the instance coming up.
"""

from __future__ import annotations

from pathlib import Path

from compute_space.config import Config
from compute_space.core.dns import DkimCname
from compute_space.core.dns import apply_email_records
from compute_space.core.email.proxy_client import EmailProxyClient
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.logging import logger
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider


def provision_email_records(config: Config) -> None:
    """Create the SES identity/identities and publish email DNS records.

    Provisions the instance's built-in zone, and the delegated custom mail domain
    when one is configured. No-op when email is disabled. Validated config
    guarantees the email_* fields are populated when email_enabled is True
    (Config.__attrs_post_init__).
    """
    if not config.email_enabled:
        return
    assert config.email_proxy_base_url is not None
    assert config.email_keycloak_issuer_url is not None
    assert config.email_keycloak_client_id is not None
    assert config.email_keycloak_client_secret is not None
    # email_inbound_mx_host is only required for ses mode (validated in Config);
    # direct mode uses the instance's own mail host + public_ip instead.

    credentials = KeycloakClientCredentials(
        issuer_url=config.email_keycloak_issuer_url,
        client_id=config.email_keycloak_client_id,
        client_secret=config.email_keycloak_client_secret,
    )
    try:
        with KeycloakTokenProvider.create(credentials) as token_provider:
            with EmailProxyClient.create(config.email_proxy_base_url, token_provider) as client:
                # Built-in zone: the proxy defaults to the caller's zone when no
                # domain is passed.
                _provision_zone(
                    config,
                    client,
                    domain=config.zone_domain_no_port,
                    zone_file_path=config.coredns_zonefile_path,
                    request_domain=None,
                )
                # Delegated custom mail domain (optional, one NS record).
                custom_domain = config.email_custom_domain_normalized
                if custom_domain is not None:
                    delegation = config.custom_domain_delegation_record()
                    if delegation is not None:
                        logger.info(
                            f"Custom mail domain {custom_domain}: ensure this single NS record is set "
                            f"at the registrar to delegate it to this instance:  {delegation.as_display_line()}"
                        )
                    _provision_zone(
                        config,
                        client,
                        domain=custom_domain,
                        zone_file_path=config.coredns_custom_zonefile_path,
                        request_domain=custom_domain,
                    )
    except EmailProxyError as e:
        logger.warning(f"Email provisioning skipped: could not reach email proxy: {e}")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email provisioning skipped: {e}")
        return


def _provision_zone(
    config: Config,
    client: EmailProxyClient,
    *,
    domain: str,
    zone_file_path: Path,
    request_domain: str | None,
) -> None:
    """Ensure the SES identity for ``domain`` and write its records into ``zone_file_path``.

    ``request_domain`` is what we ask the proxy for: None means "the caller's own
    zone" (the proxy scopes it), while a concrete value is the delegated custom
    domain the proxy must be authorized to create an identity for.
    """
    result = client.ensure_identity(request_domain)
    dkim_cnames = [DkimCname(name=r.name, target=r.value) for r in result.dkim_records]
    # Inbound: direct-to-instance (MX -> mail.<domain> -> instance IP) or SES.
    # Outbound always relays through SES regardless.
    if config.email_inbound_mode == "direct":
        inbound_mail_host: str | None = config.inbound_mail_host_for(domain)
        inbound_mail_ip: str | None = config.public_ip
        mail_from_host = ""  # unused in direct mode
    else:
        inbound_mail_host = None
        inbound_mail_ip = None
        assert config.email_inbound_mx_host is not None  # required for ses mode (validated)
        mail_from_host = config.email_inbound_mx_host
    apply_email_records(
        zone_file_path,
        domain,
        mail_from_host=mail_from_host,
        dkim_cnames=dkim_cnames,
        dmarc_rua=config.email_dmarc_rua,
        inbound_mail_host=inbound_mail_host,
        inbound_mail_ip=inbound_mail_ip,
    )
    logger.info(
        f"Published email DNS records for {domain} (mode={config.email_inbound_mode}, "
        f"{len(dkim_cnames)} DKIM CNAME(s); identity verified={result.verified})"
    )
