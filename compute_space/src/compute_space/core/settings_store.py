"""Tiny accessor over the DB ``settings`` key/value table — the router's store for singleton
runtime values.  First user: the first-boot ``claim_token`` (moved off its standalone file)."""

from __future__ import annotations

import sqlite3

# Setting keys.
CLAIM_TOKEN_KEY = "claim_token"


def get_setting(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def delete_setting(db: sqlite3.Connection, key: str) -> None:
    db.execute("DELETE FROM settings WHERE key = ?", (key,))
    db.commit()
