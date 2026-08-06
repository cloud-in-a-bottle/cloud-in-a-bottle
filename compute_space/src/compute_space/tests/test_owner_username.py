"""Tests for the owner-username plumbing on the session/Litestar auth model.

Covers the load-bearing pieces of the OPENHOST_OWNER_USERNAME feature:

  1. ``validate_owner_username`` — input rules.
  2. ``read_owner_username`` / ``update_owner_username`` — round-tripping
     through the ``users`` table.  Pre-setup zones (no user row) must
     return None / raise ValueError respectively; the env-var plumbing
     keys on None to mean "use the default".
  3. ``provision_data`` — env var stamped with the passed-in value.
  4. The Litestar ``/api/settings/owner_username`` routes.
  5. The setup app's optional username form field.
  6. /login: the session minted on login resolves to the persisted
     username on subsequent ``authenticate()`` calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import bcrypt
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.auth.auth import DEFAULT_OWNER_USERNAME
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.data import provision_data
from compute_space.core.manifest import AppManifest
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import stash_zone_middleware
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.setup_app import create_setup_app

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_user(db_path: str, username: str = "owner", password: str = "secretpass1") -> int:
    """Insert a user row and return its user_id. Uses a real bcrypt hash so
    /login flows can be exercised against the same row."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, pw_hash),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _create_session_for(db_path: str, user_id: int) -> str:
    """Issue a session token for ``user_id`` directly against the DB,
    bypassing /login.  Returned token is suitable for use as the
    session cookie."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        token = create_session(user_id, conn)
        conn.commit()
        return token
    finally:
        conn.close()


def _read_username_direct(db_path: str) -> str | None:
    """Bypass any DI connection cache by opening a fresh connection."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT username FROM users ORDER BY user_id LIMIT 1").fetchone()
        return None if row is None else row["username"]
    finally:
        conn.close()


def _bare_manifest() -> AppManifest:
    """Minimal manifest with app_data enabled so provision_data runs."""
    return AppManifest(  # type: ignore[call-arg]
        name="probe",
        version="1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=128,
        cpu_cores=0.1,
        app_data=True,
    )


def _provision(tmp_path: Path, **kwargs: Any) -> dict[str, str]:

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(exist_ok=True)
    return provision_data(
        "test-app-id",
        "probe",
        _bare_manifest(),
        str(tmp_path / "data"),
        str(tmp_path / "temp"),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20500)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def db(cfg: Any) -> Iterator[sqlite3.Connection]:
    """Open a fresh sqlite3 connection bound to the test DB."""
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _make_settings_app() -> Litestar:
    """Build a minimal Litestar app exposing just the settings routes —
    enough to exercise the GET/POST /api/settings/owner_username endpoints
    under the real ``require_owner_auth`` guard."""
    return Litestar(
        route_handlers=[api_settings_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _make_login_app() -> Litestar:
    """App exposing just /login and /logout."""
    return Litestar(
        route_handlers=[pages_login_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        middleware=[stash_zone_middleware],
        openapi_config=None,
    )


@pytest.fixture
def settings_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=_make_settings_app()) as c:
        yield c


@pytest.fixture
def login_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=_make_login_app()) as c:
        yield c


@pytest.fixture
def setup_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    """TestClient for the setup-only app.  Patches out the post-setup
    side effects (default app deploy + restart) since neither is
    relevant to username plumbing and both fight the test harness."""
    app = create_setup_app(cfg)
    with (
        mock.patch("compute_space.web.setup_app.deploy_default_apps"),
        mock.patch("compute_space.web.setup_app._trigger_restart_after_response"),
        TestClient(app=app) as c,
    ):
        yield c


def _auth_cookie(cfg: Any, username: str = "owner") -> dict[str, str]:
    """Seed a user + session and return a Cookie header dict for the TestClient."""
    user_id = _seed_user(cfg.db_path, username=username)
    token = _create_session_for(cfg.db_path, user_id)
    return {SESSION_COOKIE_NAME: token}


# ---------------------------------------------------------------------------
# validate_owner_username
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "alice",
        "alice123",
        "alice.bishop",
        "alice_bishop",
        "alice-bishop",
        "a",  # 1-char min
        "a" * 30,  # max length
        "x.y.z-1_2",  # all-allowed-punct mix
    ],
)
def test_validate_owner_username_accepts(value: str) -> None:
    assert validate_owner_username(value) is None, value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "Alice",  # uppercase rejected (lowercase-only — must be safe for subdomains)
        ".alice",  # leading punct breaks PeerTube
        "_alice",
        "-alice",
        "alice@example.com",  # email shape rejected (avoid SSO identifier collisions)
        "alice space",
        "alice/bob",
        "a" * 31,  # over max
        "alice\nfoo",  # control char breaks HTTP headers
        "ünicode",  # non-ASCII
    ],
)
def test_validate_owner_username_rejects(value: str) -> None:
    assert validate_owner_username(value) is not None, value


