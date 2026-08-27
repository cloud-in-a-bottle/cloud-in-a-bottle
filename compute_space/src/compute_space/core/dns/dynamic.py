"""Dynamic DNS: keep the instance's A records pointing at wherever it actually is.

Opt-in (``dynamic_dns_enabled``) — on a fixed address the polling is pure cost, but on a
connection that gets renumbered it is the only thing that brings the space back.

Updates go through whichever backend is configured, so this works the same for local zone files
and an external registrar.  These are router-owned records, written directly rather than through
the service handler, which reserves them so nothing else moves them out from under this loop.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing

from compute_space.config import Config
from compute_space.core.dns.backend import dns_backend
from compute_space.core.dns.backend import router_managed_domains
from compute_space.core.dns.backend import split_fqdn
from compute_space.core.dns.backend import uses_local_dns
from compute_space.core.dns.coredns import reload_coredns_for_domains
from compute_space.core.dns.public_ip import detect_public_ip
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import store_public_ip
from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.logging import logger

_ROUTER_A_NAMES = (APEX, "ns", "*")
_DEFAULT_INTERVAL_SECONDS = 300.0

# Much shorter than the zone default: pointless to poll every few minutes if resolvers cache the
# old address for an hour.
_DYNAMIC_TTL_SECONDS = 60


def check_once(config: Config, db: sqlite3.Connection) -> str | None:
    """Detect the public IP and, if it moved, store it and rewrite the records.

    Returns the new address when something changed.  A detection failure is not a change: stale
    records beat pointing the space at nothing.
    """
    detected = detect_public_ip()
    if detected is None or detected == effective_public_ip(config, db):
        return None

    logger.info(f"Public IP changed to {detected}")
    store_public_ip(db, detected)
    update_public_ip_records(config, db, detected)
    if uses_local_dns(db):
        # The zone files were rewritten in place, but the Corefile's bind address derives from the
        # IP too, so CoreDNS has to come back on the new one.
        reload_coredns_for_domains(config, db)
    return detected


def update_public_ip_records(config: Config, db: sqlite3.Connection, ip: str) -> None:
    with dns_backend(config, db) as backend:
        for zone in backend.zones():
            records = [DnsRecord(name=n, type="A", ttl=_DYNAMIC_TTL_SECONDS, data=ip) for n in _names_for(zone, db)]
            backend.set_records(zone, records)
            logger.info(f"Updated {len(records)} A record(s) in {zone} to {ip}")


def _names_for(zone: str, db: sqlite3.Connection) -> list[str]:
    """The zone-relative names to update.  With the local backend each domain is its own zone; with
    an external provider the zone may be a parent, so each domain contributes a prefixed set."""
    names: list[str] = []
    for domain in router_managed_domains(db):
        try:
            match = split_fqdn(domain, [zone])
        except Exception:
            continue  # this domain lives in a different zone
        base = "" if match.name == APEX else match.name
        names += [
            (base or APEX) if label == APEX else (f"{label}.{base}" if base else label) for label in _ROUTER_A_NAMES
        ]
    return list(dict.fromkeys(names))


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
