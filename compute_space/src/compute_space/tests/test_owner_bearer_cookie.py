import hashlib
import sqlite3
from collections.abc import Iterator

import pytest
from litestar.exceptions import NotAuthorizedException
from litestar.testing import RequestFactory

import compute_space.web.auth.auth as authmod
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.db.schema import schema_path


@pytest.fixture
def auth_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        db.executescript(f.read())
    db.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'x')")
    monkeypatch.setattr(authmod, "get_db", lambda: db)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("cookie_state", ["valid", "expired", "absent"])
@pytest.mark.parametrize("bearer_state", ["valid-api", "expired-api", "invalid", "app", "absent"])
def test_cross_origin_bearer_with_cookie(auth_db: sqlite3.Connection, cookie_state: str, bearer_state: str) -> None:
    session = create_session(1, auth_db)
    if cookie_state == "expired":
        auth_db.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    cookie = f"{SESSION_COOKIE_NAME}={session}" if cookie_state != "absent" else ""
    api_token = "test-api-bearer"
    expiry = "2000-01-01T00:00:00+00:00" if bearer_state == "expired-api" else "2100-01-01T00:00:00+00:00"
    auth_db.execute(
        "INSERT INTO api_tokens (token_hash, name, expires_at) VALUES (?, 'extension', ?)",
        (hashlib.sha256(api_token.encode()).hexdigest(), expiry),
    )
    app_id = new_app_id()
    app_token = "test-app-bearer"
    auth_db.execute(
        "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port) "
        "VALUES (?, 'hister', '1.0', 'serverfull', '/repo', 9000)",
        (app_id,),
    )
    auth_db.execute(
        "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
        (app_id, hashlib.sha256(app_token.encode()).hexdigest()),
    )
    headers = {"Origin": "chrome-extension://cciilamhchpmbdnniabclekddabkifhb"}
    if bearer_state != "absent":
        token = app_token if bearer_state == "app" else "invalid" if bearer_state == "invalid" else api_token
        headers["Authorization"] = f"Bearer {token}"
    request = RequestFactory().post("/api/add", headers=headers, cookies=cookie)
    if bearer_state == "valid-api":
        authmod.verify_owner_auth(request)
    else:
        with pytest.raises(NotAuthorizedException):
            authmod.verify_owner_auth(request)
