from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException

import compute_space.web.auth.auth as authmod
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
def session_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'x')")
    conn.commit()
    monkeypatch.setattr(authmod, "get_db", lambda: conn)
    try:
        yield f"{SESSION_COOKIE_NAME}={create_session(1, conn)}"
    finally:
        conn.close()


def _is_authorized(cookie: str, headers: dict[str, str], scope_type: str = "http", method: str = "GET") -> bool:
    scope = make_http_scope(
        method, "/feeds/refresh", host=APP_HOST, cookie=cookie, headers=headers, extra_scope={"type": scope_type}
    )
    try:
        verify_owner_auth(ASGIConnection(scope))  # type: ignore[arg-type]
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
