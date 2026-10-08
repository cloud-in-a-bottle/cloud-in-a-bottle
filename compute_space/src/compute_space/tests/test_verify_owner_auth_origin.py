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
from compute_space.tests._litestar_helpers import make_http_scope
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
    scope = make_http_scope("POST", "/feeds/refresh", host=APP_HOST, cookie=cookie, headers=headers)
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
    assert _is_authorized(_session_cookie, {"origin": f"https://{APP_HOST}", "sec-fetch-site": "same-origin"})


def test_absent_origin_is_authorized(_session_cookie: str) -> None:
    # Browsers omit Origin on ordinary same-origin GET navigations; Sec-Fetch-Site still identifies them.
    assert _is_authorized(_session_cookie, {"sec-fetch-site": "same-origin"})


def test_user_initiated_load_is_authorized(_session_cookie: str) -> None:
    # Typed URL / bookmark: no initiator at all.
    assert _is_authorized(_session_cookie, {"sec-fetch-site": "none"})


# ---------------------------------------------------------------------------
# Fetch-Metadata is required: no Sec-Fetch-Site, no owner authority.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-headers-at-all"),
        pytest.param({"origin": f"https://{APP_HOST}"}, id="matching-origin-but-no-fetch-metadata"),
    ],
)
def test_absent_fetch_metadata_is_rejected(_session_cookie: str, headers: dict[str, str]) -> None:
    """A matching Origin is no longer sufficient on its own.

    Origin is absent on every GET/HEAD navigation and subresource load, so trusting its absence is
    what let one app fire owner-authenticated GETs at another.  Anything without Fetch-Metadata is a
    pre-2023 browser or a non-browser client, and fails closed.
    """
    assert not _is_authorized(_session_cookie, headers)


# ---------------------------------------------------------------------------
# same-site: another app initiated this.  Only a visible top-level link survives.
# ---------------------------------------------------------------------------


def test_cross_app_link_navigation_is_authorized(_session_cookie: str) -> None:
    """The one same-site shape we keep: a top-level link the user clicked, e.g. one app sending the
    user into another to grant a permission.  No Origin (so it is a GET/HEAD) and Dest: document."""
    assert _is_authorized(_session_cookie, {"sec-fetch-site": "same-site", "sec-fetch-dest": "document"})


@pytest.mark.parametrize(
    "dest",
    ["iframe", "image", "script", "empty", "frame", "object", "embed"],
)
def test_cross_app_subresource_is_rejected(_session_cookie: str, dest: str) -> None:
    """The actual attack: app-A silently firing an owner-authenticated request at app-B.

    Note ``iframe`` also reports ``Sec-Fetch-Mode: navigate`` — keying the carve-out on Mode rather
    than Dest would admit a hidden ``<iframe src="https://other.zone/delete?id=1">``.
    """
    assert not _is_authorized(_session_cookie, {"sec-fetch-site": "same-site", "sec-fetch-dest": dest})


@pytest.mark.parametrize("origin", [f"https://{OTHER_APP_HOST}", "null"])
def test_cross_app_top_level_form_post_is_rejected(_session_cookie: str, origin: str) -> None:
    """A cross-app top-level *form POST* also reports ``Dest: document``, so the carve-out additionally
    requires Origin to be absent.  Browsers send an Origin on every non-GET/HEAD request — concrete
    normally, or ``null`` when the posting app sets a no-referrer policy."""
    assert not _is_authorized(
        _session_cookie,
        {"origin": origin, "sec-fetch-site": "same-site", "sec-fetch-dest": "document"},
    )


def test_cross_site_is_rejected(_session_cookie: str) -> None:
    assert not _is_authorized(_session_cookie, {"sec-fetch-site": "cross-site", "sec-fetch-dest": "document"})


# ---------------------------------------------------------------------------
# WebSocket handshakes are judged on Origin, because browsers send no Fetch-Metadata on them.
# ---------------------------------------------------------------------------


def _authed_ws(cookie: str, headers: dict[str, str]) -> ASGIConnection[Any, Any, Any, Any]:
    scope = make_http_scope(
        "GET",
        "/terminal/ws",
        host=APP_HOST,
        cookie=cookie,
        headers=headers,
        extra_scope={"type": "websocket"},
    )
    return ASGIConnection(scope)  # type: ignore[arg-type]


def _ws_is_authorized(cookie: str, headers: dict[str, str]) -> bool:
    try:
        verify_owner_auth(_authed_ws(cookie, headers))
        return True
    except NotAuthorizedException:
        return False


def test_ws_matching_origin_is_authorized(_session_cookie: str) -> None:
    """No Sec-Fetch-* is sent on a handshake, so a matching Origin alone must suffice — otherwise
    every WebSocket would fail closed."""
    assert _ws_is_authorized(_session_cookie, {"origin": f"https://{APP_HOST}"})


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": f"https://{OTHER_APP_HOST}"}, id="cross-app-origin"),
        pytest.param({"origin": "null"}, id="opaque-origin-sandboxed-iframe"),
        pytest.param({}, id="absent-origin"),
    ],
)
def test_ws_non_matching_origin_is_rejected(_session_cookie: str, headers: dict[str, str]) -> None:
    """``null`` only ever means a genuinely opaque initiator on a handshake — a referrer policy does
    not null a WebSocket's Origin — so unlike HTTP it gets no Fetch-Metadata reprieve.  An absent
    Origin is refused too: no browser omits it, and server-side callers use app/API tokens."""
    assert not _ws_is_authorized(_session_cookie, headers)


def test_ws_ignores_fetch_metadata(_session_cookie: str) -> None:
    """A forged Sec-Fetch-Site can't rescue a cross-app handshake, since the WS path never reads it."""
    assert not _ws_is_authorized(
        _session_cookie, {"origin": f"https://{OTHER_APP_HOST}", "sec-fetch-site": "same-origin"}
    )
