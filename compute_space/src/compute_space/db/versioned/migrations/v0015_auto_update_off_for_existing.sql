-- v15: keep automatic instance updates off on instances that predate them.
--
-- A fresh DB is initialised from schema.sql and skips this, so it has no
-- ``auto_update_enabled`` row and gets the default (on). An existing instance
-- reaching this version keeps its previous behaviour of never updating itself
-- until the owner turns it on in Settings.

INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_update_enabled', '0');
