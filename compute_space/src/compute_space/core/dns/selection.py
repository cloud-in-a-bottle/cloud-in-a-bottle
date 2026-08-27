"""Pick the DnsBackend this instance's own DNS writes should go through.

The choice is the ordinary service-default for the ``dns`` service, not a separate config knob,
so there is a single answer to "where does this space's DNS live" instead of two settings that
can disagree.  The router is the *implicit* provider: it has no row in ``apps`` (and
``service_defaults.app_id`` is a foreign key into it), so rather than registering itself, it is
what you get when no app has claimed the service.  Installing a connector app registers it as the
default in the ordinary way and switches the whole space over, including the router's own writes.

The router's own provider is dispatched in-process rather than over HTTP — there is no point
making the router talk to itself through loopback — so a local default yields
``LocalZoneFileBackend`` directly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from compute_space.config import Config
from compute_space.core.dns.backend import DnsBackend
from compute_space.core.dns.local import LocalZoneFileBackend
from compute_space.core.dns.remote import ServiceDnsBackend
from compute_space.core.dns.remote import domains_for_grants
from compute_space.core.dns.service import DNS_SERVICE_URL
from compute_space.core.dns.service import DNS_SERVICE_VERSION
from compute_space.core.dns.service import ROUTER_DNS_PROVIDER_ID
from compute_space.core.services_v2 import resolve_provider


def dns_provider_id(db: sqlite3.Connection) -> str:
    """The app id providing the ``dns`` service, defaulting to the router's own implementation."""
    row = db.execute("SELECT app_id FROM service_defaults WHERE service_url = ?", (DNS_SERVICE_URL,)).fetchone()
    return row["app_id"] if row else ROUTER_DNS_PROVIDER_ID


def uses_local_dns(db: sqlite3.Connection) -> bool:
    """True when this instance answers its own DNS, so CoreDNS must serve the public zones."""
    return dns_provider_id(db) == ROUTER_DNS_PROVIDER_ID


@contextmanager
def dns_backend(config: Config, db: sqlite3.Connection) -> Iterator[DnsBackend]:
    """The backend for the router's own record writes, closed on exit.

    A context manager because the remote backend holds an HTTP client; the local one has nothing
    to release and ignores the close.
    """
    provider_id = dns_provider_id(db)
    if provider_id == ROUTER_DNS_PROVIDER_ID:
        yield LocalZoneFileBackend.create(config, db)
        return

    _, port, _, endpoint = resolve_provider(
        DNS_SERVICE_URL, f">={DNS_SERVICE_VERSION}", db, provider_app_id=provider_id
    )
    backend = ServiceDnsBackend.create(port=port, endpoint=endpoint, domains=domains_for_grants(db))
    try:
        yield backend
    finally:
        backend.close()
