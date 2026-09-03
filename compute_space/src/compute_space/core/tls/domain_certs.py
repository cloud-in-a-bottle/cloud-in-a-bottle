import sqlite3

from compute_space.config import Config
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.domains import Domain
from compute_space.core.tls.provision import acquire_cert_for_domain
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import get_cert_status


async def ensure_cert_for(
    config: Config, domain: Domain, db: sqlite3.Connection, dns_provider: InternalDnsProvider
) -> None:
    """Idempotently ensure a usable TLS cert exists at the domain's stable path.

    The provisioning-time domain keeps the legacy top-level certificate paths; every other domain
    uses its named pair under ``certs/``. Promotion changes neither domain's path ownership.

    This is the single acquisition entry point shared by initial setup and later domain
    addition (via /api/domains).  No-op for non-TLS domains (mDNS ``.local`` is served over
    plain http and never touches ACME).  For a TLS domain it acquires a cert only when one is
    missing or expiring, so it's safe to call repeatedly.

    Requires a running DNS provider (DNS-01) and, for a *non-primary* public domain, that the
    domain's DNS is delegated to this instance's CoreDNS (or handled by the cert_api broker) —
    otherwise acquisition can't complete.  Callers surface that failure as the domain's cert status.
    """
    if not domain.tls:
        return
    name = domain.name_no_port
    cert_path, key_path = config.cert_key_paths_for(db, name)
    if get_cert_status(cert_path, key_path) == CertStatus.OK:
        return
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    await acquire_cert_for_domain(config, name, cert_path, key_path, db, dns_provider)
