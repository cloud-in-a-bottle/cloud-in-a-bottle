"""v14: add the ``dns_records`` table.  Body in v0014_dns_records.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0014DnsRecords(SqlFileMigration):
    version = 14
    sql_file = "v0014_dns_records.sql"