# ---------------------------------------------------------------------------
# read / update owner username
# ---------------------------------------------------------------------------


def test_read_owner_username_returns_none_pre_setup(db: sqlite3.Connection) -> None:
    """Pre-setup, the users table is empty; read must return None, not raise —
    provisioning keys on the default fallback to mean "no operator-set value"."""
    assert read_owner_username(db) is None


def test_read_owner_username_returns_value_after_setup(cfg: Any, db: sqlite3.Connection) -> None:
    _seed_user(cfg.db_path, "alice")
    assert read_owner_username(db) == "alice"


def test_update_owner_username_persists(cfg: Any, db: sqlite3.Connection) -> None:
    _seed_user(cfg.db_path, "owner")
    update_owner_username(db, "zack")
    db.commit()
    assert read_owner_username(db) == "zack"


def test_update_owner_username_does_not_create_extra_row(cfg: Any, db: sqlite3.Connection) -> None:
    """update_* must mutate the single user row, not insert a second."""
    _seed_user(cfg.db_path, "owner")
    update_owner_username(db, "zack")
    update_owner_username(db, "alice")
    db.commit()
    rows = db.execute("SELECT user_id, username FROM users").fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] == "alice"


def test_update_owner_username_raises_pre_setup(db: sqlite3.Connection) -> None:
    """Updating with no user row must raise ValueError — the route turns
    this into a 400 telling the operator to run /setup first."""
    with pytest.raises(ValueError, match="/setup"):
        update_owner_username(db, "alice")


# ---------------------------------------------------------------------------
# provision_data: OPENHOST_OWNER_USERNAME stamping
# ---------------------------------------------------------------------------


def test_provision_data_stamps_owner_username(tmp_path: Path) -> None:
    env = _provision(tmp_path, owner_username="alice")
    assert env["OPENHOST_OWNER_USERNAME"] == "alice"
    assert env["OPENHOST_APP_NAME"] == "probe"
    assert env["OPENHOST_ZONE_DOMAIN"] == "example.com"


# ---------------------------------------------------------------------------
# /api/settings/owner_username route
# ---------------------------------------------------------------------------


def test_route_get_requires_auth(settings_client: TestClient[Litestar]) -> None:
    """No cookie => 401, not a silent pass-through."""
    resp = settings_client.get("/api/settings/owner_username")
    assert resp.status_code == 401


def test_route_get_returns_current_value(cfg: Any, settings_client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg, username="zack")
    settings_client.cookies.update(cookies)
    resp = settings_client.get("/api/settings/owner_username")
    assert resp.status_code == 200
    assert resp.json() == {"username": "zack"}


def test_route_set_updates_user_row(cfg: Any, settings_client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg, username="owner")
    settings_client.cookies.update(cookies)
    resp = settings_client.post(
        "/api/settings/owner_username",
        json={"username": "alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"username": "alice"}
    # Pin persisted state, not just the response — guards against a bug
    # where the route returns the new value but forgets to commit.
    assert _read_username_direct(cfg.db_path) == "alice"


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"username": "alice space"}, "letters"),  # validator error verbatim
        ({"username": ""}, "Validation"),  # attrs _not_blank validator rejects empty
    ],
)
def test_route_set_rejects_invalid(
    cfg: Any,
    settings_client: TestClient[Litestar],
    payload: dict[str, Any],
    needle: str,
) -> None:
    cookies = _auth_cookie(cfg, username="owner")
    settings_client.cookies.update(cookies)
    resp = settings_client.post("/api/settings/owner_username", json=payload)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert needle in body["detail"]


# ---------------------------------------------------------------------------
# /setup end-to-end (form-driven persistence + session creation)
# ---------------------------------------------------------------------------


