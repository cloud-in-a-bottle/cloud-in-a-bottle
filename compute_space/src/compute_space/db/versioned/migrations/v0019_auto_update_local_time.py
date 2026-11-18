from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0019AutoUpdateLocalTime(SqlFileMigration):
    version = 19
    sql_file = "v0019_auto_update_local_time.sql"
