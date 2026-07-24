"""v12: add the ``'local'`` archive backend state and make it the default.

Body in v0012_archive_local_backend.sql.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0012ArchiveLocalBackend(SqlFileMigration):
    version = 12
    sql_file = "v0012_archive_local_backend.sql"
