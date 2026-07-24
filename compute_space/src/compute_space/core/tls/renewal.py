import datetime
import enum
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography import x509

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.logging import logger
from compute_space.core.tls.provision import acquire_cert_for_domain
from compute_space.core.tls.provision import provision_cert

# Renew well before expiry so transient ACME/DNS failures have days of retries left, not hours.
RENEW_BEFORE = datetime.timedelta(days=7)
CHECK_INTERVAL = datetime.timedelta(hours=12)
RETRY_INTERVAL = datetime.timedelta(hours=1)


class CertStatus(enum.Enum):
    MISSING = "missing"
    EXPIRED = "expired"
    EXPIRING_SOON = "expiring_soon"
    OK = "ok"


def get_cert_status(cert_path: Path, key_path: Path, now: datetime.datetime | None = None) -> CertStatus:
    if not cert_path.exists() or not key_path.exists():
        return CertStatus.MISSING
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except ValueError:
        # An unreadable cert can't be served; re-acquiring is the remedy, same as expired.
        logger.warning(f"Could not parse TLS cert at {cert_path}; treating it as expired")
        return CertStatus.EXPIRED
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    expires_at = cert.not_valid_after_utc
    if expires_at <= now:
        return CertStatus.EXPIRED
    if expires_at <= now + RENEW_BEFORE:
        return CertStatus.EXPIRING_SOON
    return CertStatus.OK


def renew_cert_if_needed(
    config: Config,
    restart_caddy: Callable[[], None],
    provision: Callable[[Config], None] = provision_cert,
    acquire: Callable[[Config, str, Path, Path], None] = acquire_cert_for_domain,
) -> bool:
    """Renew every TLS cert that is missing, expired, or inside the renewal window — the primary
    and each additional TLS domain — restarting Caddy once if anything was renewed.

    The primary keeps its legacy cert paths and dedicated ``provision`` routine (behavior
    unchanged).  Each additional TLS domain uses its own ``certs/<name>`` paths; a failure on one
    (e.g. its DNS isn't delegated to this instance) is logged and skipped so it can't starve the
    primary or the other domains.  Because this re-acquires any non-OK cert, it also re-drives a
    domain left mid-acquisition by a restart.  Returns True if any cert was (re)installed.
    """
    renewed = False

    # Primary — unchanged: legacy cert paths + the injectable ``provision`` routine.  A failure
    # here propagates (the renewal thread catches it and retries sooner), as before.
    status = get_cert_status(config.tls_cert_path, config.tls_key_path)
    if status != CertStatus.OK:
        logger.info(f"TLS cert for {config.primary_domain.name} is {status.value}; renewing")
        provision(config)
        renewed = True

    # Additional TLS domains — per-domain paths, each isolated so one bad domain doesn't block
    # the rest (or the already-renewed primary's Caddy restart).
    for domain in config.all_domains:
        name = domain.name_no_port
        if not domain.tls or name == config.primary_domain.name_no_port:
            continue
        cert_path, key_path = config.cert_path_for(name), config.key_path_for(name)
        status = get_cert_status(cert_path, key_path)
        if status == CertStatus.OK:
            continue
        try:
            logger.info(f"TLS cert for {name} is {status.value}; renewing")
            cert_path.parent.mkdir(parents=True, exist_ok=True)
            acquire(config, name, cert_path, key_path)
            renewed = True
        except Exception:
            logger.exception(f"TLS cert renewal failed for {name}; will retry next cycle")

    if renewed:
        restart_caddy()
    return renewed


def start_renewal_thread(restart_caddy: Callable[[], None]) -> threading.Thread:
    """Run renew_cert_if_needed periodically in a daemon thread, retrying sooner after failures.

    Reads the *live* active config each cycle (``get_config()``), so a domain added at runtime via
    /api/domains after startup is picked up by renewal rather than frozen out by a stale snapshot.
    """

    def _loop() -> None:
        while True:
            interval = CHECK_INTERVAL
            try:
                renew_cert_if_needed(get_config(), restart_caddy)
            except Exception:
                logger.exception(f"TLS cert renewal failed; retrying in {RETRY_INTERVAL}")
                interval = RETRY_INTERVAL
            time.sleep(interval.total_seconds())

    thread = threading.Thread(target=_loop, name="tls-cert-renewal", daemon=True)
    thread.start()
    return thread
