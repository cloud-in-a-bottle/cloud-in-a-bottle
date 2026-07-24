CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- app_id: opaque 12-char base58 identity, the cross-table FK target.
    -- Stable across renames. ``name`` (below) is a label / subdomain only.
    app_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    manifest_name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    description TEXT,
    runtime_type TEXT NOT NULL DEFAULT 'serverfull',
    repo_path TEXT NOT NULL,
    repo_url TEXT,
    health_check TEXT,
    local_port INTEGER NOT NULL UNIQUE,
    container_port INTEGER,
    container_id TEXT,
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_cores REAL NOT NULL DEFAULT 0.1,
    gpu INTEGER NOT NULL DEFAULT 0,
    public_paths TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    manifest_raw TEXT,
    installed_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    db_name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
    UNIQUE(app_id, db_name)
);

CREATE TABLE IF NOT EXISTS app_port_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    label TEXT NOT NULL,
    container_port INTEGER NOT NULL,
    host_port INTEGER NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
    UNIQUE(app_id, label)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_port_mappings_host_port ON app_port_mappings(host_port);
CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_app_id ON apps(app_id);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);


-- API tokens: long-lived bearer tokens that grant owner-level access
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cross-app services: app authentication tokens
CREATE TABLE IF NOT EXISTS app_tokens (
    app_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

-- V2: service providers with git URL identity and versioning
CREATE TABLE IF NOT EXISTS service_providers_v2 (
    service_url TEXT NOT NULL,
    app_id TEXT NOT NULL,
    service_version TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    PRIMARY KEY (service_url, app_id, service_version),
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

-- V2: permissions with JSON grants and scope.
-- provider_app_id should be '' (which will be mapped to None in python) if scope==global
CREATE TABLE IF NOT EXISTS permissions_v2 (
    consumer_app_id TEXT NOT NULL,
    service_url TEXT NOT NULL,
    grant_payload TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
    provider_app_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (consumer_app_id, service_url, grant_payload, scope, provider_app_id),
    FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

-- V2: owner-configured default providers
CREATE TABLE IF NOT EXISTS service_defaults (
    service_url TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

-- Versioned-migrations metadata: single-row table recording the current
-- schema version. The runner (compute_space/db/versioned/runner.py) owns
-- the value; schema.sql only creates the table.
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

-- Archive backend state.  Single-row table.  Fresh zones come up at
-- backend='local' — the archive tier is backed by a directory on the
-- instance's local disk and is always available.  The operator can
-- later upgrade to durable object storage by configuring S3, which
-- migrates the local archive data into the bucket.  ('disabled' is
-- retained in the CHECK for backward compatibility with pre-v12 rows
-- but is no longer used by fresh installs.)
CREATE TABLE IF NOT EXISTS archive_backend (
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

INSERT OR IGNORE INTO archive_backend (id) VALUES (1);
