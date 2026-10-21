"""Plain-HTTP cookie provenance and confirmation at real authentication boundaries."""

from __future__ import annotations

import hashlib
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit

import httpx
import pytest
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar import head
from litestar import post
from litestar.connection import ASGIConnection
from litestar.enums import MediaType
from litestar.exceptions import NotAuthorizedException
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient

import compute_space.web.app as appmod
import compute_space.web.middleware.subdomain_proxy as proxymod
from compute_space.config import Config
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import Domain
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests._litestar_helpers import stash_zone_middleware
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import require_owner_or_app_auth
from compute_space.web.auth.auth import verify_owner_auth
from compute_space.web.auth.http_cookie import HttpCookieConfirmationRequired
from compute_space.web.auth.http_cookie import http_cookie_confirmation
from compute_space.web.auth.http_cookie import is_same_origin_http_cookie_request
from compute_space.web.auth.http_cookie import needs_http_cookie_confirmation
from compute_space.web.auth.http_cookie import protect_owner_html
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY
from compute_space.web.setup_app import create_setup_app

ZONE = "testzone.local"
APP_HOST = f"notes.{ZONE}"
SELF = f"http://{APP_HOST}"
OTHER = f"http://other.{ZONE}"
FETCH_HEADERS = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user")
HTTP_ZONE = Domain(name=ZONE, tls=False)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=False)


@pytest.fixture
def cookie(config: Config) -> str:
    # Every production get_db() gets its own file-backed connection, including
    # the ones closed by middleware and the owner-or-app alternative guard.
    with closing(open_db(config)) as db:
        db.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'unused')")
        token = create_session(1, db)
        db.commit()
    return f"{SESSION_COOKIE_NAME}={token}"


@pytest.fixture
def app_id(config: Config) -> str:
    identifier = new_app_id()
    with closing(open_db(config)) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port, status, public_paths) "
            "VALUES (?, 'notes', '1.0', 'serverfull', '/unused', 9000, 'running', '[]')",
            (identifier,),
        )
        db.commit()
    return identifier


