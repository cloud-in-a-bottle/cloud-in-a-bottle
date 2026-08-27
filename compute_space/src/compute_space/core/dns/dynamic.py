"""Dynamic DNS: keep the instance's A records pointing at wherever it actually is.

Opt-in (``dynamic_dns_enabled``), because on a VPS with a fixed address the polling is pure cost.
On a home server it is what makes the space reachable at all after the ISP renumbers.

The update goes through whichever DnsBackend is configured, so it works the same whether records
live in the local CoreDNS zone files or at an external registrar.  These are router-owned records
(apex, ``ns``, wildcard), so this path writes them directly rather than through the service
handler, which reserves them precisely so nothing else moves them out from under this loop.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing

from compute_space.config import Config
from compute_space.core.dns.backend import split_fqdn
from compute_space.core.dns.coredns import reload_coredns_for_domains
from compute_space.core.dns.public_ip import detect_public_ip
from compute_space.core.dns.public_ip import effective_public_ip
from compute_space.core.dns.public_ip import store_public_ip
from compute_space.core.dns.records import APEX
from compute_space.core.dns.records import DnsRecord
from compute_space.core.dns.remote import domains_for_grants
from compute_space.core.dns.selection import dns_backend
from compute_space.core.dns.selection import uses_local_dns
from compute_space.core.logging import logger

# Router-owned names whose A record follows the instance.  "ns" only matters for the local
# backend (it is the glue target for the delegation), but writing it everywhere is harmless and
# keeps the two backends producing the same zone contents.
_ROUTER_A_NAMES = (APEX, "ns", "*")

_DEFAULT_INTERVAL_SECONDS = 300.0

# Records must resolve quickly after a move, so they carry a much shorter TTL than the zone
# default.  Pointless to poll every five minutes if resolvers cache the old address for an hour.
_DYNAMIC_TTL_SECONDS = 60


def update_public_ip_records(config: Config, db: sqlite3.Connection, ip: str) -> None:
    """Point every managed domain's router-owned A records at ``ip``."""
    with dns_backend(config, db) as backend:
        for zone in backend.zones():
            records = [DnsRecord(name=n, type="A", ttl=_DYNAMIC_TTL_SECONDS, data=ip) for n in _names_for(zone, db)]
            backend.set_records(zone, records)
            logger.info(f"Updated {len(records)} A record(s) in {zone} to {ip}")


def _names_for(zone: str, db: sqlite3.Connection) -> list[str]:
    """The zone-relative names to update in ``zone``.

    With the local backend each domain is its own zone, so the names are just the router-owned
    labels.  With an external provider the zone may be a parent (``example.com`` holding
    ``host.example.com``), so each managed domain contributes its own prefixed set.
    """
    names: list[str] = []
    for domain in domains_for_grants(db):
        try:
            match = split_fqdn(domain, [zone])
        except Exception:
            continue  # this domain lives in a different zone
        base = "" if match.name == APEX else match.name
        for label in _ROUTER_A_NAMES:
            if label == APEX:
                names.append(base or APEX)
            else:
                names.append(f"{label}.{base}" if base else label)
    return list(dict.fromkeys(names))


def check_once(config: Config, db: sqlite3.Connection) -> str | None:
    """Detect the public IP and, if it moved, store it and rewrite the records.

    Returns the new address when something changed, None otherwise.  A detection failure is not a
    change: leaving stale records up beats pointing the space at nothing.
    """
    detected = detect_public_ip()
    if detected is None:
        return None
    current = effective_public_ip(config, db)
    if detected == current:
        return None

    logger.info(f"Public IP changed: {current} -> {detected}")
    store_public_ip(db, detected)
    update_public_ip_records(config, db, detected)

    if uses_local_dns(db):
        # The local zone files were just rewritten in place, but the Corefile's bind address is
        # derived from the IP too, so CoreDNS needs to come back on the new address.
        reload_coredns_for_domains(config, db)
    return detected


def start_dynamic_dns_thread(
    config: Config,
    open_db: Callable[[], sqlite3.Connection],
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> threading.Thread:
    """Run ``check_once`` on a loop in a daemon thread.

    Its own DB connection per iteration, since sqlite3 connections are not shareable across
    threads and a long-lived one would hold a handle open across the sleep.
    """

    def loop() -> None:
        logger.info(f"Dynamic DNS watcher started (every {interval_seconds:.0f}s)")
        while True:
            time.sleep(interval_seconds)
            try:
                with closing(open_db()) as db:
                    check_once(config, db)
            except Exception:
                # Never let a transient provider or network error kill the watcher; the next tick
                # is a free retry.
                logger.exception("Dynamic DNS check failed; retrying at the next interval")

    thread = threading.Thread(target=loop, name="dynamic-dns", daemon=True)
    thread.start()
    return thread
