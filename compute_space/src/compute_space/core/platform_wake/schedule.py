from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import httpx
from cryptography import x509

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.runner import next_run_at
from compute_space.core.domains import effective_domains
from compute_space.core.logging import logger
from compute_space.core.platform_wake.client import WakeApi
from compute_space.core.tls.renewal import RENEW_BEFORE
from compute_space.db import get_db

# How often the schedule is recomputed and, if it changed, reported. The platform also wakes instances at least daily,
# so a missed report delays work by at most a day.
REPORT_INTERVAL = timedelta(minutes=10)

_changed = asyncio.Event()


def schedule_changed() -> None:
    _changed.set()


def _cert_renewals(config: Config, db: sqlite3.Connection) -> list[datetime]:
    out = []
    for domain in (d for d in effective_domains(db) if d.tls):
        cert_path, _key_path = config.cert_key_paths_for(domain.name_no_port)
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except (OSError, ValueError):
            continue
        out.append(cert.not_valid_after_utc - RENEW_BEFORE)
    return out


def next_wake_at(config: Config, db: sqlite3.Connection, now: datetime) -> datetime | None:
    """The next time this instance has scheduled work: the daily update, or a certificate entering its renewal
    window."""
    candidates = [t for t in _cert_renewals(config, db) if t > now]
    update = read_auto_update_config(db)
    if update.enabled:
        candidates.append(next_run_at(update.time_utc))
    return min(candidates, default=None)


def start_platform_wake_task(api: WakeApi) -> asyncio.Task[None]:
    async def _run() -> None:
        reported: datetime | None = None
        first = True
        while True:
            try:
                with closing(get_db()) as db:
                    wake_at = next_wake_at(get_config(), db, datetime.now(UTC))
                if first or wake_at != reported:
                    await api.set_wake_at(wake_at)
                    logger.info(f"reported next scheduled work to the platform: {wake_at}")
                    reported, first = wake_at, False
            except httpx.HTTPError as e:
                logger.warning(f"could not report the next scheduled work to the platform: {e}")
            except Exception:
                logger.exception("computing the next scheduled work failed")
            try:
                await asyncio.wait_for(_changed.wait(), REPORT_INTERVAL.total_seconds())
            except TimeoutError:
                pass
            _changed.clear()

    return asyncio.create_task(_run(), name="platform-wake")