def test_setup_persists_custom_username_and_creates_session(cfg: Any, setup_client: TestClient[Litestar]) -> None:
    """The /setup POST path must (a) persist the operator-supplied username
    into the users row, and (b) issue a session bound to that user — apps
    consuming the session cookie immediately after setup must see the
    chosen name, not the literal 'owner'."""
    resp = setup_client.post(
        "/setup",
        data={"username": "zack", "password": "secretpass1", "confirm_password": "secretpass1"},
    )
    assert resp.status_code == 200, resp.text
    assert _read_username_direct(cfg.db_path) == "zack"
    assert _session_username_after(cfg.db_path) == "zack"


def _session_username_after(db_path: str) -> str | None:
    """Resolve the most recent session row to its owning user's username."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT u.username FROM sessions s JOIN users u ON u.user_id = s.user_id"
            " ORDER BY s.expires_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row["username"]
    finally:
        conn.close()


def test_setup_blank_username_falls_back_to_default(cfg: Any, setup_client: TestClient[Litestar]) -> None:
    resp = setup_client.post(
        "/setup",
        data={"username": "", "password": "secretpass1", "confirm_password": "secretpass1"},
    )
    assert resp.status_code == 200, resp.text
    assert _read_username_direct(cfg.db_path) == DEFAULT_OWNER_USERNAME == "owner"


def test_setup_invalid_username_re_renders_form(cfg: Any, setup_client: TestClient[Litestar]) -> None:
    """Bad input must not persist anything and must re-render the form
    with the validator's error string."""
    resp = setup_client.post(
        "/setup",
        data={
            "username": "alice space",
            "password": "secretpass1",
            "confirm_password": "secretpass1",
        },
    )
    assert resp.status_code == 200
    assert "letters" in resp.text
    conn = sqlite3.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        conn.close()


def test_setup_get_disables_submit_button_to_prevent_double_submit(setup_client: TestClient[Litestar]) -> None:
    """Owner provisioning is a one-time, non-idempotent POST, so a double-click
    must not be able to fire a second /setup request. The rendered form wires a
    submit handler that disables the submit button on first submit."""
    resp = setup_client.get("/setup")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'id="setup-form"' in body
    assert "addEventListener('submit'" in body
    assert "btn.disabled = true" in body


def test_setup_get_explains_username_format_and_validates_client_side(
    setup_client: TestClient[Litestar],
) -> None:
    """The setup form must tell the operator what username format is
    allowed (no silent @-rejection) and wire up live client-side
    validation that mirrors the server-side rule."""
    resp = setup_client.get("/setup")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Explanatory text: the allowed chars and the explicit "no @" note.
    assert "lowercase letters, digits" in body
    assert "<code>@</code>" in body
    # Live client-side validation is wired in via the shared module and
    # the form blocks submit on an invalid value.
    assert "username-validation.js" in body
    assert "OpenHostUsername.usernameError" in body
    assert "reportValidity" in body


# ---------------------------------------------------------------------------
# /login: session resolves to persisted username (mirror invariant of /setup)
# ---------------------------------------------------------------------------


