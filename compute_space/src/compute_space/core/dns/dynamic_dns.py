"""Dynamic DNS: keep the instance's A records pointing at wherever it actually is.

Opt-in (``dynamic_dns_enabled``) — on a fixed address the polling is pure cost, but on a
connection that gets renumbered it is the only thing that brings the space back.

When the router serves DNS the address records are derived from the stored IP at render time, so
updating them is a re-render.  With an external provider there is nothing to re-render, so the
records are written through the ``dns`` service instead.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing

from compute_space.config import Config
from compute_space.core.dns.client import dns_client
from compute_space.core.dns.client import router_managed_domains
from compute_space.core.dns.client import uses_local_dns
from compute_space.core.dns.coredns_provider.coredns import reload_coredns_for_domains
from compute_space.core.dns.public_ip import detect_public_ip
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import store_public_ip
from compute_space.core.logging import logger

_DEFAULT_INTERVAL_SECONDS = 300.0

# Much shorter than the zone default: pointless to poll every few minutes if resolvers cache the
# old address for an hour.
_DYNAMIC_TTL_SECONDS = 60


def check_once(config: Config, db: sqlite3.Connection) -> str | None:
    """Detect the public IP and, if it moved, store it and re-point the address records.

    Returns the new address when something changed.  A detection failure is not a change: stale
    records beat pointing the space at nothing.
    """
    detected = detect_public_ip()
    if detected is None or detected == effective_public_ip(config, db):
        return None

    logger.info(f"Public IP changed to {detected}")
    store_public_ip(db, detected)

    if uses_local_dns(db):
        # Re-rendering *is* the update: the address records come from the stored IP.  Writing them
        # as records too would duplicate what the template emits.  The Corefile's bind address
        # derives from the IP as well, so CoreDNS has to come back on the new one.
        reload_coredns_for_domains(config, db)
    else:
        with dns_client(config, db) as dns:
            for domain in router_managed_domains(db):
                dns.set_address(domain, detected, ttl=_DYNAMIC_TTL_SECONDS)
    return detected


def start_dynamic_dns_thread(
    config: Config,
    open_db: Callable[[], sqlite3.Connection],
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> threading.Thread:
    """A fresh DB connection per tick: sqlite3 connections aren't shareable across threads, and a
    long-lived one would hold a handle open across the sleep."""

    def loop() -> None:
        logger.info(f"Dynamic DNS watcher started (every {interval_seconds:.0f}s)")
        while True:
            time.sleep(interval_seconds)
            try:
                with closing(open_db()) as db:
                    check_once(config, db)
            except Exception:
                # A transient provider or network error must not kill the watcher.
                logger.exception("Dynamic DNS check failed; retrying at the next interval")

    thread = threading.Thread(target=loop, name="dynamic-dns", daemon=True)
    thread.start()
    return thread
