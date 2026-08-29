-- v16: DNS records apply to every zone, so they no longer carry one.
--
-- The `zone` column held the *result* of expanding a request at write time -- a write naming
-- every zone became one row per zone that existed right then -- so adding a zone could never
-- backfill the records that should have been in it.  Records now say what was asked for, and the
-- zones they render into are decided at render time, so a zone appearing needs no backfill at all.
--
-- The zone set itself is not stored: it is derived from `domains` at startup and held in memory
-- by the provider.
--
-- DDL is kept identical to db/schema.sql so fresh installs and upgrades converge.

-- Collapse the per-zone rows.  DISTINCT rather than picking a zone: rows that differed only by
-- zone were one request fanned out, and rows that genuinely differed were separate requests that
-- both still stand.
CREATE TABLE dns_records_new (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    type  TEXT NOT NULL,
    ttl   INTEGER NOT NULL,
    data  TEXT NOT NULL
);
INSERT INTO dns_records_new (name, type, ttl, data)
    SELECT DISTINCT name, type, ttl, data FROM dns_records;
DROP TABLE dns_records;
ALTER TABLE dns_records_new RENAME TO dns_records;
CREATE INDEX IF NOT EXISTS idx_dns_records_rrset ON dns_records(name, type);
