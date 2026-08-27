import datetime
import enum
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from cryptography import x509

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.domains import set_record_status
from compute_space.core.logging import logger
from compute_space.core.tls.provision import acquire_cert_for_domain
from compute_space.core.tls.provision import provision_cert
from compute_space.db import get_db

# Renew well before expiry so transient ACME/DNS failures have days of retries left, not hours.
RENEW_BEFORE = datetime.timedelta(days=7)
CHECK_INTERVAL = datetime.timedelta(hours=12)
RETRY_INTERVAL = datetime.timedelta(hours=1)

# First failures back off from seconds, not straight to an hour.  This thread owns the *first*
# acquisition too, and at boot it can legitimately fail a few times in a row: when an app provides
# the ``dns`` service, that container is still starting.  An hour of no cert over a few seconds of
# startup skew would be a poor trade.
INITIAL_RETRY_INTERVAL = datetime.timedelta(seconds=15)


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
        with closing(get_db()) as db:
            for domain in effective_domains(db):
                if not domain.tls:
                    continue
                name = domain.name_no_port
                record = get_record(db, name)
                if record is None or record.cert_status == DomainCertStatus.ACTIVE:
                    continue
                cert_path, key_path = config.cert_key_paths_for(db, name)
                if get_cert_status(cert_path, key_path) == CertStatus.OK:
                    set_record_status(db, name, DomainCertStatus.ACTIVE)
    except Exception:
        logger.exception("cert_status display sync skipped (non-fatal)")


def _mark_cert_active(name: str) -> None:
    """Best-effort: record a freshly (re)acquired cert as active now, rather than waiting for the next
    cycle's ``_sync_cert_statuses``.  Display-only, so a DB error is logged and swallowed."""
    try:
        with closing(get_db()) as db:
            set_record_status(db, name, DomainCertStatus.ACTIVE)
    except Exception:
        logger.exception(f"cert_status active-mark skipped for {name} (non-fatal)")


def renew_cert_if_needed(
    config: Config,
    reload_caddy: Callable[[Config, sqlite3.Connection], object],
    provision: Callable[[Config, sqlite3.Connection], None] = provision_cert,
    acquire: Callable[[Config, str, Path, Path, sqlite3.Connection], None] = acquire_cert_for_domain,
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

    with closing(get_db()) as db:
        primary = primary_domain_or_none(db)
        # Primary — legacy cert paths + injectable ``provision``, but only when the primary is itself a
        # TLS domain (a non-TLS/.local primary has no cert to provision).  A failure here propagates.
        if primary is not None and primary.tls:
            status = get_cert_status(config.tls_cert_path, config.tls_key_path)
            if status != CertStatus.OK:
                logger.info(f"TLS cert for {primary.name} is {status.value}; renewing")
                provision(config, db)
                _mark_cert_active(primary.name_no_port)
                renewed = True

        # Additional TLS domains — per-domain paths, each isolated so one bad domain doesn't block
        # the rest (or the already-renewed primary's Caddy restart).
        primary_no_port = primary.name_no_port if primary else None
        for domain in effective_domains(db):
            name = domain.name_no_port
            if not domain.tls or name == primary_no_port:
                continue
            cert_path, key_path = config.cert_path_for(name, False), config.key_path_for(name, False)
            status = get_cert_status(cert_path, key_path)
            if status == CertStatus.OK:
                continue
            try:
                logger.info(f"TLS cert for {name} is {status.value}; renewing")
                cert_path.parent.mkdir(parents=True, exist_ok=True)
                acquire(config, name, cert_path, key_path, db)
                _mark_cert_active(name)
                renewed = True
            except Exception:
                logger.exception(f"TLS cert renewal failed for {name}; will retry next cycle")

        if renewed:
            reload_caddy(config, db)
    return renewed


def start_renewal_thread(reload_caddy: Callable[[Config, sqlite3.Connection], object]) -> threading.Thread:
    """Acquire and renew certs in a daemon thread.

    This owns the *first* acquisition as well as renewals — boot does not wait for a cert, so until
    the first pass succeeds Caddy serves the domain with its internal CA.

    Reads the *live* active config each cycle (``get_config()``), so a domain added at runtime via
    /api/domains after startup is picked up by renewal rather than frozen out by a stale snapshot.
    ``reload_caddy`` regenerates the Caddyfile so a renewed/newly-acquired cert is actually served.
    """

    def _loop() -> None:
        retry = INITIAL_RETRY_INTERVAL
        while True:
            try:
                renew_cert_if_needed(get_config(), reload_caddy)
            except Exception:
                logger.exception(f"TLS cert acquisition failed; retrying in {retry}")
                time.sleep(retry.total_seconds())
                # Back off towards the steady-state retry so a persistently broken setup isn't
                # hammering an ACME server or a registrar API.
                retry = min(retry * 2, RETRY_INTERVAL)
                continue
            retry = INITIAL_RETRY_INTERVAL
            time.sleep(CHECK_INTERVAL.total_seconds())

    thread = threading.Thread(target=_loop, name="tls-cert-renewal", daemon=True)
    thread.start()
    return thread