@pytest.fixture
def api_token(config: Config) -> str:
    token = "http-cookie-regression-owner-token"
    with closing(open_db(config)) as db:
        db.execute(
            "INSERT INTO api_tokens (token_hash, name, expires_at) VALUES (?, 'tests', '2100-01-01T00:00:00+00:00')",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        db.commit()
    return token


def connection(
    cookie: str = "",
    headers: dict[str, str] | None = None,
    *,
    host: str = APP_HOST,
    method: str = "GET",
    zone: Any = HTTP_ZONE,
    scope_type: str = "http",
    duplicates: tuple[tuple[str, str], ...] = (),
) -> ASGIConnection[Any, Any, Any, Any]:
    scope = make_http_scope(
        method,
        "/private",
        host=host,
        cookie=cookie,
        headers=headers,
        extra_scope={"type": scope_type},
    )
    if zone is not None:
        scope[ZONE_SCOPE_KEY] = zone
    scope["headers"].extend((name.encode(), value.encode()) for name, value in duplicates)
    return ASGIConnection(scope)


def without_metadata(client: TestClient[Any]) -> None:
    # conftest installs browser metadata on every TestClient. Remove all four,
    # rather than sending empty values (which are partial metadata, not absent).
    for name in FETCH_HEADERS:
        client.headers.pop(name, None)
    assert not any(name in client.headers for name in FETCH_HEADERS)


class Document(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def assert_landing(response: httpx.Response, *, head: bool = False) -> str | None:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
        assert directive in csp
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
    assert "refresh" not in response.headers
    if head:
        assert response.content == b""
        return None
    doc = Document(response.text)
    assert "Continue" in response.text
    assert not any(
        tag in {"script", "iframe", "object", "embed", "form", "img", "audio", "video", "base"}
        for tag, _ in doc.elements
    )
    links = []
    for tag, attrs in doc.elements:
        assert not any(name.startswith("on") for name in attrs)
        assert "src" not in attrs and "srcset" not in attrs
        assert not (tag == "meta" and "http-equiv" in attrs)
        if tag == "link":
            assert attrs == {"rel": "icon", "href": "data:,"}
        elif "href" in attrs:
            assert tag == "a"
            links.append(attrs["href"])
    assert len(links) == 1 and isinstance(links[0], str)
    assert "url(" not in response.text.lower() and "@import" not in response.text.lower()
    return links[0]


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize(
    "host,headers",
    [
        (APP_HOST, {"origin": SELF}),
        (APP_HOST, {"origin": SELF + ":80"}),
        (APP_HOST + ":80", {"origin": SELF}),
        (APP_HOST, {"origin": f"http://NOTES.{ZONE.upper()}"}),
        (APP_HOST, {"referer": SELF + "/landing?return=1"}),
        (APP_HOST, {"referer": SELF + "/reports/one,two?columns=a,b"}),
        (APP_HOST + ":8080", {"origin": SELF + ":8080"}),
        (APP_HOST + ":8080", {"referer": SELF + ":8080/landing"}),
        (APP_HOST, {"origin": SELF, "referer": SELF + "/landing"}),
    ],
)
def test_exact_http_origin_or_referer_authenticates(
    cookie: str, method: str, host: str, headers: dict[str, str]
) -> None:
    conn = connection(cookie, headers, host=host, method=method)
    assert is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    verify_owner_auth(conn)


BAD_SOURCES = [
    "",
    "null",
    "not-a-url",
    "//" + APP_HOST,
    "https://" + APP_HOST,
    OTHER,
    SELF + ":8080",
    "http://owner@" + APP_HOST,
    "http://owner:pw@" + APP_HOST,
    SELF + "#fragment",
    SELF + " ",
    SELF + "\t",
    SELF + "," + OTHER,
    SELF + "/page," + OTHER + "/page",
    SELF + "/page," + OTHER.upper() + "/page",
    SELF + " " + OTHER,
    SELF + ":bad",
    SELF + ":65536",
    "http://[broken",
]


@pytest.mark.parametrize("source", BAD_SOURCES)
@pytest.mark.parametrize("same_referer", [False, True])
def test_bad_origin_is_closed_and_matching_referer_never_rescues_it(
    cookie: str, source: str, same_referer: bool
) -> None:
    headers = {"origin": source, "accept": "text/html"}
    if same_referer:
        headers["referer"] = SELF + "/landing"
    conn = connection(cookie, headers)
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException) as exc:
        verify_owner_auth(conn)
    assert not isinstance(exc.value, HttpCookieConfirmationRequired)


@pytest.mark.parametrize("source", [SELF + "/", SELF + "?query", SELF + "?"])
def test_origin_cannot_contain_path_or_query(cookie: str, source: str) -> None:
    conn = connection(cookie, {"origin": source, "referer": SELF + "/", "accept": "text/html"})
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


@pytest.mark.parametrize("source", BAD_SOURCES)
def test_bad_or_foreign_referer_cannot_prove_origin(cookie: str, source: str) -> None:
    # A syntactically valid foreign Referer may request a landing, never access.
    conn = connection(cookie, {"referer": source, "accept": "text/html"})
    assert not is_same_origin_http_cookie_request(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)
    assert needs_http_cookie_confirmation(conn) == (source in {"https://" + APP_HOST, OTHER, SELF + ":8080"})


@pytest.mark.parametrize("referer", [OTHER + "/", "null", "", SELF + "#fragment"])
def test_same_origin_header_does_not_override_conflicting_referer(cookie: str, referer: str) -> None:
    conn = connection(cookie, {"origin": SELF, "referer": referer, "accept": "text/html"})
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


@pytest.mark.parametrize("name,value", [("origin", SELF), ("referer", SELF + "/"), ("host", APP_HOST)])
@pytest.mark.parametrize("duplicate", ["same", "foreign"])
def test_duplicate_provenance_or_host_fails_closed(cookie: str, name: str, value: str, duplicate: str) -> None:
    headers = {"accept": "text/html", "referer": SELF + "/"}
    if name != "host":
        headers[name] = value
    conn = connection(cookie, headers, duplicates=((name, value if duplicate == "same" else "http://evil.example"),))
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


@pytest.mark.parametrize("zone", [None, "not-a-domain", Domain(name=ZONE, tls=True)])
@pytest.mark.parametrize("headers", [{"origin": SELF}, {"referer": SELF + "/"}, {"accept": "text/html"}])
def test_fallback_requires_stashed_non_tls_domain(cookie: str, zone: Any, headers: dict[str, str]) -> None:
    conn = connection(cookie, headers, zone=zone)
    assert conn.scope["scheme"] == "http"  # TLS termination must not enable fallback.
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


def test_websocket_context_never_uses_http_referer_or_confirmation(cookie: str) -> None:
    conn = connection(cookie, {"referer": SELF + "/", "accept": "text/html"}, scope_type="websocket")
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


@pytest.mark.parametrize("name", FETCH_HEADERS[1:])
@pytest.mark.parametrize("value", ["", "navigate"])
@pytest.mark.parametrize("proof", [False, True])
def test_partial_fetch_metadata_blocks_http_fallback(cookie: str, name: str, value: str, proof: bool) -> None:
    headers = {name: value, "accept": "text/html"}
    if proof:
        headers["referer"] = SELF + "/"
    conn = connection(cookie, headers)
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)


