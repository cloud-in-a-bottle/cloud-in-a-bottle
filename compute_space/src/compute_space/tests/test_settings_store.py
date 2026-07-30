"""The DB-backed settings key/value store (config/domains consolidation)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from compute_space.core.settings_store import delete_setting
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting
from compute_space.db.versioned import apply_migrations
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com")
    apply_migrations(cfg.db_path)
    return cfg


def test_set_get_delete(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        assert get_setting(db, "claim_token") is None
        set_setting(db, "claim_token", "abc123")
        assert get_setting(db, "claim_token") == "abc123"
        # upsert overwrites
        set_setting(db, "claim_token", "def456")
        assert get_setting(db, "claim_token") == "def456"
        delete_setting(db, "claim_token")
        assert get_setting(db, "claim_token") is None
        # deleting a missing key is a no-op
        delete_setting(db, "claim_token")
