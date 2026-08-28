"""v14: drop ``apps.installed_by``.  Body in v0014_drop_apps_installed_by.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0014DropAppsInstalledBy(SqlFileMigration):
    version = 14
    sql_file = "v0014_drop_apps_installed_by.sql"