@pytest.mark.parametrize("name", ["purpose", "sec-purpose", "x-moz"])
@pytest.mark.parametrize("proof", [False, True])
def test_explicit_prefetch_blocks_proof_and_confirmation(cookie: str, name: str, proof: bool) -> None:
    headers = {name: "prefetch", "accept": "text/html"}
    if proof:
        headers["referer"] = SELF + "/"
    conn = connection(cookie, headers)
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException) as exc:
        verify_owner_auth(conn)
    assert not isinstance(exc.value, HttpCookieConfirmationRequired)


@pytest.mark.parametrize("tls", [False, True])
def test_forwarded_headers_cannot_supply_or_change_provenance(cookie: str, tls: bool) -> None:
    headers = {
        "origin": OTHER,
        "accept": "text/html",
        "x-forwarded-host": "other." + ZONE,
        "x-forwarded-proto": "http",
        "forwarded": f'host="other.{ZONE}";proto=http',
    }
    conn = connection(cookie, headers, zone=Domain(name=ZONE, tls=tls))
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException):
        verify_owner_auth(conn)
    # Even a forged HTTP hint plus a matching Origin cannot downgrade TLS.
    if tls:
        headers["origin"] = SELF
        conn = connection(cookie, headers, zone=Domain(name=ZONE, tls=True))
        assert not is_same_origin_http_cookie_request(conn)
        assert not needs_http_cookie_confirmation(conn)
        with pytest.raises(NotAuthorizedException):
            verify_owner_auth(conn)


def guarded_app(calls: list[str], *, alternative: bool = False) -> Litestar:
    guard = require_owner_or_app_auth if alternative else require_owner_auth

    @get("/owner", guards=[guard], sync_to_thread=False)
    def owner() -> Response[str]:
        calls.append("GET")
        return Response("private owner document", media_type=MediaType.HTML)

    @head("/owner", guards=[guard], sync_to_thread=False, media_type=MediaType.HTML)
    def owner_head() -> None:
        calls.append("HEAD")

    @post("/owner", guards=[guard], sync_to_thread=False, status_code=200)
    def mutate() -> dict[str, bool]:
        calls.append("POST")
        return {"executed": True}

    return Litestar(
        [owner, owner_head, mutate],
        middleware=[stash_zone_middleware],
        openapi_config=None,
        exception_handlers={NotAuthorizedException: appmod._auth_required_handler},
        after_request=protect_owner_html,
    )


def test_low_level_service_response_passes_html_hook_unchanged(cookie: str) -> None:
    @get("/provider", guards=[require_owner_auth], sync_to_thread=False)
    def provider(request: Request[Any, Any, Any]) -> ASGIResponse:
        return Response(
            "provider html",
            media_type=MediaType.HTML,
            headers={
                "content-security-policy": "frame-ancestors https://provider.test",
                "referrer-policy": "no-referrer",
            },
        ).to_asgi_response(None, request=request)

    app = Litestar(
        [provider], middleware=[stash_zone_middleware], after_request=protect_owner_html, openapi_config=None
    )
    with TestClient(app, base_url=SELF) as client:
        without_metadata(client)
        response = client.get("/provider", headers={"cookie": cookie, "referer": SELF + "/"})
    assert response.status_code == 200 and response.text == "provider html"
    assert response.headers["content-security-policy"] == "frame-ancestors https://provider.test"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "x-frame-options" not in response.headers


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("referer", [None, OTHER + "/untrusted"])
def test_unknown_html_navigation_is_inert_until_user_continue(cookie: str, method: str, referer: str | None) -> None:
    calls: list[str] = []
    headers = {"cookie": cookie, "accept": "text/html"}
    if referer:
        headers["referer"] = referer
    if method == "HEAD":
        # TestClient suppresses HEAD body bytes even if the server emits them.
        # Check the production response content before that transport filtering.
        conn = connection(cookie, headers, method="HEAD")
        assert needs_http_cookie_confirmation(conn)
        with pytest.raises(HttpCookieConfirmationRequired):
            verify_owner_auth(conn)
        assert http_cookie_confirmation(conn).content == ""
    with TestClient(guarded_app(calls), base_url=SELF) as client:
        without_metadata(client)
        landing = client.request(method, "/owner?keep=1", headers=headers, follow_redirects=False)
        destination = assert_landing(landing, head=method == "HEAD")
        assert calls == []
        assert "private owner" not in landing.text
        if method == "GET":
            assert destination == SELF + "/owner?keep=1"
        response = client.get(
            SELF + "/owner?keep=1", headers={"cookie": cookie, "accept": "text/html", "referer": str(landing.url)}
        )
        assert response.status_code == 200
        assert response.text == "private owner document"
        assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        assert response.headers["referrer-policy"] == "same-origin"
        assert calls == ["GET"]


