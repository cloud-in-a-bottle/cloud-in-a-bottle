"""Phase 2: links, redirects, and cookies are built on the domain the request arrived
on, not a single canonical one — so a `.local` request stays on `.local` (http) and a
public request stays on the public domain (https)."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import httpx
import pytest
from hypothesis import given
from hypothesis import note
from hypothesis import strategies as st
from litestar import Litestar
from litestar.di import Provide

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import host_with_request_port
from compute_space.core.domains import seed_domains
from compute_space.db.schema import schema_path
from compute_space.web.auth.auth import build_login_url
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie
from compute_space.web.routes.pages.login import _validated_next
from compute_space.web.routes.pages.login import login_get

PUBLIC = Domain("host.example.com", tls=True)
LOCAL = Domain("myhost.local", tls=False, mdns=True)


# --- build_login_url: redirect stays on the arriving domain ------------------------


def test_login_url_on_local_domain_is_http_and_local() -> None:
    url = build_login_url(LOCAL, "myapp.myhost.local", "/private", "")
    assert url == "http://myhost.local/login?next=http%3A%2F%2Fmyapp.myhost.local%2Fprivate"


def test_login_url_on_public_domain_is_https_and_public() -> None:
    url = build_login_url(PUBLIC, "myapp.host.example.com", "/x", "a=b")
    assert url.startswith("https://host.example.com/login?next=")
    assert "https%3A%2F%2Fmyapp.host.example.com%2Fx%3Fa%3Db" in url


# --- port preservation: a non-default access port survives into links/redirects ---
# The instance can be reached on a non-default port (SSH tunnel :8088, NAT forward
# :8080) with a wildcard-to-loopback domain like lvh.me.  The /login redirect must
# keep that port instead of bouncing the user to port 80.


def test_login_url_preserves_request_port() -> None:
    url = build_login_url(LOCAL, "myhost.local:8088", "/private", "")
    assert url == "http://myhost.local:8088/login?next=http%3A%2F%2Fmyhost.local%3A8088%2Fprivate"


def test_login_url_preserves_port_from_app_subdomain() -> None:
    # Arrived on an app subdomain with a port; /login goes to the router host, same port.
    url = build_login_url(PUBLIC, "app.host.example.com:8443", "/x", "")
    assert url.startswith("https://host.example.com:8443/login?next=")


def test_login_url_no_port_when_default() -> None:
    url = build_login_url(PUBLIC, "app.host.example.com", "/x", "")
    assert url.startswith("https://host.example.com/login?next=")


def test_host_with_request_port() -> None:
    assert host_with_request_port("lvh.me", "lvh.me:8088") == "lvh.me:8088"
    assert host_with_request_port("lvh.me", "foo.lvh.me:8088") == "lvh.me:8088"  # port copied off subdomain
    assert host_with_request_port("lvh.me", "lvh.me") == "lvh.me"  # no port → unchanged
    assert host_with_request_port("foo.lvh.me", "bar.lvh.me:8080") == "foo.lvh.me:8080"
    assert host_with_request_port("lvh.me", "") == "lvh.me"
    assert host_with_request_port("lvh.me", "[::1]") == "lvh.me"  # non-numeric tail → no port


# --- _validated_next: accepts any configured domain -------------------------------


def _db() -> sqlite3.Connection:
    """In-memory DB seeded with PUBLIC (primary) + LOCAL, for the DB-backed ``Domain.match``."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    seed_domains(conn, PUBLIC, [DomainRecord(LOCAL.name, LOCAL.tls, LOCAL.mdns)])
    return conn


def test_validated_next_allows_relative_path() -> None:
    assert _validated_next("/dashboard", _db()) == "/dashboard"


def test_validated_next_allows_both_domains() -> None:
    db = _db()
    assert _validated_next("https://myapp.host.example.com/x", db) == "https://myapp.host.example.com/x"
    assert _validated_next("http://myapp.myhost.local/x", db) == "http://myapp.myhost.local/x"
    assert _validated_next("http://myhost.local/", db) == "http://myhost.local/"


