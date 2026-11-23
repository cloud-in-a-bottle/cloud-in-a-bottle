import hashlib
import secrets
import sqlite3
from datetime import UTC
from datetime import datetime

from compute_space.core.apps import App


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(
    db: sqlite3.Connection, name: str, app_ids: list[str], max_uses: int, expires_at: datetime
) -> tuple[int, str]:
    """Insert an invite for ``app_ids`` and return its id and raw token. Caller commits."""
    token = secrets.token_urlsafe(32)
    cursor = db.execute(
        "INSERT INTO invites (name, token_hash, uses_left, expires_at) VALUES (?, ?, ?, ?)",
        (name, _hash(token), max_uses, expires_at.isoformat()),
    )
    db.executemany(
        "INSERT INTO invite_apps (invite_id, app_id) VALUES (?, ?)",
        [(cursor.lastrowid, app_id) for app_id in app_ids],
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid, token


_OPEN_INVITE = "token_hash = ? AND uses_left > 0 AND datetime(expires_at) > datetime(?)"


def is_open_invite(db: sqlite3.Connection, token: str) -> bool:
    query = f"SELECT 1 FROM invites WHERE {_OPEN_INVITE}"
    return db.execute(query, (_hash(token), datetime.now(UTC).isoformat())).fetchone() is not None


def create_user(db: sqlite3.Connection, token: str, username: str, password_hash: str) -> int | None:
    """Spend one use of an open invite on a new user and return its user_id, or None if the invite is not open.

    Raises sqlite3.IntegrityError if the username is taken. Caller commits.
    """
    row = db.execute(
        f"UPDATE invites SET uses_left = uses_left - 1 WHERE {_OPEN_INVITE} RETURNING id",
        (_hash(token), datetime.now(UTC).isoformat()),
    ).fetchone()
    if row is None:
        return None
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, invite_id) VALUES (?, ?, ?)",
        (username, password_hash, row["id"]),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def user_can_access_app(db: sqlite3.Connection, invite_id: int, app_id: str) -> bool:
    query = "SELECT 1 FROM invite_apps WHERE invite_id = ? AND app_id = ?"
    return db.execute(query, (invite_id, app_id)).fetchone() is not None


def invite_apps(db: sqlite3.Connection, invite_id: int) -> list[App]:
    rows = db.execute(
        "SELECT a.* FROM invite_apps i JOIN apps a ON a.app_id = i.app_id WHERE i.invite_id = ? ORDER BY a.name",
        (invite_id,),
    )
    return [App.from_row(r) for r in rows]