@pytest.mark.parametrize(
    "method,accept",
    [
        ("POST", "text/html"),
        ("POST", "application/json"),
        ("GET", "application/json"),
        ("HEAD", "application/json"),
        ("GET", "text/html,application/json"),
        ("GET", "*/*"),
    ],
)
def test_post_and_api_requests_do_not_confirm_or_execute(cookie: str, method: str, accept: str) -> None:
    calls: list[str] = []
    with TestClient(guarded_app(calls), base_url=SELF) as client:
        without_metadata(client)
        response = client.request(
            method, "/owner", headers={"cookie": cookie, "accept": accept}, follow_redirects=False
        )
    assert response.status_code == (302 if method == "GET" and accept == "*/*" else 401)
    assert "Continue" not in response.text
    assert calls == []


@pytest.mark.parametrize("source", [None, "null", OTHER])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_api_key_rescues_cookie_without_http_provenance(
    cookie: str, api_token: str, source: str | None, method: str
) -> None:
    calls: list[str] = []
    headers = {"cookie": cookie, "accept": "text/html", "authorization": f"Bearer {api_token}"}
    if source is not None:
        headers["origin"] = source
    with TestClient(guarded_app(calls), base_url=SELF) as client:
        without_metadata(client)
        response = client.request(method, "/owner", headers=headers)
    assert response.status_code == 200
    if method == "POST":
        assert response.json() == {"executed": True}
    else:
        assert response.text == "private owner document"
    assert calls == [method]


@pytest.mark.parametrize(
    "source,allowed",
    [
        (SELF, True),
        (None, False),
        ("null", False),
        ("http://missing." + ZONE, False),
        ("http://[broken", False),
        (SELF + ":bad", False),
        (SELF + ":65536", False),
    ],
)
def test_owner_or_app_alternative_preserves_real_app_auth(
    cookie: str, app_id: str, source: str | None, allowed: bool
) -> None:
    calls: list[str] = []
    headers = {"cookie": cookie, "accept": "text/html"}
    if source is not None:
        # The route's owner host differs from the app Origin; only app auth can pass.
        headers["origin"] = source
    with TestClient(guarded_app(calls, alternative=True), base_url=f"http://{ZONE}") as client:
        without_metadata(client)
        response = client.get("/owner", headers=headers, follow_redirects=False)
    if allowed:
        assert response.status_code == 200 and response.text == "private owner document"
        assert calls == ["GET"]
    elif source is None:
        assert_landing(response)
        assert calls == []
    else:
        assert response.status_code == 302
        assert "Continue" not in response.text and calls == []


def test_owner_or_app_accepts_server_app_token(config: Config, app_id: str) -> None:
    token = "http-cookie-regression-app-token"
    with closing(open_db(config)) as db:
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, hashlib.sha256(token.encode()).hexdigest()),
        )
        db.commit()
    calls: list[str] = []
    with TestClient(guarded_app(calls, alternative=True), base_url=f"http://{ZONE}") as client:
        without_metadata(client)
        response = client.post("/owner", headers={"authorization": f"Bearer {token}", "accept": "application/json"})
    assert response.status_code == 200 and calls == ["POST"]


@pytest.fixture
def full_app(config: Config, monkeypatch: pytest.MonkeyPatch) -> Any:
    # Skip machine/bootstrap work; request routing, DB/domain lookup, guards,
    # exception handling, templates, and response hooks are all production code.
    monkeypatch.setattr(appmod, "_full_app_bootstrap", lambda _config: None)
    return appmod.create_app(config, Mock(), Mock())


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str]]]:
    calls: list[list[tuple[str, str]]] = []

    async def proxy(request: Any, *, target: Any, extra_headers: list[tuple[str, str]]) -> Any:
        calls.append(extra_headers)
        # Backend framing must survive unchanged; owner hooks are router-only.
        return Response(
            "backend document",
            media_type=MediaType.HTML,
            headers={
                "content-security-policy": "frame-ancestors *",
                "x-frame-options": "backend-value",
                "referrer-policy": "no-referrer",
            },
        ).to_asgi_response(None, request=request)

    monkeypatch.setattr(proxymod, "proxy_http_request", proxy)
    return calls


