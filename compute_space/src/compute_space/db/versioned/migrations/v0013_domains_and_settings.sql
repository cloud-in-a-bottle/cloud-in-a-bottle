-- v13: domains + settings tables.
--
-- Introduces the DB as the single source of truth for the instance's domain set (replacing the
-- config.toml [[openhost.domains]] array + runtime_domains.json) and a small key/value settings
-- store (which will hold the first-boot claim token, moved off its standalone file).  This
-- migration only creates the tables; seeding from the legacy config-file domains /
-- runtime_domains.json / claim_token file happens in the application layer on first boot after
-- upgrade (see docs/config-consolidation-design.md).  DDL is kept identical to db/schema.sql so
-- fresh installs and upgrades converge on the same shape.

CREATE TABLE IF NOT EXISTS domains (
    name          TEXT PRIMARY KEY,
    tls           INTEGER NOT NULL DEFAULT 0,
    mdns          INTEGER NOT NULL DEFAULT 0,
    is_primary    INTEGER NOT NULL DEFAULT 0,
    cert_status   TEXT NOT NULL DEFAULT 'none' CHECK(cert_status IN ('none', 'acquiring', 'active', 'error')),
    error_message TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_domains_one_primary ON domains(is_primary) WHERE is_primary = 1;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