def test_validated_next_rejects_foreign_domain() -> None:
    assert _validated_next("https://evil.example.org/phish", _db()) is None


@given(
    label=st.from_regex(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", fullmatch=True),
    prefix=st.one_of(st.sampled_from(["http://", "https://"]), st.text(alphabet="/", min_size=2)),
    path=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/._~-"),
)
def test_validated_next_rejects_foreign_network_locations(label: str, prefix: str, path: str) -> None:
    # HTTP(S) browsers interpret two or more leading slashes as an authority,
    # including spellings that urllib.parse treats as a relative path.
    target = f"{prefix}{label}.invalid/{path}"
    note(f"redirect target: {target!r}")
    with closing(_db()) as db:
        assert _validated_next(target, db) is None


def test_validated_next_rejects_userinfo_host_spoof() -> None:
    # `host.example.com:1@evil.com` navigates to evil.com; the port before `@` must not fool
    # the domain match (regression: matching on netloc split the userinfo at the first colon).
    db = _db()
    assert _validated_next("https://host.example.com:1@evil.com/phish", db) is None
    assert _validated_next("https://myapp.host.example.com@evil.com/phish", db) is None
    # userinfo in front of a real configured host still resolves to that host, so it's allowed.
    assert _validated_next("https://evil.com@host.example.com/x", db) == "https://evil.com@host.example.com/x"


@pytest.mark.parametrize(
    "target",
    [
        " ///outside.invalid/",
        "\t//outside.invalid/",
        "/\\outside.invalid/",
        "https://outside.invalid\\@host.example.com/",
        "/dashboard\n",
        "/dashboard\x7f",
        "javascript://host.example.com/",
        "ftp://host.example.com/",
        "https:////outside.invalid/",
        "https://[invalid/",
        "//[invalid/",
        "https://host.example.com:invalid/",
        "https://host.example.com:65536/",
    ],
)
def test_validated_next_rejects_ambiguous_or_malformed_targets(target: str) -> None:
    with closing(_db()) as db:
        assert _validated_next(target, db) is None


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "dashboard",
        "?tab=apps",
        "#settings",
        "/some%20path",
        "//host.example.com/apps",
        "https://myapp.host.example.com:8443/path?query=value#section",
    ],
)
def test_validated_next_preserves_safe_target_forms(target: str) -> None:
    with closing(_db()) as db:
        assert _validated_next(target, db) == target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("///outside.invalid/", "/"),
        ("https://outside.invalid/path", "/"),
        ("/dashboard", "/dashboard"),
        ("https://myapp.host.example.com:8443/path", "https://myapp.host.example.com:8443/path"),
    ],
)
async def test_authenticated_login_validates_return_target(target: str, expected: str) -> None:
    with closing(_db()) as db:
        db.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'unused')")
        token = create_session(1, db)

        def provide_test_db() -> sqlite3.Connection:
            return db

        app = Litestar(
            route_handlers=[login_get],
            dependencies={"db": Provide(provide_test_db, sync_to_thread=False)},
            openapi_config=None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"https://{PUBLIC.name}",
            cookies={SESSION_COOKIE_NAME: token},
        ) as client:
            response = await client.get("/login", params={"next": target})

        assert response.status_code == 302
        assert response.headers["location"] == expected


# --- cookies: scoped + Secure per arriving domain ---------------------------------


def test_session_cookie_local_is_local_scoped_and_insecure() -> None:
    c = build_session_cookie("tok", LOCAL)
    assert c.domain == "myhost.local"
    assert c.secure is False


def test_session_cookie_public_is_public_scoped_and_secure() -> None:
    c = build_session_cookie("tok", PUBLIC)
    assert c.domain == "host.example.com"
    assert c.secure is True


def test_clear_cookie_matches_scope_and_secure() -> None:
    local = clear_session_cookie(LOCAL)
    assert local.domain == "myhost.local" and local.secure is False and local.max_age == 0
    public = clear_session_cookie(PUBLIC)
    assert public.domain == "host.example.com" and public.secure is True and public.max_age == 0