@pytest.mark.parametrize("status", ["running", "starting", "building"])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_outer_middleware_confirms_before_backend_startup_or_public_path(
    config: Config,
    cookie: str,
    app_id: str,
    full_app: Any,
    backend: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    public: bool,
    method: str,
) -> None:
    with closing(open_db(config)) as db:
        db.execute(
            "UPDATE apps SET status=?, public_paths=? WHERE app_id=?", (status, '["/"]' if public else "[]", app_id)
        )
        db.commit()
    public_check = Mock(wraps=proxymod.is_public_request_path)
    starting = Mock(wraps=proxymod.app_starting_response)
    monkeypatch.setattr(proxymod, "is_public_request_path", public_check)
    monkeypatch.setattr(proxymod, "app_starting_response", starting)
    with TestClient(full_app, base_url=SELF + ":8080") as client:
        without_metadata(client)
        landing = client.request(method, "/private?keep=1", headers={"cookie": cookie, "accept": "text/html"})
        assert_landing(landing, head=method == "HEAD")
        assert backend == []
        public_check.assert_not_called()
        starting.assert_not_called()
        response = client.get(
            "/private?keep=1", headers={"cookie": cookie, "accept": "text/html", "referer": str(landing.url)}
        )
    public_check.assert_not_called()
    if status == "running":
        assert response.status_code == 200 and response.text == "backend document"
        assert len(backend) == 1
        assert proxymod.IS_OWNER_HEADER in backend[0]
        assert response.headers["content-security-policy"] == "frame-ancestors *"
        assert response.headers["x-frame-options"] == "backend-value"
        assert response.headers["referrer-policy"] == "no-referrer"
        starting.assert_not_called()
    else:
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
        assert "http://testzone.local:8080" in response.text
        starting.assert_called_once()
        assert backend == []


@pytest.mark.parametrize("cookie_state", ["absent", "invalid", "expired", "revoked"])
@pytest.mark.parametrize("public", [False, True])
def test_outer_anonymous_private_404_and_public_normal(
    config: Config,
    cookie: str,
    app_id: str,
    full_app: Any,
    backend: list[Any],
    cookie_state: str,
    public: bool,
) -> None:
    with closing(open_db(config)) as db:
        db.execute("UPDATE apps SET public_paths=? WHERE app_id=?", ('["/public"]' if public else "[]", app_id))
        if cookie_state == "expired":
            db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        elif cookie_state == "revoked":
            db.execute("DELETE FROM sessions")
        db.commit()
    headers = {"accept": "text/html"}
    if cookie_state != "absent":
        headers["cookie"] = f"{SESSION_COOKIE_NAME}=invalid" if cookie_state == "invalid" else cookie
    with TestClient(full_app, base_url=SELF) as client:
        without_metadata(client)
        response = client.get("/public", headers=headers, follow_redirects=False)
    assert "Continue" not in response.text
    if public:
        assert response.status_code == 200 and response.text == "backend document"
        assert len(backend) == 1 and proxymod.IS_OWNER_HEADER not in backend[0]
    else:
        assert response.status_code == 404 and backend == []


def test_outer_tls_domain_over_http_transport_stays_rejected(
    config: Config,
    cookie: str,
    app_id: str,
    full_app: Any,
    backend: list[Any],
) -> None:
    with closing(open_db(config)) as db:
        db.execute("UPDATE domains SET tls=1")
        db.commit()
    with TestClient(full_app, base_url=SELF) as client:
        without_metadata(client)
        response = client.get(
            "/private",
            headers={
                "cookie": cookie,
                "origin": SELF,
                "referer": SELF + "/",
                "accept": "text/html",
                "x-forwarded-proto": "http",
            },
        )
    assert response.status_code == 404
    assert backend == [] and "Continue" not in response.text


