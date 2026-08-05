"""Build a pre-consolidation (v12) router.db for the migration-walk container test: the full
schema minus the v13 domains/settings tables, stamped at version 12 (a real old instance's state)."""

from __future__ import annotations

import sqlite3
import sys

from compute_space.db.schema import schema_path


def main(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(open(schema_path()).read())
        conn.execute("DROP TABLE domains")
        conn.execute("DROP TABLE settings")
        conn.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, 12)")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1])
