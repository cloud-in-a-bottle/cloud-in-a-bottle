from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0015DropDomainsMdns(SqlFileMigration):
    version = 15
    sql_file = "v0015_drop_domains_mdns.sql"
