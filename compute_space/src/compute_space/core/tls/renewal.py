import datetime
import enum
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography import x509

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.domain_store import CERT_STATUS_ACTIVE
from compute_space.core.domain_store import get_record
from compute_space.core.domain_store import set_record_status
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


def _sync_cert_statuses(config: Config) -> None:
    """Bring the DB ``cert_status`` in step with the certs actually on disk, so the stored column
    matches what the dashboard shows (it derives display status from the files).  Only upgrades a
    tracked TLS domain to ``active`` when its cert+key are present — chiefly the primary, whose legacy
    cert predates the ``domains`` table and was seeded ``none``.  Idempotent (skips rows already
    ``active``); acquiring/error states are left to the add-domain flow and the renewal below.

    Best-effort: this only keeps the *display* column honest, so any failure (a locked or not-yet-
    migrated DB) is logged and swallowed rather than allowed to break the actual cert renewal."""
    try:
        for domain in config.all_domains:
            if not domain.tls:
                continue
            name = domain.name_no_port
            record = get_record(config, name)
            if record is None or record.cert_status == CERT_STATUS_ACTIVE:
                continue
            if get_cert_status(config.cert_path_for(name), config.key_path_for(name)) == CertStatus.OK:
                set_record_status(config, name, CERT_STATUS_ACTIVE)
    except Exception:
        logger.exception("cert_status display sync skipped (non-fatal)")


def renew_cert_if_needed(
    config: Config,
    reload_caddy: Callable[[Config], object],
    provision: Callable[[Config], None] = provision_cert,
    acquire: Callable[[Config, str, Path, Path], None] = acquire_cert_for_domain,
) -> bool:
    """Renew every TLS cert that is missing, expired, or inside the renewal window.

    The primary keeps its legacy cert paths and dedicated ``provision`` routine (behavior
    unchanged).  Each additional TLS domain uses its own ``certs/<name>`` paths; a failure on one
    (e.g. its DNS isn't delegated to this instance) is logged and skipped so it can't starve the
    primary or the other domains.  Because this re-acquires any non-OK cert, it also re-drives a
    domain left mid-acquisition by a restart.  Returns True if any cert was (re)installed.
    """
    _sync_cert_statuses(config)  # keep the stored cert_status honest (e.g. the seeded-'none' primary)
    renewed = False

    # Primary — legacy cert paths + injectable ``provision``, but only when the primary is itself a
    # TLS domain (a non-TLS/.local primary has no cert to provision).  A failure here propagates.
    if config.primary_domain.tls:
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
        reload_caddy(config)
    return renewed


def start_renewal_thread(reload_caddy: Callable[[Config], object]) -> threading.Thread:
    """Run renew_cert_if_needed periodically in a daemon thread, retrying sooner after failures.

    Reads the *live* active config each cycle (``get_config()``), so a domain added at runtime via
    /api/domains after startup is picked up by renewal rather than frozen out by a stale snapshot.
    ``reload_caddy`` regenerates the Caddyfile so a renewed/newly-acquired cert is actually served.
    """

    def _loop() -> None:
        while True:
            interval = CHECK_INTERVAL
            try:
                renew_cert_if_needed(get_config(), reload_caddy)
            except Exception:
                logger.exception(f"TLS cert renewal failed; retrying in {RETRY_INTERVAL}")
                interval = RETRY_INTERVAL
            time.sleep(interval.total_seconds())

    thread = threading.Thread(target=_loop, name="tls-cert-renewal", daemon=True)
    thread.start()
    return thread
