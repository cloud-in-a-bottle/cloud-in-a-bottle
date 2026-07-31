"""Provision the instance's email DNS records at startup.

When email is enabled, this:
  1. authenticates to the email proxy with the instance's shared Imbue identity,
  2. asks the proxy to create/ensure the SES domain identity and return the
     DKIM CNAME records SES requires,
  3. writes those DKIM records plus SPF/DMARC/MX into the CoreDNS zone.

This runs for the instance's built-in zone (the DB primary domain), and — when the
owner has delegated a custom mail domain (email_custom_domain) to this instance with
an NS record — also for that custom zone, so a single NS record is all it takes to
send/receive as the custom domain.

Runs after start_coredns on each boot (the zone file is regenerated from template
there, so the email records must be re-applied every time). Best-effort: a proxy or
SES hiccup logs and returns rather than blocking router startup — mail is not
load-bearing for the instance coming up.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.core.dns import DkimCname
from compute_space.core.dns import apply_email_records
from compute_space.core.domains import is_primary_domain
from compute_space.core.domains import primary_domain
from compute_space.core.email.enablement import email_enabled
from compute_space.core.email.enablement import resolve_email_identity
from compute_space.core.email.proxy_client import EmailProxyClient
from compute_space.core.email.proxy_client import EmailProxyError
from compute_space.core.logging import logger
from compute_space.core.tls.keycloak import KeycloakTokenProvider


def provision_email_records(config: Config, db: sqlite3.Connection) -> None:
    """Create the SES identity/identities and publish email DNS records.

    Provisions the instance's built-in zone, and the delegated custom mail domain
    when one is configured. No-op when email is not enabled. Past the guard, the
    email prerequisites (proxy URL, Imbue identity, public IP) are all present.
    """
    if not email_enabled(config, db):
        return
    assert config.email_proxy_base_url is not None
    credentials = resolve_email_identity(config, db)
    assert credentials is not None
    # Inbound is always direct-to-instance, so the MX/A records need the instance's
    # public IP (guaranteed non-None by email_enabled).
    assert config.public_ip is not None

    primary = primary_domain(db)
    zone = primary.name_no_port
    try:
        with KeycloakTokenProvider.create(credentials) as token_provider:
            with EmailProxyClient.create(config.email_proxy_base_url, token_provider) as client:
                # Built-in zone: the proxy defaults to the caller's zone when no
                # domain is passed.
                _ensure_identity_and_publish_records(
                    config,
                    db,
                    client,
                    domain=zone,
                    request_domain=None,
                )
                # Delegated custom mail domain (optional, one NS record).
                custom_domain = config.email_custom_domain_normalized
                if custom_domain is not None:
                    delegation = config.custom_domain_delegation_record(zone)
                    if delegation is not None:
                        logger.info(
                            f"Custom mail domain {custom_domain}: ensure this single NS record is set "
                            f"at the registrar to delegate it to this instance:  {delegation.as_display_line()}"
                        )
                    _ensure_identity_and_publish_records(
                        config,
                        db,
                        client,
                        domain=custom_domain,
                        request_domain=custom_domain,
                    )
    except EmailProxyError as e:
        logger.warning(f"Email provisioning skipped: could not reach email proxy: {e}")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email provisioning skipped: {e}")
        return


def _ensure_identity_and_publish_records(
    config: Config,
    db: sqlite3.Connection,
    client: EmailProxyClient,
    *,
    domain: str,
    request_domain: str | None,
) -> None:
    """Ensure the SES identity for ``domain`` and write its records into ``domain``'s zone file.

    ``request_domain`` is what we ask the proxy for: None means "the caller's own
    zone" (the proxy scopes it), while a concrete value is the delegated custom
    domain the proxy must be authorized to create an identity for.
    """
    result = client.ensure_identity(request_domain)
    dkim_cnames = [DkimCname(name=r.name, target=r.value) for r in result.dkim_records]
    zone_file_path = config.coredns_zonefile_path_for(domain, is_primary_domain(db, domain))
    # Inbound is always direct-to-instance: MX -> mail.<domain> -> instance IP, so
    # mail is delivered straight to the instance's own mail server (never through
    # OpenHost infra). Outbound relays through SES regardless.
    assert config.public_ip is not None  # guaranteed by email_enabled
    apply_email_records(
        zone_file_path,
        domain,
        inbound_mail_host=config.inbound_mail_host_for(domain),
        inbound_mail_ip=config.public_ip,
        dkim_cnames=dkim_cnames,
        dmarc_rua=config.email_dmarc_rua,
    )
    logger.info(
        f"Published email DNS records for {domain} "
        f"({len(dkim_cnames)} DKIM CNAME(s); identity verified={result.verified})"
    )
