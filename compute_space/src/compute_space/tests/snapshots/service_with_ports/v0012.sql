BEGIN TRANSACTION;
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO "api_tokens" VALUES(1,'ci-deploy','api-hash-ci','2099-01-01T00:00:00','2024-01-01T00:00:00');
INSERT INTO "api_tokens" VALUES(2,'monitoring','api-hash-mon','2099-12-31T00:00:00','2024-03-01T00:00:00');
CREATE TABLE "app_databases" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                db_name TEXT NOT NULL,
                db_path TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, db_name)
            );
INSERT INTO "app_databases" VALUES(1,'111111111112','orders_db','/data/orders/orders.db');
INSERT INTO "app_databases" VALUES(2,'111111111113','billing_db','/data/billing/billing.db');
CREATE TABLE "app_port_mappings" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                label TEXT NOT NULL,
                container_port INTEGER NOT NULL,
                host_port INTEGER NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, label)
            );
INSERT INTO "app_port_mappings" VALUES(1,'111111111112','grpc',8000,19500);
INSERT INTO "app_port_mappings" VALUES(2,'111111111112','health',8080,19501);
INSERT INTO "app_port_mappings" VALUES(3,'111111111113','http',8000,19502);
CREATE TABLE "app_tokens" (
                app_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
INSERT INTO "app_tokens" VALUES('111111111112','app-hash-orders');
INSERT INTO "app_tokens" VALUES('111111111113','app-hash-billing');
CREATE TABLE "apps" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                gpu INTEGER NOT NULL DEFAULT 0,
                public_paths TEXT NOT NULL DEFAULT '[]',
                manifest_raw TEXT,
                installed_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            , links TEXT NOT NULL DEFAULT '[]', cpu_cores REAL NOT NULL DEFAULT 0.1);
INSERT INTO "apps" VALUES(1,'111111111112','orders','orders','1.0.0','Order service','serverfull','/repo/orders',NULL,NULL,19100,NULL,NULL,'stopped',NULL,256,0,'[]',NULL,NULL,'2024-01-01T00:00:00','2024-01-01T00:00:00','[]',0.5);
INSERT INTO "apps" VALUES(2,'111111111113','billing','billing','2.1.0','Billing service','serverfull','/repo/billing','https://git.example/billing',NULL,19101,NULL,NULL,'running',NULL,512,0,'["/invoices"]',NULL,NULL,'2024-02-15T10:00:00','2024-02-15T10:00:00','[]',1.0);
CREATE TABLE "archive_backend" (
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
INSERT INTO "archive_backend" VALUES(1,'local',NULL,NULL,NULL,NULL,NULL,NULL,'openhost',NULL,NULL);
CREATE TABLE "permissions_v2" (
                consumer_app_id TEXT NOT NULL,
                service_url TEXT NOT NULL,
                grant_payload TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
                provider_app_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (consumer_app_id, service_url, grant_payload, scope, provider_app_id),
                FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO "schema_version" VALUES(1,12);
CREATE TABLE "service_defaults" (
                service_url TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE "service_providers_v2" (
                service_url TEXT NOT NULL,
                app_id TEXT NOT NULL,
                service_version TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                PRIMARY KEY (service_url, app_id, service_version),
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO "users" VALUES(1,'admin','argon2-stub-owner-hash','2024-01-01T00:00:00');
CREATE INDEX idx_apps_status ON apps(status);
CREATE UNIQUE INDEX idx_apps_app_id ON apps(app_id);
CREATE UNIQUE INDEX idx_port_mappings_host_port ON app_port_mappings(host_port);
CREATE INDEX sessions_user_id_idx ON sessions(user_id);
COMMIT;
