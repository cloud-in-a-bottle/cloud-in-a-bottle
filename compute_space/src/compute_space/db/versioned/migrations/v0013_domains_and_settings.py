"""v13: add ``domains`` + ``settings`` tables.  Body in v0013_domains_and_settings.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0013DomainsAndSettings(SqlFileMigration):
    version = 13
    sql_file = "v0013_domains_and_settings.sql"
