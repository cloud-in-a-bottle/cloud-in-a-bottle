-- v10: add ``apps.links`` column.
--
-- Stores the app's user-facing [[links]] from its cloudinabottle.toml as a JSON
-- array of {"name", "path"} objects. These are advertised to the user on
-- the dashboard so they can discover interesting paths on the app (e.g. an
-- admin console at /_openhost/admin) that aren't the bare app root.
-- Defaults to '[]' for existing rows; they are backfilled on the next
-- reload/update of each app from its manifest.

ALTER TABLE apps ADD COLUMN links TEXT NOT NULL DEFAULT '[]';
