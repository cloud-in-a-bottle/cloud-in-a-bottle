import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compute_space.db.connection import init_db
from compute_space.db.versioned import apply_migrations
from compute_space.db.versioned import read_version
from compute_space.db.versioned.registry import REGISTRY


@pytest.mark.parametrize("verified", [0, 1])
@pytest.mark.parametrize("version", [18, 19])
def test_upgrade_drops_only_verification_state_and_is_repeatable(tmp_path: Path, verified: int, version: int) -> None:
    path = str(tmp_path / "router.db")
    init_db(path)
    with closing(sqlite3.connect(path)) as db:
        # v18 and v19's schema is otherwise the current schema without v21's invites and user_paths. Reconstruct a
        # main-tracking instance that already ran the now-retired verification gate, with a deployed app and its
        # persisted manifest.
        db.execute("ALTER TABLE apps ADD COLUMN prebuilt_image_verified INTEGER NOT NULL DEFAULT 0")
        db.execute("DROP INDEX users_one_owner")
        db.execute("ALTER TABLE users DROP COLUMN invite_id")
        db.execute("DROP TABLE user_apps")
        db.execute("DROP TABLE invite_apps")
        db.execute("DROP TABLE invites")
        db.execute("ALTER TABLE apps DROP COLUMN user_paths")
        db.execute("UPDATE schema_version SET version = ?", (version,))
        db.execute(
            "INSERT INTO apps (app_id, name, version, local_port, repo_path, repo_url, status, manifest_raw, prebuilt_image_verified) "
            "VALUES ('app1', 'notes', '1.0.0', 19001, '/repo/notes', 'https://github.com/cloud-in-a-bottle/md-notes', "
            "'running', '[app]\nname = \"notes\"', ?)",
            (verified,),
        )
        db.commit()
        before = db.execute("SELECT * FROM apps").fetchall()
    apply_migrations(path, registry=REGISTRY)
    apply_migrations(path, registry=REGISTRY)
    with closing(sqlite3.connect(path)) as db:
        assert read_version(db) == REGISTRY[-1].version
        assert "prebuilt_image_verified" not in {row[1] for row in db.execute("PRAGMA table_info(apps)")}
        # v20 dropped the verification column (last before); v21 then appended user_paths.
        after = db.execute("SELECT * FROM apps").fetchall()
        assert [row[:-1] for row in after] == [row[:-1] for row in before]
        assert [row[-1] for row in after] == ["[]"]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
