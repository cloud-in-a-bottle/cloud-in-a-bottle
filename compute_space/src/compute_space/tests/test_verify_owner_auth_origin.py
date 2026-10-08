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


def _is_authorized(cookie: str, headers: dict[str, str], scope_type: str = "http") -> bool:
    scope = make_http_scope(
        "POST", "/feeds/refresh", host=APP_HOST, cookie=cookie, headers=headers, extra_scope={"type": scope_type}
    )
    try:
        verify_owner_auth(ASGIConnection(scope))  # type: ignore[arg-type]
        return True
    except NotAuthorizedException:
        return False


@pytest.mark.parametrize(
    "origin,site,dest,allowed",
    [
        (None, "same-origin", None, True),
        (SELF, "same-origin", None, True),
        (None, "none", None, True),
        # a same-origin POST under Referrer-Policy: no-referrer carries Origin: null.
        ("null", "same-origin", None, True),
        ("null", "same-site", None, False),
        ("null", "cross-site", None, False),
        # a concrete foreign Origin vetoes whatever Sec-Fetch-Site claims.
        (OTHER, "same-origin", None, False),
        # no Fetch-Metadata fails closed, even with a matching Origin.
        (None, None, None, False),
        (SELF, None, None, False),
        ("null", None, None, False),
        # same-site: only a top-level link from another app survives.
        (None, "same-site", "document", True),
        *[(None, "same-site", d, False) for d in ["iframe", "frame", "image", "script", "empty", "object", "embed"]],
        (OTHER, "same-site", "document", False),
        ("null", "same-site", "document", False),
        (None, "cross-site", "document", False),
    ],
)
def test_http(session_cookie: str, origin: str | None, site: str | None, dest: str | None, allowed: bool) -> None:
    headers = {"origin": origin, "sec-fetch-site": site, "sec-fetch-dest": dest}
    assert _is_authorized(session_cookie, {k: v for k, v in headers.items() if v is not None}) == allowed


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
