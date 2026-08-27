"""Records written through the ``dns`` service, stored in the DB.

The zone file CoreDNS serves is generated from these rows, so RRset semantics are just SQL and
there is no read-modify-write on a file to race.  The router's own apex/ns/wildcard A records are
not stored — they are derived from the instance's public IP at render time.
"""

from __future__ import annotations

import sqlite3

from compute_space.core.dns.service_api import DnsRecord
from compute_space.core.dns.service_api import normalize_zone


def records_for(db: sqlite3.Connection, zone: str) -> list[DnsRecord]:
    rows = db.execute(
        "SELECT name, type, ttl, data FROM dns_records WHERE zone = ? ORDER BY name, type, data",
        (normalize_zone(zone),),
    ).fetchall()
    return [DnsRecord(name=r["name"], type=r["type"], ttl=r["ttl"], data=r["data"]) for r in rows]


def set_records(db: sqlite3.Connection, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """Replace each ``(name, type)`` RRset mentioned; leave everything else alone."""
    with db:
        for name, rrtype in {(r.name, r.type) for r in records}:
            db.execute(
                "DELETE FROM dns_records WHERE zone = ? AND name = ? AND type = ?",
                (normalize_zone(zone), name, rrtype),
            )
        _insert(db, zone, records)
    return records


def append_records(db: sqlite3.Connection, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    with db:
        _insert(db, zone, records)
    return records


def delete_records(db: sqlite3.Connection, zone: str, records: list[DnsRecord]) -> list[DnsRecord]:
    """A record with no data clears its whole RRset, which is how a caller cleans up a name without
    knowing what is there.  Absent records are ignored, so cleanup is safe to re-run."""
    with db:
        for record in records:
            if record.data is None:
                db.execute(
                    "DELETE FROM dns_records WHERE zone = ? AND name = ? AND type = ?",
                    (normalize_zone(zone), record.name, record.type),
                )
            else:
                db.execute(
                    "DELETE FROM dns_records WHERE zone = ? AND name = ? AND type = ? AND data = ?",
                    (normalize_zone(zone), record.name, record.type, record.data),
                )
    return records


def _insert(db: sqlite3.Connection, zone: str, records: list[DnsRecord]) -> None:
    db.executemany(
        "INSERT INTO dns_records (zone, name, type, ttl, data) VALUES (?, ?, ?, ?, ?)",
        [(normalize_zone(zone), r.name, r.type, r.ttl, r.data) for r in records],
    )
