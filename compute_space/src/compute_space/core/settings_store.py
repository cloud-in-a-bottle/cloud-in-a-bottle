"""Tiny accessor over the DB ``settings`` key/value table — the router's store for singleton
runtime values.  First user: the first-boot ``claim_token`` (moved off its standalone file)."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from compute_space.config import Config

# Setting keys.
CLAIM_TOKEN_KEY = "claim_token"


def _connect(config: Config) -> sqlite3.Connection:
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    return db


def get_setting(config: Config, key: str) -> str | None:
    with closing(_connect(config)) as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_setting(config: Config, key: str, value: str) -> None:
    with closing(_connect(config)) as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        db.commit()


def delete_setting(config: Config, key: str) -> None:
    with closing(_connect(config)) as db:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
        db.commit()
