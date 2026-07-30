"""v13: add per-app egress columns.  Body in v0013_app_egress.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0013AppEgress(SqlFileMigration):
    version = 13
    sql_file = "v0013_app_egress.sql"
