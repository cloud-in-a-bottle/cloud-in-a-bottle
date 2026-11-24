import hashlib
import secrets
import sqlite3
from datetime import UTC
from datetime import datetime

import attr

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


# The apps a user may use: their invite's, plus any the owner assigned to them directly.
_USER_APP_IDS = "SELECT app_id FROM invite_apps WHERE invite_id = :invite_id UNION SELECT app_id FROM user_apps WHERE user_id = :user_id"


def user_can_access_app(db: sqlite3.Connection, user_id: int, invite_id: int, app_id: str) -> bool:
    query = f"SELECT 1 FROM ({_USER_APP_IDS}) WHERE app_id = :app_id"
    return db.execute(query, {"user_id": user_id, "invite_id": invite_id, "app_id": app_id}).fetchone() is not None


def user_apps(db: sqlite3.Connection, user_id: int, invite_id: int) -> list[App]:
    query = f"SELECT * FROM apps WHERE app_id IN ({_USER_APP_IDS}) ORDER BY name"
    return [App.from_row(r) for r in db.execute(query, {"user_id": user_id, "invite_id": invite_id})]


def invite_apps(db: sqlite3.Connection, invite_id: int) -> list[App]:
    rows = db.execute(
        "SELECT a.* FROM invite_apps i JOIN apps a ON a.app_id = i.app_id WHERE i.invite_id = ? ORDER BY a.name",
        (invite_id,),
    )
    return [App.from_row(r) for r in rows]


@attr.s(auto_attribs=True, frozen=True)
class AppUser:
    """A user as the owner sees them on one app's page."""

    user_id: int
    username: str
    invite_name: str
    via_invite: bool
    assigned: bool


def app_users(db: sqlite3.Connection, app_id: str) -> list[AppUser]:
    rows = db.execute(
        """SELECT u.user_id, u.username, i.name AS invite_name,
                  EXISTS (SELECT 1 FROM invite_apps WHERE invite_id = u.invite_id AND app_id = :app_id) AS via_invite,
                  EXISTS (SELECT 1 FROM user_apps WHERE user_id = u.user_id AND app_id = :app_id) AS assigned
           FROM users u JOIN invites i ON i.id = u.invite_id ORDER BY u.username""",
        {"app_id": app_id},
    )
    return [
        AppUser(r["user_id"], r["username"], r["invite_name"], bool(r["via_invite"]), bool(r["assigned"]))
        for r in rows
    ]


def set_app_users(db: sqlite3.Connection, app_id: str, user_ids: list[int]) -> None:
    """Replace the users assigned to ``app_id`` directly. Caller validates the ids and commits."""
    db.execute("DELETE FROM user_apps WHERE app_id = ?", (app_id,))
    db.executemany("INSERT INTO user_apps (user_id, app_id) VALUES (?, ?)", [(u, app_id) for u in user_ids])
