import hashlib
import re
import secrets
import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import attr
import bcrypt

from compute_space.core.auth.scopes import parse_scopes

SESSION_TTL_SECONDS = 28 * 24 * 60 * 60  # four weeks
SESSION_COOKIE_NAME = "session_token"

# Lowercase alphanumeric + dots/underscores/hyphens, starting with an alphanumeric.
# Length cap matches Mastodon's 30-char column. Lowercase-only so usernames are safe for subdomains.
OWNER_USERNAME_MAX_LEN = 30
_OWNER_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,29}$")
DEFAULT_OWNER_USERNAME = "owner"


def validate_owner_username(value: str) -> str | None:
    """Return None if valid, else an error string for the setup/settings form."""
    if not value:
        return "Username is required."
    if len(value) > OWNER_USERNAME_MAX_LEN:
        return f"Username must be at most {OWNER_USERNAME_MAX_LEN} characters."
    if not _OWNER_USERNAME_RE.match(value):
        return (
            "Username must start with a lowercase letter or digit and contain only"
            " lowercase letters, digits, `.`, `_` or `-`."
        )
    return None


def read_owner_username(db: sqlite3.Connection) -> str | None:
    """Return the configured owner username, or None if no user exists (pre-setup)."""
    row = db.execute("SELECT username FROM users ORDER BY user_id LIMIT 1").fetchone()
    if row is None:
        return None
    username: str = row["username"]
    return username or None


def update_owner_username(db: sqlite3.Connection, new_username: str) -> None:
    """Replace the owner's username. Caller must validate input and commit.

    Raises ValueError if no user row exists (pre-setup).
    """
    cursor = db.execute(
        "UPDATE users SET username = ? WHERE user_id = (SELECT user_id FROM users ORDER BY user_id LIMIT 1)",
        (new_username,),
    )
    if cursor.rowcount == 0:
        raise ValueError("No user exists; cannot update username before /setup runs.")


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAccessor:
    pass


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedUser(AuthenticatedAccessor):
    user_id: int
    username: str


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAPIKey(AuthenticatedAccessor):
    # Stable opaque identity of the token (the ``api_tokens.token_id`` string,
    # e.g. "tok_ab12…"), so handlers can attribute actions to it — e.g. stamp
    # apps.installed_by on token-initiated deploys, or audit-log.  This is a
    # random string, not the numeric row id, mirroring apps.app_id vs apps.id.
    token_id: str
    name: str
    # The scopes granted to this token (parsed from api_tokens.scopes).
    # `require_scope` checks membership; the special `owner` scope grants all.
    scopes: frozenset[str] = attr.ib(factory=frozenset)


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedApp(AuthenticatedAccessor):
    app_id: str


# Prefix for the opaque ``api_tokens.token_id`` identity string.
API_TOKEN_ID_PREFIX = "tok_"


def new_token_id() -> str:
    """Mint a fresh opaque token identity, e.g. ``tok_ab12…`` (128 bits)."""
    return f"{API_TOKEN_ID_PREFIX}{secrets.token_hex(16)}"


# Namespace prefix for ``apps.installed_by`` when a deploy is initiated by an
# API token (a consumer app stores its app name there instead).  Kept in its
# own namespace so a future "manage only the apps this token deployed"
# ownership scope can match token-initiated installs unambiguously.
API_TOKEN_INSTALLED_BY_PREFIX = "apitoken:"


def api_token_installed_by(token_id: str) -> str:
    """The ``apps.installed_by`` value stamped for a token-initiated deploy."""
    return f"{API_TOKEN_INSTALLED_BY_PREFIX}{token_id}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_password(password: str, db: sqlite3.Connection) -> int | None:
    # currently only have 1 user
    row = db.execute("SELECT user_id, password_hash FROM users LIMIT 1").fetchone()
    if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return int(row["user_id"])
    return None


def create_session(user_id: int, db: sqlite3.Connection) -> str:
    now = datetime.now(UTC)
    # Sweep expired tokens
    db.execute("DELETE FROM sessions WHERE datetime(expires_at) < datetime(?)", (now.isoformat(),))
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash(token), user_id, expires_at.isoformat()),
    )
    return token


def revoke_session(token: str, db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def validate_session_token(token: str, db: sqlite3.Connection) -> AuthenticatedUser | None:
    token_hash = _hash(token)
    query = """
        SELECT u.user_id, u.username, s.expires_at
        FROM sessions s JOIN users u ON u.user_id = s.user_id
        WHERE s.token_hash = ?"""
    if row := db.execute(query, (token_hash,)).fetchone():
        if datetime.fromisoformat(row["expires_at"]) >= datetime.now(UTC):
            return AuthenticatedUser(user_id=row["user_id"], username=row["username"])
    return None


def validate_api_token(token: str, db: sqlite3.Connection) -> AuthenticatedAPIKey | None:
    token_hash = _hash(token)

    if row := db.execute(
        "SELECT token_id, name, expires_at, scopes FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone():
        # NULL / empty expires_at means "never expires" — created via the
        # "never" option in /api/tokens.  Anything else is parsed and
        # compared.
        expires_at = row["expires_at"]
        accessor = AuthenticatedAPIKey(
            token_id=row["token_id"],
            name=row["name"],
            scopes=parse_scopes(row["scopes"]),
        )
        if not expires_at:
            return accessor
        if datetime.fromisoformat(expires_at) >= datetime.now(UTC):
            return accessor
    return None


def validate_app_token(token: str, db: sqlite3.Connection) -> AuthenticatedApp | None:
    token_hash = _hash(token)
    query = """
        SELECT apps.app_id, apps.name
        FROM app_tokens JOIN apps ON apps.app_id = app_tokens.app_id
        WHERE app_tokens.token_hash = ?"""
    if row := db.execute(query, (token_hash,)).fetchone():
        return AuthenticatedApp(app_id=row["app_id"])
    return None
