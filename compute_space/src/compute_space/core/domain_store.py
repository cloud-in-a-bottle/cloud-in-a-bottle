"""The DB ``domains`` table is the single source of truth for the hostnames this instance answers on.

The set is loaded into the active ``Config`` (``rebuild_active_domains``) so routing, Caddy/CoreDNS
generation, and URL-building read it via ``config.all_domains`` with no per-request query.  Exactly
one row is the primary (``is_primary``); it supplies the canonical ``zone_domain``.  Mutations are
single SQL statements, so a load-all/save-all race isn't possible.

On first boot the table is empty; ``seed_first_boot`` (see ``core/first_boot.py``) populates it once
from ``first_boot.toml`` or the config-file ``zone_domain`` + ``[[openhost.domains]]``.  After that
those config-file fields are never read again.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import attr

from compute_space.config import Config
from compute_space.config import Domain
from compute_space.config import set_active_config
from compute_space.core.logging import logger

# Per-domain cert/acquisition status surfaced by /api/domains.
CERT_STATUS_NONE = "none"  # TLS domain with no cert yet acquired
CERT_STATUS_ACQUIRING = "acquiring"  # acquisition in flight (served via `tls internal` meanwhile)
CERT_STATUS_ACTIVE = "active"  # cert in place (or non-TLS domain — nothing to acquire, serving http)
CERT_STATUS_ERROR = "error"  # acquisition failed (see error_message)

_COLS = "name, tls, mdns, is_primary, cert_status, error_message"


@attr.s(auto_attribs=True, frozen=True)
class DomainRecord:
    """A row of the ``domains`` table: the Domain fields, the primary flag, and the cert-acquisition
    status shown in the dashboard/API."""

    name: str
    tls: bool
    mdns: bool
    cert_status: str = CERT_STATUS_NONE
    error_message: str | None = None
    is_primary: bool = False

    def to_domain(self) -> Domain:
        return Domain(name=self.name, tls=self.tls, mdns=self.mdns)


def _connect(config: Config) -> sqlite3.Connection:
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _row_to_record(row: sqlite3.Row) -> DomainRecord:
    return DomainRecord(
        name=row["name"],
        tls=bool(row["tls"]),
        mdns=bool(row["mdns"]),
        cert_status=row["cert_status"],
        error_message=row["error_message"],
        is_primary=bool(row["is_primary"]),
    )


def load_records(config: Config) -> tuple[DomainRecord, ...]:
    """Every domain, primary first."""
    with closing(_connect(config)) as db:
        rows = db.execute(f"SELECT {_COLS} FROM domains ORDER BY is_primary DESC, name").fetchall()
    return tuple(_row_to_record(r) for r in rows)


def get_record(config: Config, name: str) -> DomainRecord | None:
    with closing(_connect(config)) as db:
        row = db.execute(f"SELECT {_COLS} FROM domains WHERE name = ?", (name.lower(),)).fetchone()
    return _row_to_record(row) if row is not None else None


def upsert_record(config: Config, record: DomainRecord) -> None:
    """Insert a domain, or replace an existing one's fields (never its primary flag — the primary is
    set only by seeding).  Single atomic statement."""
    with closing(_connect(config)) as db:
        db.execute(
            f"INSERT INTO domains ({_COLS}) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET tls = excluded.tls, mdns = excluded.mdns, "
            "cert_status = excluded.cert_status, error_message = excluded.error_message",
            (
                record.name.lower(),
                int(record.tls),
                int(record.mdns),
                int(record.is_primary),
                record.cert_status,
                record.error_message,
            ),
        )
        db.commit()


def remove_record(config: Config, name: str) -> bool:
    """Delete a domain; True if a row was removed.  (Refusing to remove the primary is the API's job.)"""
    with closing(_connect(config)) as db:
        cur = db.execute("DELETE FROM domains WHERE name = ?", (name.lower(),))
        db.commit()
        return cur.rowcount > 0


def set_record_status(config: Config, name: str, cert_status: str, error_message: str | None = None) -> None:
    """Update a domain's cert status in one atomic UPDATE (no read-modify-write)."""
    with closing(_connect(config)) as db:
        db.execute(
            "UPDATE domains SET cert_status = ?, error_message = ? WHERE name = ?",
            (cert_status, error_message, name.lower()),
        )
        db.commit()


# --- active-config wiring --------------------------------------------------------------------


def effective_domains(config: Config) -> tuple[Domain, ...]:
    """The full domain set as ``Domain``s, primary first."""
    return tuple(r.to_domain() for r in load_records(config))


def rebuild_active_domains(config: Config) -> Config:
    """Load the domain set from the DB and swap it into the active config, so routing, Caddy
    generation, and URL-building immediately reflect it.  Returns the new config.

    The DB primary also drives the ``zone_domain`` / ``tls_enabled`` scalars (used by cert paths, the
    DNS zone, and the ``OPENHOST_ZONE_DOMAIN`` handed to apps), so a domain seeded from
    ``first_boot.toml`` takes effect everywhere — not just in routing.  When the table is empty
    (pre-seed only — startup runs the seed first) the active config keeps an empty domain set."""
    domains = effective_domains(config)
    if domains:
        primary = domains[0]
        new_config = config.evolve(domains=domains, zone_domain=primary.name, tls_enabled=primary.tls)
    else:
        new_config = config.evolve(domains=domains)
    set_active_config(new_config)
    return new_config


# --- first-boot / upgrade seeding ------------------------------------------------------------


def seed_domains(config: Config, primary: Domain, extras: list[DomainRecord]) -> bool:
    """Populate an empty ``domains`` table once: ``primary`` (``is_primary=1``) plus ``extras``
    (non-primary), de-duplicated by host (primary wins).  Returns True if it seeded, False if the
    table already had rows (the DB is authoritative once seeded, so this is safe on every boot)."""
    with closing(_connect(config)) as db:
        if db.execute("SELECT 1 FROM domains LIMIT 1").fetchone() is not None:
            return False
        seen: set[str] = {primary.name_no_port}
        rows: list[tuple[str, int, int, int, str, str | None]] = [
            (primary.name, int(primary.tls), int(primary.mdns), 1, CERT_STATUS_NONE, None)
        ]
        for rec in extras:
            host = rec.name.split(":")[0]
            if host in seen:
                continue
            seen.add(host)
            rows.append((rec.name, int(rec.tls), int(rec.mdns), 0, rec.cert_status, rec.error_message))
        db.executemany(f"INSERT INTO domains ({_COLS}) VALUES (?, ?, ?, ?, ?, ?)", rows)
        db.commit()
    logger.info("Seeded {} domain(s) into the DB", len(rows))
    return True


def seed_domains_from_legacy(config: Config) -> bool:
    """Seed from the config-file fields — the ``zone_domain`` primary plus any ``[[openhost.domains]]``.
    This is the path for an instance whose ``config.toml`` still carries ``zone_domain`` (fresh
    deploys seed from ``first_boot.toml`` instead).

    It is the ONLY place ``config.zone_domain`` is read: lifted into the DB here, after which the DB
    is authoritative."""
    extras = [DomainRecord(d.name, d.tls, d.mdns) for d in config.domains]
    if not config.zone_domain:
        # No legacy domain to migrate (e.g. an already-scrubbed config.toml).  Don't seed a bogus
        # empty primary; a fresh instance's domain comes from first_boot.toml / /setup instead.
        if not extras:
            return False
        primary, extras = extras[0].to_domain(), extras[1:]
    else:
        primary = Domain(name=config.zone_domain, tls=config.tls_enabled)
    return seed_domains(config, primary, extras)
