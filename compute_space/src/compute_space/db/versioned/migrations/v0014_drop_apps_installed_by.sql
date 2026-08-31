-- v14: drop ``apps.installed_by``.
--
-- The column existed only for the ``installer`` v2 service, which recorded
-- the requesting consumer app so it could scope its /status and /logs
-- endpoints. That service has been removed, so nothing writes the column
-- and nothing reads it.

ALTER TABLE apps DROP COLUMN installed_by;
