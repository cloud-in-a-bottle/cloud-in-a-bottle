"""v16: drop ``dns_records.zone``.  Body in v0016_dns_zones.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0016DnsZones(SqlFileMigration):
    version = 16
    sql_file = "v0016_dns_zones.sql"
