import hashlib
import json
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core import app_definition_secrets
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.core.service_interface.builtin_services import APP_DEFINITIONS_SERVICE_URL
from compute_space.core.service_interface.services import list_all_service_providers
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import fake_secrets
from compute_space.tests.test_app_definitions import seed_app
from compute_space.tests.test_app_definitions import seed_grant
from compute_space.tests.test_app_definitions import seed_provider
from compute_space.web.app import _login_required_redirect
from compute_space.web.routes.api.app_definitions import api_app_definitions_routes
from compute_space.web.routes.services_v2 import services_v2_routes

OWNER_PATH = "/api/app-definitions/export"
SERVICE_PATH = "/api/services/v2/call/definitions/export"
APP_TOKEN = "synthetic-consumer-token"
API_TOKEN = "synthetic-owner-api-token"
CONSUMER_MANIFEST = f"""
[app]
name = "consumer"
version = "0.1.0"
[runtime.container]
image = "Dockerfile"
port = 8080
[[services.v2.consumes]]
service = "{APP_DEFINITIONS_SERVICE_URL}"
shortname = "definitions"
version = ">=0.1.0,<0.2.0"
grants = [{{mode = "private"}}]
"""


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient[Litestar]]:
    config = _make_test_config(tmp_path)
    with closing(get_db()) as db:
        seed_app(db, "consumer", manifest=CONSUMER_MANIFEST)
        db.execute(
            "INSERT INTO app_tokens VALUES (?, ?)", ("consumer", hashlib.sha256(APP_TOKEN.encode()).hexdigest())
        )
        db.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES ('test', ?, '')",
            (hashlib.sha256(API_TOKEN.encode()).hexdigest(),),
        )
        db.commit()
    # The outer app deliberately has the production HTML login handler; the export's local
    # handler must still return JSON, 401 and no-store even without an Accept header.
    app = Litestar(
        route_handlers=[api_app_definitions_routes, services_v2_routes],
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
        exception_handlers={NotAuthorizedException: _login_required_redirect},
        openapi_config=None,
    )
    with TestClient(app=app) as test_client:
        test_client.cookies.update(auth_cookie(config))
        yield test_client


def use_app_token(client: TestClient[Litestar]) -> None:
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {APP_TOKEN}"


def approve(mode: str, *, scope: str = "global", provider: str = "") -> None:
    with closing(get_db()) as db:
        seed_grant(db, "consumer", {"mode": mode}, service=APP_DEFINITIONS_SERVICE_URL, scope=scope, provider=provider)


def assert_json_no_store(response: httpx.Response) -> None:
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("body", [{}, {"mode": "sharing"}, {"mode": "private"}])
def test_owner_session_default_mode_and_private_envelope(client: TestClient[Litestar], body: dict[str, str]) -> None:
    response = client.post(OWNER_PATH, json=body)
    assert response.status_code == 200
    assert_json_no_store(response)
    assert response.json()["mode"] == body.get("mode", "sharing")
    assert ("secret_values" in response.json()) == (body.get("mode") == "private")
    assert ("missing_secret_keys" in response.json()) == (body.get("mode") == "private")
    if body.get("mode") == "private":
        assert response.json()["secret_values"] == {}
        assert response.json()["missing_secret_keys"] == []
    assert "\n  " in response.text
    assert response.text.endswith("\n")
    assert SENTINEL not in response.text


