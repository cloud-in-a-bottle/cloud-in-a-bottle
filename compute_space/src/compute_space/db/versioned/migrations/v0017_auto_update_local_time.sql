-- v17: store the automatic update time as a wall-clock time plus an IANA timezone.
--
-- Until v16 the time was stored only in UTC (``auto_update_time_utc``). It becomes the same time in the "UTC" zone,
-- so the schedule doesn't move; the owner can then pick their own timezone in Settings.

INSERT OR IGNORE INTO settings (key, value)
  SELECT 'auto_update_time', value FROM settings WHERE key = 'auto_update_time_utc';
INSERT OR IGNORE INTO settings (key, value)
  SELECT 'auto_update_timezone', 'UTC' FROM settings WHERE key = 'auto_update_time_utc';
DELETE FROM settings WHERE key = 'auto_update_time_utc';
