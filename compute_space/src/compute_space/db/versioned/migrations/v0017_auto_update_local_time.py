"""v17: store the automatic update time with its timezone.  Body in v0017_auto_update_local_time.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0017AutoUpdateLocalTime(SqlFileMigration):
    version = 17
    sql_file = "v0017_auto_update_local_time.sql"
