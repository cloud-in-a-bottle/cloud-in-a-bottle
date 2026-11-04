from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar import WebSocket
from litestar import post
from litestar import websocket
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient

import compute_space.web.auth.auth as authmod
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.db.schema import schema_path
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests.conftest import _make_test_config
from compute_space.web.auth.auth import verify_owner_auth

ZONE = "testzone.local"
APP_HOST = f"miniflux.{ZONE}"
SELF = f"https://{APP_HOST}"
OTHER = f"https://other.{ZONE}"


@pytest.fixture
def session_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=True)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'x')")
    conn.commit()
    monkeypatch.setattr(authmod, "get_db", lambda: conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def session_cookie(session_db: sqlite3.Connection) -> str:
    return f"{SESSION_COOKIE_NAME}={create_session(1, session_db)}"


def _cookie_for_state(db: sqlite3.Connection, cookie: str, state: str) -> str:
    if state == "expired":
        db.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    elif state == "revoked":
        db.execute("DELETE FROM sessions")
    elif state == "invalid":
        return f"{SESSION_COOKIE_NAME}=invalid-session"
    return cookie if state != "absent" else ""


def _is_authorized(
    cookie: str,
    headers: dict[str, str],
    scope_type: str = "http",
    method: str = "GET",
    allow_cross_site_navigation: bool = False,
) -> bool:
    scope = make_http_scope(
        method, "/feeds/refresh", host=APP_HOST, cookie=cookie, headers=headers, extra_scope={"type": scope_type}
    )
    try:
        verify_owner_auth(
            ASGIConnection(scope),  # type: ignore[arg-type]
            allow_cross_site_navigation=allow_cross_site_navigation,
        )
        return True
    except NotAuthorizedException:
        return False


@pytest.mark.parametrize(
    "method,site,dest,allowed",
    [
        ("GET", "same-origin", None, True),
        ("POST", "same-origin", None, True),
        ("GET", "none", None, True),
        ("POST", "none", None, True),
        # no Fetch-Metadata fails closed.
        ("GET", None, None, False),
        # same-site: only a top-level GET/HEAD from another app survives.
        ("GET", "same-site", "document", True),
        ("HEAD", "same-site", "document", True),
        ("POST", "same-site", "document", False),
        *[("GET", "same-site", d, False) for d in ["iframe", "frame", "image", "script", "empty", "object", "embed"]],
        ("GET", "cross-site", "document", False),
    ],
)
def test_http(session_cookie: str, method: str, site: str | None, dest: str | None, allowed: bool) -> None:
    headers = {"sec-fetch-site": site, "sec-fetch-dest": dest}
    present = {k: v for k, v in headers.items() if v is not None}
    assert _is_authorized(session_cookie, present, method=method) == allowed


@pytest.mark.parametrize(
    "method,site,dest,allowed",
    [
        ("GET", "cross-site", "document", True),
        ("HEAD", "cross-site", "document", True),
        ("POST", "cross-site", "document", False),
        *[("GET", "cross-site", d, False) for d in ["iframe", "frame", "image", "script", "empty", "object", "embed"]],
        # the flag only widens cross-site; everything else behaves as without it.
        ("GET", "same-site", "document", True),
        ("POST", "same-site", "document", False),
        ("GET", None, None, False),
    ],
)
def test_http_allowing_cross_site_navigation(
    session_cookie: str, method: str, site: str | None, dest: str | None, allowed: bool
) -> None:
    headers = {"sec-fetch-site": site, "sec-fetch-dest": dest}
    present = {k: v for k, v in headers.items() if v is not None}
    assert _is_authorized(session_cookie, present, method=method, allow_cross_site_navigation=True) == allowed


def test_http_ignores_origin(session_cookie: str) -> None:
    assert _is_authorized(session_cookie, {"origin": OTHER, "sec-fetch-site": "same-origin"})
    assert not _is_authorized(session_cookie, {"origin": SELF, "sec-fetch-site": "cross-site"})


@pytest.mark.parametrize(
    "headers,allowed",
    [
        ({"origin": SELF}, True),
        ({"origin": OTHER}, False),
        ({"origin": "null"}, False),
        ({}, False),
        # the WebSocket path never reads Fetch-Metadata, so it can't rescue a foreign Origin.
        ({"origin": OTHER, "sec-fetch-site": "same-origin"}, False),
    ],
)
def test_websocket(session_cookie: str, headers: dict[str, str], allowed: bool) -> None:
    assert _is_authorized(session_cookie, headers, scope_type="websocket") == allowed


@pytest.fixture
def owner_api_token(session_db: sqlite3.Connection) -> str:
    token = "owner-api-token-for-origin-test"
    session_db.execute(
        "INSERT INTO api_tokens (token_hash, name, expires_at) VALUES (?, 'extension', '2100-01-01T00:00:00+00:00')",
        (hashlib.sha256(token.encode()).hexdigest(),),
    )
    session_db.commit()
    return token


@pytest.fixture
def app_token(session_db: sqlite3.Connection) -> str:
    app_id = new_app_id()
    token = "app-token-for-origin-test"
    session_db.execute(
        "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port) "
        "VALUES (?, 'hister', '1.0', 'serverfull', '/repo', 9000)",
        (app_id,),
    )
    session_db.execute(
        "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
        (app_id, hashlib.sha256(token.encode()).hexdigest()),
    )
    session_db.commit()
    return token


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
@pytest.mark.parametrize("cookie_state", ["valid", "expired", "revoked", "invalid", "absent"])
def test_owner_bearer_is_not_shadowed_by_cookie(
    session_db: sqlite3.Connection, session_cookie: str, owner_api_token: str, scope_type: str, cookie_state: str
) -> None:
    cookie = _cookie_for_state(session_db, session_cookie, cookie_state)
    headers = {
        "authorization": f"Bearer {owner_api_token}",
        "origin": "chrome-extension://cciilamhchpmbdnniabclekddabkifhb",
        "sec-fetch-site": "cross-site",
    }
    with patch.object(authmod, "validate_api_token", wraps=authmod.validate_api_token) as validate:
        assert _is_authorized(cookie, headers, scope_type=scope_type, method="POST")
    assert validate.call_count == 1


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
@pytest.mark.parametrize("token_state", ["invalid", "expired", "revoked", "app", "hister-header"])
@pytest.mark.parametrize("cookie_state", ["valid", "expired", "revoked", "invalid", "absent"])
def test_bad_bearer_cannot_rescue_cross_origin_cookie(
    session_db: sqlite3.Connection,
    session_cookie: str,
    owner_api_token: str,
    app_token: str,
    token_state: str,
    scope_type: str,
    cookie_state: str,
) -> None:
    cookie = _cookie_for_state(session_db, session_cookie, cookie_state)
    headers = {"origin": OTHER, "sec-fetch-site": "cross-site"}
    if token_state == "expired":
        session_db.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    elif token_state == "revoked":
        session_db.execute("DELETE FROM api_tokens")
    if token_state == "hister-header":
        headers["x-access-token"] = owner_api_token
    else:
        token = "invalid-token" if token_state == "invalid" else app_token if token_state == "app" else owner_api_token
        headers["authorization"] = f"Bearer {token}"
    with patch.object(authmod, "validate_api_token", wraps=authmod.validate_api_token) as validate:
        assert not _is_authorized(cookie, headers, scope_type=scope_type, method="POST")
    assert validate.call_count == (0 if token_state == "hister-header" else 1)


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
@pytest.mark.parametrize("token_state", ["valid", "invalid", "expired", "revoked", "app", "absent"])
def test_safe_cookie_happy_path_does_not_validate_bearer(
    session_db: sqlite3.Connection,
    session_cookie: str,
    owner_api_token: str,
    app_token: str,
    scope_type: str,
    token_state: str,
) -> None:
    if token_state == "expired":
        session_db.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    elif token_state == "revoked":
        session_db.execute("DELETE FROM api_tokens")
    headers = {"origin": SELF, "sec-fetch-site": "same-origin"}
    if token_state != "absent":
        token = app_token if token_state == "app" else "invalid-token" if token_state == "invalid" else owner_api_token
        headers["authorization"] = f"Bearer {token}"
    with patch.object(authmod, "validate_api_token", wraps=authmod.validate_api_token) as validate:
        assert _is_authorized(session_cookie, headers, scope_type=scope_type, method="POST")
    validate.assert_not_called()


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("safe_origin", [False, True])
@pytest.mark.parametrize("cookie_state", ["valid", "expired", "revoked", "invalid", "absent"])
@pytest.mark.parametrize("token_state", ["valid", "invalid", "expired", "revoked", "app", "absent"])
def test_owner_guards_with_cookies_and_bearers(
    session_db: sqlite3.Connection,
    session_cookie: str,
    owner_api_token: str,
    app_token: str,
    transport: str,
    safe_origin: bool,
    cookie_state: str,
    token_state: str,
) -> None:
    cookie = _cookie_for_state(session_db, session_cookie, cookie_state)
    if token_state == "expired":
        session_db.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    elif token_state == "revoked":
        session_db.execute("DELETE FROM api_tokens")
    headers = {
        "cookie": cookie,
        "origin": SELF if safe_origin else OTHER,
        "sec-fetch-site": "same-origin" if safe_origin else "cross-site",
    }
    if token_state != "absent":
        token = app_token if token_state == "app" else "invalid-token" if token_state == "invalid" else owner_api_token
        headers["authorization"] = f"Bearer {token}"

    @post("/owner", status_code=200, guards=[authmod.require_owner_auth], sync_to_thread=False)
    def owner() -> dict[str, bool]:
        return {"ok": True}

    @websocket("/owner-ws")
    async def owner_ws(socket: WebSocket) -> None:
        if not await authmod.verify_owner_ws(socket):
            return
        await socket.accept()
        await socket.send_text("owner")
        await socket.close()

    authorized = token_state == "valid" or (safe_origin and cookie_state == "valid")
    with (
        patch.object(authmod, "validate_api_token", wraps=authmod.validate_api_token) as validate,
        TestClient(Litestar(route_handlers=[owner, owner_ws], openapi_config=None), base_url=SELF) as client,
    ):
        if transport == "http":
            response = client.post("/owner", headers=headers)
            assert response.status_code == (200 if authorized else 401)
            if authorized:
                assert response.json() == {"ok": True}
        else:
            with client.websocket_connect("/owner-ws", headers=headers) as socket:
                if authorized:
                    message = socket.receive()
                    assert message["type"] == "websocket.send" and message["text"] == "owner"
                else:
                    with pytest.raises(WebSocketDisconnect) as exc:
                        socket.receive()
                    assert exc.value.code == 4401
    expected_validations = 0 if (safe_origin and cookie_state == "valid") or token_state == "absent" else 1
    assert validate.call_count == expected_validations