def test_owner_api_key_auth(client: TestClient[Litestar]) -> None:
    client.cookies.clear()
    response = client.post(OWNER_PATH, json={}, headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 200
    assert_json_no_store(response)


@pytest.mark.parametrize("auth", ["anonymous", "app", "invalid", "expired-api", "expired-session", "spoofed"])
def test_owner_export_denies_nonowners_with_json_no_store(client: TestClient[Litestar], auth: str) -> None:
    headers = {}
    if auth != "expired-session":
        client.cookies.clear()
    if auth == "app":
        headers["Authorization"] = f"Bearer {APP_TOKEN}"
    elif auth == "invalid":
        headers["Authorization"] = "Bearer invalid"
    elif auth in {"expired-api", "expired-session"}:
        with closing(get_db()) as db:
            table = "api_tokens" if auth == "expired-api" else "sessions"
            db.execute(f"UPDATE {table} SET expires_at='2000-01-01T00:00:00+00:00'")
            db.commit()
        if auth == "expired-api":
            headers["Authorization"] = f"Bearer {API_TOKEN}"
    elif auth == "spoofed":
        headers = {
            "X-OpenHost-Consumer-Id": ROUTER_APP_ID,
            "X-OpenHost-Permissions": '[{"grant":{"mode":"private"},"scope":"global"}]',
        }
    response = client.post(OWNER_PATH, json={"mode": "private"}, headers=headers, follow_redirects=False)
    assert response.status_code == 401
    assert_json_no_store(response)
    assert "secret_values" not in response.json()
    assert "missing_secret_keys" not in response.json()


@pytest.mark.parametrize("origin", ["https://evil.example", "http://consumer.testzone.local", "null"])
def test_owner_session_denies_cross_origin(client: TestClient[Litestar], origin: str) -> None:
    response = client.post(OWNER_PATH, json={}, headers={"Origin": origin})
    assert response.status_code == 401
    assert_json_no_store(response)


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize(
    "body",
    [
        {"mode": None},
        {"mode": 1},
        {"mode": True},
        {"mode": []},
        {"mode": {}},
        {"mode": "Sharing"},
        {"mode": "all"},
        None,
        [],
        "sharing",
    ],
)
def test_invalid_modes_fail_400(client: TestClient[Litestar], path: str, body: object) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("private")
    # httpx's json=None means no body, so encode explicitly to test the JSON null case.
    response = client.post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert_json_no_store(response)


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("content", ["", f'{{"mode": {SENTINEL}', "{broken"])
def test_malformed_json_errors_are_sanitized(client: TestClient[Litestar], path: str, content: str) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
    response = client.post(path, content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert_json_no_store(response)
    assert SENTINEL not in response.text


@pytest.mark.parametrize("requested", ["sharing", "private"])
def test_requested_manifest_grant_is_not_approval_and_spoofed_headers_do_not_authorize(
    client: TestClient[Litestar], requested: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_app_token(client)
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("unapproved Secrets call"))
    response = client.post(
        SERVICE_PATH,
        json={"mode": requested},
        headers={
            "X-OpenHost-Consumer-Id": ROUTER_APP_ID,
            "X-OpenHost-Permissions": '[{"grant":{"mode":"private"},"scope":"global"}]',
        },
    )
    assert response.status_code == 403
    assert_json_no_store(response)
    required = response.json()["required_grant"]
    assert response.json()["code"] == "permission_required"
    assert required["grant"] == {"mode": requested}
    assert required["scope"] == "global"
    assert "/approve-permissions-v2?" in required["grant_url"]
    assert "consumer" in required["grant_url"]


@pytest.mark.parametrize(
    ("grant", "mode", "status"),
    [
        ("sharing", "sharing", 200),
        ("sharing", "private", 403),
        ("private", "private", 200),
        ("private", "sharing", 200),
    ],
)
def test_service_grant_modes_and_revoke(client: TestClient[Litestar], grant: str, mode: str, status: int) -> None:
    use_app_token(client)
    approve(grant)
    response = client.post(SERVICE_PATH, json={"mode": mode})
    assert response.status_code == status
    assert_json_no_store(response)
    if status == 200:
        assert response.json()["mode"] == mode
    revoke_permission_v2("consumer", APP_DEFINITIONS_SERVICE_URL, {"mode": grant})
    assert client.post(SERVICE_PATH, json={"mode": mode}).status_code == 403


def test_service_default_mode_requires_sharing_global_grant(client: TestClient[Litestar]) -> None:
    use_app_token(client)
    approve("private", scope="app", provider=ROUTER_APP_ID)
    assert client.post(SERVICE_PATH, json={}).status_code == 403
    approve("sharing")
    assert client.post(SERVICE_PATH, json={}).json()["mode"] == "sharing"


def test_export_service_registered_discoverable_and_provider_override_respected(client: TestClient[Litestar]) -> None:
    use_app_token(client)
    approve("sharing")
    with closing(get_db()) as db:
        providers = list_all_service_providers(db, APP_DEFINITIONS_SERVICE_URL)
        assert len(providers) == 1
        assert providers[0].app_id == ROUTER_APP_ID
        assert providers[0].service_version == "0.1.0"
        assert providers[0].is_default
        seed_app(db, "other")
        db.execute("INSERT INTO service_defaults VALUES (?, 'other')", (APP_DEFINITIONS_SERVICE_URL,))
        db.commit()
    assert client.post(SERVICE_PATH, json={}).status_code == 503
    response = client.post(SERVICE_PATH, json={}, headers={"X-OpenHost-Provider": ROUTER_APP_ID})
    assert response.status_code == 200


def test_builtin_header_trusting_handler_is_not_public(client: TestClient[Litestar]) -> None:
    client.cookies.clear()
    headers = {"X-OpenHost-Permissions": '[{"grant":{"mode":"private"},"scope":"global"}]'}
    assert client.post("/export", json={"mode": "private"}, headers=headers).status_code == 404
    assert (
        client.post(
            SERVICE_PATH, json={"mode": "private"}, headers={**headers, "Accept": "application/json"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("missing", [False, True])
def test_private_retrieves_values_only_after_auth_and_sharing_never_does(
    client: TestClient[Litestar], path: str, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    missing_keys = ["GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_ID"] if missing else []
    with closing(get_db()) as db:
        seed_provider(db)
        seed_grant(db, "consumer", {"key": "VALUE"})
        for key in missing_keys:
            seed_grant(db, "consumer", {"key": key})
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert json.loads(request.content) == {"keys": sorted(["VALUE", *missing_keys])}
        return httpx.Response(200, json={"secrets": {"VALUE": SENTINEL}, "missing": missing_keys})

    fake_secrets(monkeypatch, handle)
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("sharing")
        assert client.post(path, json={"mode": "private"}).status_code == 403
        assert not calls
        approve("private")
    response = client.post(path, json={"mode": "sharing"})
    assert response.status_code == 200
    assert SENTINEL not in response.text
    sharing = response.json()
    assert "secret_values" not in sharing
    assert "missing_secret_keys" not in sharing
    assert not calls
    response = client.post(path, json={"mode": "private"})
    assert response.status_code == 200
    assert response.json()["mode"] == "private"
    assert response.json()["apps"] == sharing["apps"]
    assert response.json()["secret_values"] == {"VALUE": SENTINEL}
    assert response.json()["missing_secret_keys"] == sorted(missing_keys)
    assert_json_no_store(response)
    assert len(calls) == 1


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize(
    "upstream",
    [
        httpx.Response(500, json={"error": SENTINEL, "secrets": {"KEY": SENTINEL}}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": ["KEY"]}),
        httpx.Response(200, json={"secrets": {}, "missing": []}),
        httpx.Response(200, json={"secrets": {}, "missing": ["KEY", SENTINEL]}),
    ],
)
def test_secret_provider_error_body_and_trace_never_escape(
    client: TestClient[Litestar],
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    upstream: httpx.Response,
) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("private")
    with closing(get_db()) as db:
        seed_provider(db)
        seed_grant(db, "consumer", {"key": "KEY"})
    fake_secrets(monkeypatch, lambda request: upstream)
    response = client.post(path, json={"mode": "private"})
    assert response.status_code == 502
    assert_json_no_store(response)
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text
