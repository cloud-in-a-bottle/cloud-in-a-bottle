"""Phase 2: links, redirects, and cookies are built on the domain the request arrived
on, not a single canonical one — so a `.local` request stays on `.local` (http) and a
public request stays on the public domain (https)."""

from __future__ import annotations

import sqlite3

from compute_space.config import Domain
from compute_space.core.domain_store import DomainRecord
from compute_space.core.domain_store import seed_domains
from compute_space.db.schema import schema_path
from compute_space.web.auth.auth import build_login_url
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie
from compute_space.web.routes.pages.login import _validated_next

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


# --- _validated_next: accepts any configured domain -------------------------------


def _db() -> sqlite3.Connection:
    """In-memory DB seeded with PUBLIC (primary) + LOCAL, for the DB-backed ``match_domain``."""
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


def test_validated_next_rejects_userinfo_host_spoof() -> None:
    # `host.example.com:1@evil.com` navigates to evil.com; the port before `@` must not fool
    # the domain match (regression: matching on netloc split the userinfo at the first colon).
    db = _db()
    assert _validated_next("https://host.example.com:1@evil.com/phish", db) is None
    assert _validated_next("https://myapp.host.example.com@evil.com/phish", db) is None
    # userinfo in front of a real configured host still resolves to that host, so it's allowed.
    assert _validated_next("https://evil.com@host.example.com/x", db) == "https://evil.com@host.example.com/x"


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
