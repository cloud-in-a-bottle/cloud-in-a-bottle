from __future__ import annotations

import json
import sqlite3
from enum import StrEnum

import attr

from compute_space.core.logging import logger
from compute_space.core.settings_store import ARCHIVE_MIGRATION_IN_PROGRESS_KEY
from compute_space.core.settings_store import LEGACY_DOMAIN_ASSET_OWNER_KEY
from compute_space.core.settings_store import PRIMARY_DOMAIN_RESTART_APP_IDS_KEY
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


def host_with_request_port(host: str, request_netloc: str) -> str:
    """``host`` with the port the request arrived on (from ``request_netloc``) appended.

    Lets links behave over an SSH tunnel / NAT forward on a nonstandard port — we preserve
    that port instead of dropping the browser on the default one.  No port on the request
    (default 80/443) → ``host`` unchanged.
    """
    _, sep, port = request_netloc.rpartition(":")
    return f"{host}:{port}" if sep and port.isdigit() else host


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


class AppsBusyForPrimaryChangeError(RuntimeError):
    def __init__(self, app_names: tuple[str, ...]) -> None:
        super().__init__(f"apps are busy: {', '.join(app_names)}")
        self.app_names = app_names


class ArchiveMigrationInProgressError(RuntimeError):
    pass


@attr.s(auto_attribs=True, frozen=True)
class PrimaryDomainChange:
    changed: bool
    restart_app_ids: tuple[str, ...] = ()


_COLS = "name, tls, mdns, is_primary, cert_status, error_message"
PRIMARY_DOMAIN_APP_RESTART_MARKER = "Restarting after primary domain change"
INTERRUPTED_APP_REMOVAL_MESSAGE = "App removal was interrupted by a restart; retry removal."


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
    cur = db.execute(
        "DELETE FROM domains WHERE name = ? AND is_primary = 0 AND NOT EXISTS (SELECT 1 FROM settings WHERE key = ?)",
        (name.lower(), PRIMARY_DOMAIN_RESTART_APP_IDS_KEY),
    )
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


def pending_primary_domain_restart_app_ids(db: sqlite3.Connection) -> tuple[str, ...]:
    raw = get_setting(db, PRIMARY_DOMAIN_RESTART_APP_IDS_KEY)
    if raw is None:
        return ()
    app_ids = json.loads(raw)
    if not isinstance(app_ids, list) or any(not isinstance(app_id, str) for app_id in app_ids):
        raise ValueError("invalid pending primary-domain app restart state")
    return tuple(app_ids)


def complete_primary_domain_app_restart(db: sqlite3.Connection, app_id: str) -> None:
    """Remove one app from the durable primary-domain restart queue."""
    db.execute("BEGIN IMMEDIATE")
    try:
        remaining = [pending for pending in pending_primary_domain_restart_app_ids(db) if pending != app_id]
        if remaining:
            db.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (json.dumps(remaining), PRIMARY_DOMAIN_RESTART_APP_IDS_KEY),
            )
        else:
            db.execute("DELETE FROM settings WHERE key = ?", (PRIMARY_DOMAIN_RESTART_APP_IDS_KEY,))
        db.execute(
            "UPDATE apps SET error_message = NULL WHERE app_id = ? AND error_message = ?",
            (app_id, PRIMARY_DOMAIN_APP_RESTART_MARKER),
        )
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def set_primary_domain(db: sqlite3.Connection, name: str, expected_primary: str) -> PrimaryDomainChange:
    """Atomically make an existing domain primary and claim active apps for recreation.

    ``expected_primary`` provides compare-and-swap semantics for the settings UI. The original
    primary remains the owner of legacy certificate and zone files, allowing the DB role change to
    commit without any filesystem operation or service reload. Apps already stopped stay stopped;
    active apps enter a durable recreation queue processed one at a time after the response.
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
            pending = pending_primary_domain_restart_app_ids(db)
            db.execute("COMMIT")
            return PrimaryDomainChange(changed=False, restart_app_ids=pending)
        if current_name_no_port != normalized_expected:
            raise PrimaryDomainChangedError(current_name_no_port)

        pending = pending_primary_domain_restart_app_ids(db)
        if pending:
            rows = db.execute(
                f"SELECT name FROM apps WHERE app_id IN ({', '.join('?' for _ in pending)}) ORDER BY name",
                pending,
            ).fetchall()
            names = tuple(row["name"] for row in rows) or pending
            raise AppsBusyForPrimaryChangeError(names)
        if get_setting(db, ARCHIVE_MIGRATION_IN_PROGRESS_KEY) is not None:
            raise ArchiveMigrationInProgressError("archive migration is in progress")

        busy_apps = tuple(
            row["name"]
            for row in db.execute(
                "SELECT name FROM apps WHERE status IN ('building', 'starting', 'removing') "
                "OR (status = 'error' AND (error_message = ? OR error_message LIKE '%[BUILD_CACHE_CORRUPT]%')) "
                "ORDER BY name",
                (INTERRUPTED_APP_REMOVAL_MESSAGE,),
            )
        )
        if busy_apps:
            raise AppsBusyForPrimaryChangeError(busy_apps)

        restart_app_ids = tuple(
            row["app_id"]
            for row in db.execute(
                "SELECT app_id FROM apps "
                "WHERE status = 'running' OR (status = 'error' AND container_id IS NOT NULL) "
                "ORDER BY name"
            )
        )

        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (LEGACY_DOMAIN_ASSET_OWNER_KEY, current_name_no_port),
        )
        demoted = db.execute("UPDATE domains SET is_primary = 0 WHERE name = ? AND is_primary = 1", (current_name,))
        promoted = db.execute("UPDATE domains SET is_primary = 1 WHERE name = ? AND is_primary = 0", (target_name,))
        if demoted.rowcount != 1 or promoted.rowcount != 1:
            raise RuntimeError("Primary domain changed concurrently")
        if restart_app_ids:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PRIMARY_DOMAIN_RESTART_APP_IDS_KEY, json.dumps(restart_app_ids)),
            )
        db.execute("COMMIT")
        return PrimaryDomainChange(changed=True, restart_app_ids=restart_app_ids)
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
