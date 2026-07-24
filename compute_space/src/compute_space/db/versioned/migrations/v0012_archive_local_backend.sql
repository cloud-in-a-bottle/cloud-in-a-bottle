-- v12: introduce the 'local' archive backend and make it the default.
--
-- Before: the archive tier had two states — 'disabled' (no archive at
-- all) and 's3' (JuiceFS-on-S3).  Apps that opted into app_archive were
-- refused installation until the operator configured S3.
--
-- After: the archive tier is ALWAYS available.  The new default is
-- 'local' — the archive is backed by a directory on the instance's
-- local disk.  The operator can later upgrade to durable object storage
-- by configuring S3 (which migrates the local archive data into the
-- bucket).  There is no longer a way to have "no archive tier": an app
-- that requests app_archive can always be installed; it just runs on
-- local storage until S3 is configured.
--
-- SQLite cannot alter a CHECK constraint in place, so we rebuild the
-- single-row archive_backend table.  Any existing zone currently on
-- 'disabled' (i.e. never configured S3) is migrated to 'local' so the
-- new "always available" behaviour applies uniformly to new AND existing
-- zones.  Zones already on 's3' are left untouched.
--
-- NOTE: no BEGIN/COMMIT here — SqlFileMigration.apply wraps this file in
-- its own transaction.

CREATE TABLE archive_backend_new (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend TEXT NOT NULL DEFAULT 'local' CHECK(backend IN ('disabled', 'local', 's3')),
    s3_bucket TEXT,
    s3_region TEXT,
    s3_endpoint TEXT,
    s3_prefix TEXT,
    s3_access_key_id TEXT,
    s3_secret_access_key TEXT,
    juicefs_volume_name TEXT NOT NULL DEFAULT 'openhost',
    configured_at TEXT,
    state_message TEXT
);

-- Copy the existing single row across, rewriting 'disabled' -> 'local'.
INSERT INTO archive_backend_new (
    id, backend, s3_bucket, s3_region, s3_endpoint, s3_prefix,
    s3_access_key_id, s3_secret_access_key, juicefs_volume_name,
    configured_at, state_message
)
SELECT
    id,
    CASE WHEN backend = 'disabled' THEN 'local' ELSE backend END,
    s3_bucket, s3_region, s3_endpoint, s3_prefix,
    s3_access_key_id, s3_secret_access_key, juicefs_volume_name,
    configured_at, state_message
FROM archive_backend;

DROP TABLE archive_backend;
ALTER TABLE archive_backend_new RENAME TO archive_backend;

-- Ensure the singleton row exists on any partially-initialised DB.
INSERT OR IGNORE INTO archive_backend (id, backend) VALUES (1, 'local');
