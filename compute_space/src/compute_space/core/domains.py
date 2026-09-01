from __future__ import annotations

import sqlite3
from enum import StrEnum

import attr

from compute_space.core.logging import logger
from compute_space.core.settings_store import LEGACY_DOMAIN_ASSET_OWNER_KEY
from compute_space.core.settings_store import get_setting


def _lowercase(s: str) -> str:
    # mypy can't handle str.lower apparently
    return s.lower()


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


class DomainNotFoundError(ValueError):
    pass


class PrimaryDomainChangedError(RuntimeError):
    def __init__(self, current_primary: str) -> None:
        super().__init__(f"primary domain is now {current_primary}")
        self.current_primary = current_primary


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
    """Look up a domain case- and port-insensitively, matching ``Domain.owns`` semantics."""
    name_no_port = name.split(":")[0].lower()
    for row in db.execute(f"SELECT {_COLS} FROM domains"):
        if str(row["name"]).split(":")[0].lower() == name_no_port:
            return _row_to_record(row)
    return None


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


def remove_non_primary_record(db: sqlite3.Connection, name: str) -> bool:
    """Delete a domain only while it is non-primary, closing the promotion/removal race."""
    cur = db.execute("DELETE FROM domains WHERE name = ? AND is_primary = 0", (name.lower(),))
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


def legacy_domain_asset_owner(db: sqlite3.Connection) -> str | None:
    """Domain that owns the legacy primary cert and zone paths.

    Before the first primary-domain change this is the current primary. The change transaction
    persists that original owner so later promotions only change the canonical domain, never which
    domain's certificate and DNS zone live in the legacy paths.
    """
    if owner := get_setting(db, LEGACY_DOMAIN_ASSET_OWNER_KEY):
        return owner.lower()
    primary = primary_domain_or_none(db)
    return primary.name_no_port if primary is not None else None


def domain_uses_legacy_paths(db: sqlite3.Connection, name: str) -> bool:
    owner = legacy_domain_asset_owner(db)
    return owner is not None and owner == name.split(":")[0].lower()


def set_primary_domain(db: sqlite3.Connection, name: str, expected_primary: str) -> bool:
    """Atomically make an existing domain primary; return whether anything changed.

    ``expected_primary`` provides compare-and-swap semantics for the settings UI. The original
    primary remains the owner of legacy certificate and zone files, allowing the DB role change to
    commit without any filesystem operation or service reload.
    """
    normalized_name = name.lower()
    normalized_expected = expected_primary.split(":")[0].lower()
    db.execute("BEGIN IMMEDIATE")
    try:
        target = get_record(db, normalized_name)
        if target is None:
            raise DomainNotFoundError(normalized_name)

        current = db.execute(f"SELECT {_COLS} FROM domains WHERE is_primary = 1").fetchone()
        if current is None:
            raise RuntimeError("No primary domain configured")
        current_name = str(current["name"])
        current_name_no_port = current_name.split(":")[0]
        target_name = target.name
        if current_name_no_port == target.to_domain().name_no_port:
            db.execute("COMMIT")
            return False
        if current_name_no_port != normalized_expected:
            raise PrimaryDomainChangedError(current_name_no_port)

        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (LEGACY_DOMAIN_ASSET_OWNER_KEY, current_name_no_port),
        )
        demoted = db.execute("UPDATE domains SET is_primary = 0 WHERE name = ? AND is_primary = 1", (current_name,))
        promoted = db.execute("UPDATE domains SET is_primary = 1 WHERE name = ? AND is_primary = 0", (target_name,))
        if demoted.rowcount != 1 or promoted.rowcount != 1:
            raise RuntimeError("Primary domain changed concurrently")
        db.execute("COMMIT")
        return True
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


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
