"""Tests for verify_owner_auth's origin gating, incl. the ``Origin: null`` case.

The bug: a real browser sends ``Origin: null`` for some legitimate same-origin top-level form POSTs
(a referrer policy or a redirect in the POST chain can opaque-ify the Origin while the request stays
same-origin and still carries the SameSite=Lax session cookie).  The strict origin-match check rejected
those, so authenticated form actions (add feed, refresh, ...) failed even though the session was valid.

The fix accepts a null Origin only when the unforgeable ``Sec-Fetch-Site: same-origin`` Fetch-Metadata
header corroborates that it really is the app posting to itself.  A cross-app (``same-site``) or
``cross-site`` request — which is how untrusted app JS would try to forge an owner request — reports a
different Sec-Fetch-Site and stays rejected, so this does not reopen cross-app CSRF.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException

import compute_space.web.auth.auth as authmod
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.db.schema import schema_path
from compute_space.tests.conftest import _make_test_config
from compute_space.web.auth.auth import verify_owner_auth

ZONE = "kilo-dev.selfhost.imbue.com"
APP_HOST = f"miniflux.{ZONE}"
OTHER_APP_HOST = f"other.{ZONE}"


@pytest.fixture
def _session_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'x')")
    conn.commit()
    token = create_session(1, conn)
    # verify_owner_auth calls get_db() (imported into authmod's namespace) to authenticate the cookie.
    monkeypatch.setattr(authmod, "get_db", lambda: conn)
    try:
        yield f"{SESSION_COOKIE_NAME}={token}"
    finally:
        conn.close()


def _authed(cookie: str, headers: dict[str, str]) -> ASGIConnection[Any, Any, Any, Any]:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/feeds/refresh",
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8080),
        "root_path": "",
        "headers": [(b"host", APP_HOST.encode()), (b"cookie", cookie.encode())]
        + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return ASGIConnection(scope)  # type: ignore[arg-type]


def _is_authorized(cookie: str, headers: dict[str, str]) -> bool:
    try:
        verify_owner_auth(_authed(cookie, headers))
        return True
    except NotAuthorizedException:
        return False


def test_null_origin_with_same_origin_fetch_site_is_authorized(_session_cookie: str) -> None:
    # The bug's real case: legit same-origin top-level form POST that carries Origin: null.
    assert _is_authorized(_session_cookie, {"origin": "null", "sec-fetch-site": "same-origin"})


@pytest.mark.parametrize("sec_fetch_site", ["same-site", "cross-site"])
def test_null_origin_with_non_same_origin_fetch_site_is_rejected(_session_cookie: str, sec_fetch_site: str) -> None:
    # A cross-app forgery attempt: untrusted app JS can't set Origin: null on a fetch, but even if a
    # request reached us with a null Origin, Sec-Fetch-Site (unforgeable) reveals it isn't same-origin.
    assert not _is_authorized(_session_cookie, {"origin": "null", "sec-fetch-site": sec_fetch_site})


def test_null_origin_without_fetch_metadata_is_rejected(_session_cookie: str) -> None:
    # No corroborating Sec-Fetch-Site (very old browser): fail closed on an opaque Origin.
    assert not _is_authorized(_session_cookie, {"origin": "null"})


def test_concrete_cross_origin_host_stays_rejected_even_with_spoofed_fetch_site(_session_cookie: str) -> None:
    # A concrete Origin for a different app subdomain is cross-origin regardless of any Sec-Fetch-Site.
    assert not _is_authorized(
        _session_cookie, {"origin": f"https://{OTHER_APP_HOST}", "sec-fetch-site": "same-origin"}
    )


def test_matching_origin_is_authorized(_session_cookie: str) -> None:
    assert _is_authorized(_session_cookie, {"origin": f"https://{APP_HOST}"})


def test_absent_origin_is_authorized(_session_cookie: str) -> None:
    # Browsers omit Origin on ordinary same-origin GET navigations.
    assert _is_authorized(_session_cookie, {})