def test_login_creates_session_resolving_to_persisted_username(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """After login, the session row must resolve to the username that's
    actually in the DB — not a stale literal from the old single-owner code."""
    _seed_user(cfg.db_path, "alice", password="loginpass1")

    resp = login_client.post("/login", data={"password": "loginpass1"}, follow_redirects=False)
    assert resp.status_code in (200, 302), resp.text
    assert _session_username_after(cfg.db_path) == "alice"


def test_login_rejects_cross_origin_request(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """A cross-site POST to /login (login CSRF) must be rejected without minting a
    session, so a hostile page can't land the operator in a session it chose."""
    _seed_user(cfg.db_path, "alice", password="loginpass1")

    resp = login_client.post(
        "/login",
        data={"password": "loginpass1"},
        headers={"Origin": "https://evil.example.com"},
        follow_redirects=False,
    )

    assert resp.status_code == 401, resp.text
    assert _session_count(cfg.db_path) == 0


# ---------------------------------------------------------------------------
# /logout: the control surfaced by the dashboard nav button must revoke the
# session and clear the cookie. The button is just a form POST to this route,
# so exercising the route covers what the button triggers.
# ---------------------------------------------------------------------------


def _session_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    finally:
        conn.close()


def test_logout_revokes_session_and_clears_cookie(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """POST /logout (what the dashboard Log out button submits) must delete the
    session row server-side and emit a cleared session cookie, so a stolen/cached
    cookie can't be replayed after logout."""
    user_id = _seed_user(cfg.db_path, "alice", password="loginpass1")
    token = _create_session_for(cfg.db_path, user_id)
    assert _session_count(cfg.db_path) == 1

    login_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = login_client.post("/logout", follow_redirects=False)

    # Redirects the operator back to the login page.
    assert resp.status_code in (200, 302), resp.text
    assert resp.headers.get("location") == "/login"
    # Session row is gone -> the old cookie is now inert server-side.
    assert _session_count(cfg.db_path) == 0
    # And the response clears the cookie (max-age=0).
    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower()


def test_logout_without_session_is_safe(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """Hitting /logout with no session cookie must not error; it just redirects."""
    resp = login_client.post("/logout", follow_redirects=False)
    assert resp.status_code in (200, 302), resp.text
    assert resp.headers.get("location") == "/login"


def test_logout_rejects_cross_origin_request(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """A cross-site POST to /logout (forced-logout CSRF) must be rejected, and the
    victim's session must survive. The browser always sets Origin on cross-origin
    requests, so a mismatching Origin is the signal."""
    user_id = _seed_user(cfg.db_path, "alice", password="loginpass1")
    token = _create_session_for(cfg.db_path, user_id)
    assert _session_count(cfg.db_path) == 1

    login_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = login_client.post(
        "/logout",
        headers={"Origin": "https://evil.example.com"},
        follow_redirects=False,
    )

    # Rejected (NotAuthorizedException -> 401), not a 302 logout.
    assert resp.status_code == 401, resp.text
    # The victim's session must NOT have been revoked.
    assert _session_count(cfg.db_path) == 1


def test_logout_allows_same_origin_request(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """A same-origin POST (Origin matching the target host) must still log out.
    Modern browsers send Origin even on same-origin top-level form posts."""
    user_id = _seed_user(cfg.db_path, "alice", password="loginpass1")
    token = _create_session_for(cfg.db_path, user_id)

    login_client.cookies.set(SESSION_COOKIE_NAME, token)
    # Match the TestClient's base URL host so Origin == target netloc.
    same_origin = str(login_client.base_url).rstrip("/")
    resp = login_client.post(
        "/logout",
        headers={"Origin": same_origin},
        follow_redirects=False,
    )

    assert resp.status_code in (200, 302), resp.text
    assert resp.headers.get("location") == "/login"
    assert _session_count(cfg.db_path) == 0


def test_logout_allows_request_without_origin_header(cfg: Any, login_client: TestClient[Litestar]) -> None:
    """A top-level form post that omits Origin (some browsers/contexts) must still
    work — the check only rejects a present-and-mismatching Origin."""
    user_id = _seed_user(cfg.db_path, "alice", password="loginpass1")
    token = _create_session_for(cfg.db_path, user_id)

    login_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = login_client.post("/logout", follow_redirects=False)  # no Origin header

    assert resp.status_code in (200, 302), resp.text
    assert _session_count(cfg.db_path) == 0


@pytest.mark.parametrize(
    "bad_origin",
    [
        "null",  # sandboxed/opaque iframe — must NOT be treated as "no header"
        "http://testserver.local.evil.com",  # suffix-style lookalike host
        "http://evil.testserver.local",  # subdomain of an attacker domain
        "http://testserver.local:1337",  # right host, wrong port
        "http://evil.example.com",  # plainly different host
    ],
)
def test_logout_rejects_spoofed_or_opaque_origins(
    cfg: Any, login_client: TestClient[Litestar], bad_origin: str
) -> None:
    """Forced-logout CSRF must be blocked for opaque (``null``) and lookalike/mismatched
    origins, and the victim's session must survive each attempt."""
    user_id = _seed_user(cfg.db_path, "alice", password="loginpass1")
    token = _create_session_for(cfg.db_path, user_id)
    assert _session_count(cfg.db_path) == 1

    login_client.cookies.set(SESSION_COOKIE_NAME, token)
    resp = login_client.post("/logout", headers={"Origin": bad_origin}, follow_redirects=False)

    assert resp.status_code == 401, f"{bad_origin!r} -> {resp.status_code}: {resp.text}"
    assert _session_count(cfg.db_path) == 1, f"session revoked by spoofed origin {bad_origin!r}"
