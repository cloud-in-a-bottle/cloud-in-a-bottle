import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compute_space.db.connection import init_db
from compute_space.db.versioned import apply_migrations
from compute_space.db.versioned import read_version
from compute_space.db.versioned.registry import REGISTRY


@pytest.mark.parametrize("verified", [0, 1])
def test_v18_upgrade_drops_only_verification_state_and_is_repeatable(tmp_path: Path, verified: int) -> None:
    path = str(tmp_path / "router.db")
    init_db(path)
    with closing(sqlite3.connect(path)) as db:
        # v18's schema is otherwise identical to the current schema. Reconstruct a main-tracking instance that
        # already ran the now-retired verification gate, with a deployed app and its persisted manifest.
        db.execute("ALTER TABLE apps ADD COLUMN prebuilt_image_verified INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE schema_version SET version = 18")
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
        assert read_version(db) == 19
        assert "prebuilt_image_verified" not in {row[1] for row in db.execute("PRAGMA table_info(apps)")}
        assert db.execute("SELECT * FROM apps").fetchall() == [row[:-1] for row in before]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
