"""v14: add ``api_tokens.scopes`` column.  Body in v0014_api_token_scopes.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0014ApiTokenScopes(SqlFileMigration):
    version = 14
    sql_file = "v0014_api_token_scopes.sql"
