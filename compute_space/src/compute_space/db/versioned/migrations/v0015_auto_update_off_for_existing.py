"""v15: seed ``auto_update_enabled = 0`` on existing DBs.  Body in v0015_auto_update_off_for_existing.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0015AutoUpdateOffForExisting(SqlFileMigration):
    version = 15
    sql_file = "v0015_auto_update_off_for_existing.sql"
