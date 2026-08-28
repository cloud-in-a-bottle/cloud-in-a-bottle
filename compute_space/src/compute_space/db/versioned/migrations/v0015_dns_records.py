"""v15: add the ``dns_records`` table.  Body in v0015_dns_records.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0015DnsRecords(SqlFileMigration):
    version = 15
    sql_file = "v0015_dns_records.sql"
