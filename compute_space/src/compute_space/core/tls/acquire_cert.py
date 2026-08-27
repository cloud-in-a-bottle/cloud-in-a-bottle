import asyncio
import os
from pathlib import Path

from compute_space.core.dns.backend import DnsBackend
from compute_space.core.logging import logger
from compute_space.core.tls.util import _acquire_cert_dns01
from compute_space.core.tls.util import load_account_key

GTS_PRODUCTION = "https://dv.acme-v02.api.pki.goog/directory"


def write_cert_and_key(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """Write the cert chain and private key to disk, locking the key to 0600.

    This is the single cert-install path used by every cert provider so Caddy
    always finds the pair at the same place with the same permissions.
    """
    with open(cert_path, "wb") as f:
        f.write(cert_pem)
    # Create the key with 0o600 from the start so the private key is never
    # briefly readable by other users between write and chmod.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)


async def acquire_tls_cert(
    domain: str,
    cert_path: Path,
    key_path: Path,
    acme_account_key_path: Path,
    backend: DnsBackend,
    directory_url: str | None = None,
    verify_ssl: bool = True,
    acme_email: str | None = None,
) -> None:

    logger.info(f"Acquiring TLS certificate for {domain}...")

    account_key = load_account_key(acme_account_key_path)
    logger.info(f"Loaded ACME account key from {acme_account_key_path}")

    if directory_url is None:
        directory_url = GTS_PRODUCTION
    domains = [domain, f"*.{domain}"]
    logger.info(f"Requesting wildcard TLS cert for {domains} from {directory_url} (DNS-01)")
    cert_pem, key_pem = await asyncio.to_thread(
        _acquire_cert_dns01,
        domains=domains,
        directory_url=directory_url,
        backend=backend,
        account_key=account_key,
        verify_ssl=verify_ssl,
        acme_email=acme_email,
    )

    logger.info(f"TLS cert acquired for {domain}, writing to {cert_path} and {key_path}")

    write_cert_and_key(cert_path, key_path, cert_pem, key_pem)
