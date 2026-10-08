"""Unit tests for the proxy's inbound-header sanitization — what a backend app is allowed to see."""

import hashlib
import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from litestar.datastructures import Headers

from compute_space.core.auth.auth import is_openhost_credential
from compute_space.db.connection import init_db
from compute_space.web.auth.auth import carries_openhost_credential
from compute_space.web.helpers.proxy import _sanitize_forwarded_headers


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _add_api_token(db: sqlite3.Connection, token: str, expires_at: str = "") -> None:
    db.execute(
        "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
        ("test", _hash(token), expires_at),
    )


def _add_app_token(db: sqlite3.Connection, token: str, app_id: str = "app1") -> None:
    # validate_app_token joins apps, so the app row has to exist for the token to resolve.
    db.execute(
        "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port) "
        "VALUES (?, 'someapp', '1.0', 'serverfull', '/repo', 9000)",
        (app_id,),
    )
    db.execute("INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)", (app_id, _hash(token)))


def _add_session_token(db: sqlite3.Connection, token: str) -> None:
    db.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'x')")
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, 1, ?)",
        (_hash(token), (datetime.now(UTC) + timedelta(days=1)).isoformat()),
    )


# -- _sanitize_forwarded_headers ---------------------------------------------


def test_authorization_dropped_when_asked() -> None:
    """An openhost credential must never reach a backend app."""
    sanitized = _sanitize_forwarded_headers(
        [("Authorization", "Bearer owner-token"), ("Accept", "*/*")], strip_authorization=True
    )
    assert sanitized == [("Accept", "*/*")]


def test_authorization_kept_when_not_ours() -> None:
    """Apps run their own bearer auth, so an unrecognised Authorization passes through."""
    sanitized = _sanitize_forwarded_headers(
        [("Authorization", "Bearer app-own-token"), ("Accept", "*/*")], strip_authorization=False
    )
    assert sanitized == [("Authorization", "Bearer app-own-token"), ("Accept", "*/*")]


def test_every_authorization_value_dropped() -> None:
    """A request carrying several Authorization headers must not smuggle one past us."""
    sanitized = _sanitize_forwarded_headers(
        [("Authorization", "Basic abc"), ("Authorization", "Bearer owner-token")], strip_authorization=True
    )
    assert sanitized == []


def test_openhost_headers_and_session_cookie_still_stripped() -> None:
    """Pre-existing sanitization is unchanged by the Authorization handling."""
    sanitized = _sanitize_forwarded_headers(
        [
            ("X-OpenHost-Is-Owner", "true"),
            ("Cookie", "session_token=secret; theme=dark"),
            ("Authorization", "Bearer app-own-token"),
        ],
        strip_authorization=False,
    )
    assert sanitized == [("Cookie", "theme=dark"), ("Authorization", "Bearer app-own-token")]


# -- is_openhost_credential --------------------------------------------------


def test_api_token_is_our_credential(db: sqlite3.Connection) -> None:
    _add_api_token(db, "owner-token")
    assert is_openhost_credential("owner-token", db) is True


def test_app_token_is_our_credential(db: sqlite3.Connection) -> None:
    """An app token is a credential too — handing app A's token to app B lets B impersonate A."""
    _add_app_token(db, "app-token")
    assert is_openhost_credential("app-token", db) is True


def test_session_token_is_our_credential(db: sqlite3.Connection) -> None:
    """A session token presented as a bearer is still ours, so still must not be forwarded."""
    _add_session_token(db, "session-token")
    assert is_openhost_credential("session-token", db) is True


def test_unknown_token_is_not_our_credential(db: sqlite3.Connection) -> None:
    _add_api_token(db, "owner-token")
    assert is_openhost_credential("some-apps-own-token", db) is False


def test_expired_api_token_is_not_our_credential(db: sqlite3.Connection) -> None:
    """An expired token is no longer a credential the router would accept."""
    _add_api_token(db, "stale-token", expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert is_openhost_credential("stale-token", db) is False


# -- carries_openhost_credential ---------------------------------------------


def test_carries_detects_our_bearer(tmp_path: Path) -> None:
    init_db(str(tmp_path / "test.db"))
    with sqlite3.connect(str(tmp_path / "test.db")) as db:
        _add_api_token(db, "owner-token")
        db.commit()
    assert carries_openhost_credential(Headers({"Authorization": "Bearer owner-token"})) is True


def test_carries_ignores_foreign_and_non_bearer_schemes(tmp_path: Path) -> None:
    """Basic / AWS SigV4 and an app's own bearer are none of our business."""
    init_db(str(tmp_path / "test.db"))
    assert carries_openhost_credential(Headers({"Authorization": "Bearer app-own-token"})) is False
    assert carries_openhost_credential(Headers({"Authorization": "AWS4-HMAC-SHA256 Credential=k/..."})) is False
    assert carries_openhost_credential(Headers({"Authorization": "Basic dXNlcjpwYXNz"})) is False
    assert carries_openhost_credential(Headers({"Accept": "*/*"})) is False
