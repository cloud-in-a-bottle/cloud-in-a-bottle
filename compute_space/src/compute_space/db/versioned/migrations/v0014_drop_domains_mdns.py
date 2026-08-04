"""v14: drop the redundant ``domains.mdns`` column — mDNS is derived from the ``.local`` name.
Body in v0014_drop_domains_mdns.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0014DropDomainsMdns(SqlFileMigration):
    version = 14
    sql_file = "v0014_drop_domains_mdns.sql"
