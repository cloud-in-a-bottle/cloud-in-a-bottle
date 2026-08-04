from __future__ import annotations

import sqlite3
from enum import StrEnum

import attr

from compute_space.core.logging import logger


def _lowercase(s: str) -> str:
    # mypy can't handle str.lower apparently
    return s.lower()


def is_local_name(name: str) -> bool:
    """True if ``name`` (port stripped) is an mDNS ``.local`` name — the one place that decides it."""
    n = name.split(":")[0].lower()
    return n == "local" or n.endswith(".local")


@attr.s(auto_attribs=True, frozen=True)
class Domain:
    """One hostname the instance answers on, with its scheme and discovery method.  The set lives in
    the DB ``domains`` table below; the primary (``primary_domain``) is the canonical domain used by
    background tasks and outbound links."""

    # the domain name, eg `host.example.com` or `myhost.local`; may optionally
    # include a non-80/443 port.
    name: str = attr.ib(converter=_lowercase)
    # served over TLS (https)?  Public domains: True; mDNS `.local`: False (plain http).
    tls: bool = False
    # published via the built-in wildcard mDNS responder (`.local`) rather than public DNS?
    mdns: bool = False

    @property
    def name_no_port(self) -> str:
        return self.name.split(":")[0]

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    @property
    def is_local(self) -> bool:
        """An mDNS ``.local`` name: CoreDNS resolves it to the LAN IP and it never gets a public cert."""
        return is_local_name(self.name)

    def owns(self, host: str) -> bool:
        """True if ``host`` is this domain itself or one of its ``<app>.<domain>`` subdomains.
        ``host`` may include a ``:port``; it's compared port-insensitively."""
        name = self.name_no_port
        if not name:
            # An empty name would make `endswith("." + name)` match any trailing-dot host.
            return False
        host_no_port = host.split(":")[0].lower()
        return host_no_port == name or host_no_port.endswith("." + name)

    def is_app_subdomain(self, host: str) -> bool:
        """True if ``host`` is an ``<app>.<domain>`` subdomain of this domain, not the domain itself."""
        return self.owns(host) and host.split(":")[0].lower() != self.name_no_port

    @classmethod
    def match(cls, db: sqlite3.Connection, host: str) -> Domain | None:
        """The configured Domain that owns ``host`` — the domain itself (the router) or one of its
        ``<app>.<domain>`` subdomains — or None if none match.

        Longest domain name wins, so overlapping domains resolve to the most specific (e.g.
        ``host.example.com`` beats a hypothetical ``example.com``)."""
        best: Domain | None = None
        for domain in effective_domains(db):
            if domain.owns(host) and (best is None or len(domain.name_no_port) > len(best.name_no_port)):
                best = domain
        return best


class DomainCertStatus(StrEnum):
    """Per-domain cert/acquisition status surfaced by /api/domains."""

    NONE = "none"  # TLS domain with no cert yet acquired
    ACQUIRING = "acquiring"  # acquisition in flight (served via `tls internal` meanwhile)
    ACTIVE = "active"  # cert in place (or non-TLS domain — nothing to acquire, serving http)
    ERROR = "error"  # acquisition failed (see error_message)


_COLS = "name, tls, mdns, is_primary, cert_status, error_message"


@attr.s(auto_attribs=True, frozen=True)
class DomainRecord:
    """A row of the ``domains`` table: the Domain fields, the primary flag, and the cert-acquisition
    status shown in the dashboard/API."""

    name: str
    tls: bool
    mdns: bool
    cert_status: DomainCertStatus = DomainCertStatus.NONE
    error_message: str | None = None
    is_primary: bool = False

    def to_domain(self) -> Domain:
        return Domain(name=self.name, tls=self.tls, mdns=self.mdns)


def _row_to_record(row: sqlite3.Row) -> DomainRecord:
    return DomainRecord(
        name=row["name"],
        tls=bool(row["tls"]),
        mdns=bool(row["mdns"]),
        cert_status=DomainCertStatus(row["cert_status"]),
        error_message=row["error_message"],
        is_primary=bool(row["is_primary"]),
    )


def load_records(db: sqlite3.Connection) -> tuple[DomainRecord, ...]:
    """Every domain, primary first."""
    rows = db.execute(f"SELECT {_COLS} FROM domains ORDER BY is_primary DESC, name").fetchall()
    return tuple(_row_to_record(r) for r in rows)


def get_record(db: sqlite3.Connection, name: str) -> DomainRecord | None:
    row = db.execute(f"SELECT {_COLS} FROM domains WHERE name = ?", (name.lower(),)).fetchone()
    return _row_to_record(row) if row is not None else None


def upsert_record(db: sqlite3.Connection, record: DomainRecord) -> None:
    """Insert a domain, or replace an existing one's fields (never its primary flag — the primary is
    set only by seeding).  Single atomic statement."""
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


def remove_record(db: sqlite3.Connection, name: str) -> bool:
    """Delete a domain; True if a row was removed.  (Refusing to remove the primary is the API's job.)"""
    cur = db.execute("DELETE FROM domains WHERE name = ?", (name.lower(),))
    db.commit()
    return cur.rowcount > 0


def set_record_status(
    db: sqlite3.Connection, name: str, cert_status: DomainCertStatus, error_message: str | None = None
) -> None:
    """Update a domain's cert status in one atomic UPDATE (no read-modify-write)."""
    db.execute(
        "UPDATE domains SET cert_status = ?, error_message = ? WHERE name = ?",
        (cert_status, error_message, name.lower()),
    )
    db.commit()


# --- domain queries --------------------------------------------------------------------------


def effective_domains(db: sqlite3.Connection) -> tuple[Domain, ...]:
    """The full domain set as ``Domain``s, primary first."""
    return tuple(r.to_domain() for r in load_records(db))


def primary_domain_or_none(db: sqlite3.Connection) -> Domain | None:
    """The canonical (primary) domain from the DB, or None if the table is unseeded."""
    row = db.execute(f"SELECT {_COLS} FROM domains WHERE is_primary = 1").fetchone()
    return _row_to_record(row).to_domain() if row is not None else None


def primary_domain(db: sqlite3.Connection) -> Domain:
    """The canonical (primary) domain from the DB.  Raises if unseeded — seed_first_boot must run first."""
    primary = primary_domain_or_none(db)
    if primary is None:
        raise RuntimeError("No primary domain in the DB `domains` table — seed_first_boot must run first.")
    return primary


def is_primary_domain(db: sqlite3.Connection, name: str) -> bool:
    """True if ``name`` (port stripped) is the DB's primary domain."""
    primary = primary_domain_or_none(db)
    return primary is not None and name.split(":")[0] == primary.name_no_port


# --- first-boot seeding ----------------------------------------------------------------------


def seed_domains(db: sqlite3.Connection, primary: Domain, extras: list[DomainRecord]) -> bool:
    """Populate an empty ``domains`` table once: ``primary`` (``is_primary=1``) plus ``extras``
    (non-primary), de-duplicated by host (primary wins).  Returns True if it seeded, False if the
    table already had rows (the DB is authoritative once seeded, so this is safe on every boot)."""
    if db.execute("SELECT 1 FROM domains LIMIT 1").fetchone() is not None:
        return False
    seen: set[str] = {primary.name_no_port}
    rows: list[tuple[str, int, int, int, str, str | None]] = [
        (primary.name, int(primary.tls), int(primary.mdns), 1, DomainCertStatus.NONE, None)
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
