-- v14: records written through the `dns` service.
--
-- The zone files CoreDNS serves are generated, never edited: rendered from the instance's public
-- IP plus these rows, and overwritten whole on any change.  Keeping the records here rather than
-- in the file means no zone-file parsing, no read-modify-write races, and RRset semantics that
-- are four SQL statements.  The router's own apex/ns/wildcard A records are not stored -- they
-- are derived from the public IP at render time.
--
-- DDL is kept identical to db/schema.sql so fresh installs and upgrades converge.

CREATE TABLE IF NOT EXISTS dns_records (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    zone  TEXT NOT NULL,
    name  TEXT NOT NULL,
    type  TEXT NOT NULL,
    ttl   INTEGER NOT NULL,
    data  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dns_records_rrset ON dns_records(zone, name, type);
