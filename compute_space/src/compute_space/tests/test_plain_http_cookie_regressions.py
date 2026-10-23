"""Regressions for plain-HTTP cookie origin evidence and startup retries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar import Request
from litestar import get
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient

import compute_space.web.auth.auth as auth
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import Domain
from compute_space.db.schema import schema_path
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests._litestar_helpers import stash_zone_middleware
from compute_space.tests.conftest import _make_test_config
from compute_space.web.helpers.app_starting import app_starting_response
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY

ZONE = "testzone.local"
HOST = f"notes.{ZONE}"


@pytest.fixture
def cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=False)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(Path(schema_path()).read_text())
    connection.execute("INSERT INTO users (user_id, username, password_hash) VALUES (1, 'owner', 'unused')")
    token = create_session(1, connection)
    monkeypatch.setattr(auth, "get_db", lambda: connection)
    try:
        yield f"{SESSION_COOKIE_NAME}={token}"
    finally:
        connection.close()


def request(cookie: str, headers: dict[str, str], *, host: str = HOST) -> ASGIConnection[Any, Any, Any, Any]:
    return ASGIConnection(
        make_http_scope(
            "POST",
            "/owner",
            host=host,
            cookie=cookie,
            headers=headers,
            extra_scope={ZONE_SCOPE_KEY: Domain(ZONE, tls=False)},
        )
    )


@pytest.mark.parametrize("header", ["Origin", "Referer"])
@pytest.mark.parametrize("host", [HOST, HOST + ":18080"])
def test_plain_http_proof_must_match_scheme(cookie: str, header: str, host: str) -> None:
    suffix = "/source" if header == "Referer" else ""
    # Positive control proves this is an authenticated HTTP request, not a
    # missing cookie, TLS-only zone, or missing domain-stashing fixture.
    auth.verify_owner_auth(request(cookie, {header: f"http://{host}{suffix}"}, host=host))
    with pytest.raises(NotAuthorizedException):
        auth.verify_owner_auth(request(cookie, {header: f"https://{host}{suffix}"}, host=host))


@pytest.mark.parametrize("header", ["Origin", "Referer"])
@pytest.mark.parametrize("host,source", [(HOST, HOST + ":80"), (HOST + ":80", HOST)])
def test_plain_http_default_port_is_equivalent(cookie: str, header: str, host: str, source: str) -> None:
    suffix = "/source" if header == "Referer" else ""
    auth.verify_owner_auth(request(cookie, {header: f"http://{host}{suffix}"}, host=host))
    # Omitting the HTTP default port does not make a different origin.
    auth.verify_owner_auth(request(cookie, {header: f"http://{source}{suffix}"}, host=host))


@pytest.mark.parametrize("origin", [HOST, f"http://{HOST}/path", f"http://{HOST}?query", f"http://user@{HOST}"])
def test_malformed_origin_cannot_prove_cookie_access(cookie: str, origin: str) -> None:
    auth.verify_owner_auth(request(cookie, {"Origin": f"http://{HOST}"}))
    with pytest.raises(NotAuthorizedException):
        auth.verify_owner_auth(request(cookie, {"Origin": origin}))


def test_safe_plain_http_cookie_skips_api_validation(cookie: str) -> None:
    connection = request(cookie, {"Origin": f"http://{HOST}", "Authorization": "Bearer unrelated-app-credential"})
    with patch.object(auth, "validate_api_token", wraps=auth.validate_api_token) as validate:
        auth.verify_owner_auth(connection)
    validate.assert_not_called()


def test_starting_page_retains_same_origin_reload_evidence(cookie: str) -> None:
    @get("/waiting", guards=[auth.require_owner_auth], sync_to_thread=False)
    def waiting(request: Request[Any, Any, Any]) -> ASGIResponse:
        return app_starting_response(request)

    app = Litestar([waiting], middleware=[stash_zone_middleware], openapi_config=None)
    with TestClient(app, base_url=f"http://{HOST}:18080") as client:
        for name in ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user"):
            client.headers.pop(name, None)
        response = client.get(
            "/waiting",
            headers={"cookie": cookie, "accept": "text/html", "referer": f"http://{HOST}:18080/waiting"},
        )
    assert response.status_code == 503
    assert "app-starting" in response.text
    # A real Chromium reload after a no-referrer response omits Referer and
    # returns to Continue instead of automatically opening the ready app.
    assert response.headers["referrer-policy"] == "same-origin"
