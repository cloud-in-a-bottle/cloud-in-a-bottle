"""v18: add ``apps.prebuilt_image_verified``.  Body in v0018_apps_prebuilt_image_verified.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0018AppsPrebuiltImageVerified(SqlFileMigration):
    version = 18
    sql_file = "v0018_apps_prebuilt_image_verified.sql"