@pytest.mark.parametrize("path", ["/", "/login", "/setup"])
def test_owner_html_is_frame_protected_through_full_app_hook(cookie: str, full_app: Any, path: str) -> None:
    with TestClient(full_app, base_url=f"http://{ZONE}") as client:
        without_metadata(client)
        # /setup redirects to the real dashboard. /login without a cookie renders
        # the actual login template; / renders the authenticated dashboard.
        headers = {"accept": "text/html"}
        if path != "/login":
            client.cookies.set(SESSION_COOKIE_NAME, cookie.split("=", 1)[1], domain=ZONE)
            headers["referer"] = f"http://{ZONE}/landing"
        response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert (" - Login" if path == "/login" else " - Dashboard") in response.text
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("path", ["/", "/setup"])
def test_setup_html_is_frame_protected_through_setup_app_hook(config: Config, path: str) -> None:
    with TestClient(create_setup_app(config), base_url=f"http://{ZONE}") as client:
        without_metadata(client)
        response = client.get(path)
    assert response.status_code == 200 and "setup" in response.text.lower()
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("path", ["/", "/login?next=%2Fsettings"])
def test_root_and_login_cookie_navigation_confirm_before_owner_content(cookie: str, full_app: Any, path: str) -> None:
    with TestClient(full_app, base_url=f"http://{ZONE}") as client:
        without_metadata(client)
        landing = client.get(path, headers={"cookie": cookie, "accept": "text/html"}, follow_redirects=False)
        destination = assert_landing(landing)
        assert destination == f"http://{ZONE}" + path
        assert "Dashboard" not in landing.text and "Password" not in landing.text
        response = client.get(
            destination,
            headers={"cookie": cookie, "accept": "text/html", "referer": str(landing.url)},
            follow_redirects=False,
        )
    assert response.status_code == (302 if path.startswith("/login") else 200)
    if path.startswith("/login"):
        assert response.headers["location"] == "/settings"


@pytest.mark.parametrize(
    "path,query,expected_path,expected_query",
    [
        ("//evil.example/path", "next=https://evil.example", "//evil.example/path", "next=https://evil.example"),
        (
            '/a"<tag>#fragment',
            'x="<tag>&next=//evil.example',
            "/a%22%3Ctag%3E%23fragment",
            "x=%22%3Ctag%3E&next=//evil.example",
        ),
        (
            "/already%2Fencoded",
            "a=1&b=%23fragment&c=two%20words",
            "/already%2Fencoded",
            "a=1&b=%23fragment&c=two%20words",
        ),
    ],
)
def test_confirmation_link_is_absolute_escaped_and_preserves_target(
    cookie: str,
    path: str,
    query: str,
    expected_path: str,
    expected_query: str,
) -> None:
    conn = connection(
        cookie,
        {"accept": "text/html", "x-forwarded-host": "evil.example", "x-forwarded-proto": "https"},
        host=APP_HOST + ":8080",
    )
    # ASGI path is decoded; raw_path retains escapes with routing significance.
    conn.scope["path"] = unquote(path)
    conn.scope["raw_path"] = quote(path, safe="/%").encode()
    conn.scope["query_string"] = query.encode()
    assert needs_http_cookie_confirmation(conn)
    result = http_cookie_confirmation(conn)
    # Assert the actual rendered HTML as well as the decoded link, to catch
    # attribute injection and accidental external resources/automatic actions.
    response = httpx.Response(200, text=result.content, headers={**result.headers, "content-type": "text/html"})
    destination = assert_landing(response)
    assert destination == SELF + ":8080" + expected_path + "?" + expected_query
    parsed = urlsplit(destination)
    assert (parsed.scheme, parsed.hostname, parsed.port) == ("http", APP_HOST, 8080)
    assert parsed.fragment == ""
    if "&" in query:
        assert "&amp;" in response.text
    assert 'href="http://' in response.text
    assert '"<tag>' not in response.text


@pytest.mark.parametrize("host", ["", "[broken", APP_HOST + ":bad", APP_HOST + ":65536", "owner@" + APP_HOST])
def test_invalid_target_never_qualifies_for_confirmation_or_reaches_assertion(cookie: str, host: str) -> None:
    # An empty Host must not acquire cookie provenance from Litestar's fallback
    # to scope['server']; qualification requires the browser's actual authority.
    conn = connection(cookie, {"accept": "text/html"}, host=host)
    assert not is_same_origin_http_cookie_request(conn)
    assert not needs_http_cookie_confirmation(conn)
    with pytest.raises(NotAuthorizedException) as exc:
        verify_owner_auth(conn)
    assert not isinstance(exc.value, HttpCookieConfirmationRequired)
