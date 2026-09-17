import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import yaml
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.app_definitions import ExportMode
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.core.service_interface.builtin_services import APP_DEFINITIONS_SERVICE_URL
from compute_space.core.service_interface.services import list_all_service_providers
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import seed_api_token
from compute_space.tests.test_app_definitions import seed_app
from compute_space.web.app import _login_required_redirect
from compute_space.web.routes.api import app_definitions
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
        seed_api_token(db, "test", API_TOKEN)
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
        db.execute(
            """INSERT OR IGNORE INTO permissions_v2
               (consumer_app_id, service_url, grant_payload, scope, provider_app_id) VALUES (?, ?, ?, ?, ?)""",
            ("consumer", APP_DEFINITIONS_SERVICE_URL, json.dumps({"mode": mode}), scope, provider),
        )
        db.commit()


def assert_json_no_store(response: httpx.Response) -> None:
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "no-store"
    if response.status_code >= 400:
        assert not any(header.startswith("x-app-definitions-") for header in response.headers)


@pytest.mark.parametrize("body", [{}, {"mode": "sharing"}, {"mode": "private"}])
def test_owner_session_default_mode_and_private_envelope(client: TestClient[Litestar], body: dict[str, str]) -> None:
    response = client.post(OWNER_PATH, json=body)
    assert response.status_code == 200
    assert_json_no_store(response)
    assert response.json()["mode"] == body.get("mode", "sharing")
    assert response.json()["schema_version"] == 2
    assert ("platform_api_tokens" in response.json()) == (body.get("mode") == "private")
    if body.get("mode") == "private":
        assert response.json()["platform_api_tokens"] == [
            {"name": "test", "token_hash": hashlib.sha256(API_TOKEN.encode()).hexdigest(), "expires_at": None}
        ]
    assert "\n  " in response.text
    assert response.text.endswith("\n")
    assert SENTINEL not in response.text
    assert API_TOKEN not in response.text
    assert APP_TOKEN not in response.text


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_owner_api_key_auth(client: TestClient[Litestar], mode: str) -> None:
    client.cookies.clear()
    response = client.post(OWNER_PATH, json={"mode": mode}, headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 200
    assert_json_no_store(response)
    assert API_TOKEN not in response.text
    assert ("platform_api_tokens" in response.json()) == (mode == "private")


@pytest.mark.parametrize("auth", ["anonymous", "app", "invalid", "expired-api", "expired-session", "spoofed"])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
def test_owner_export_denies_nonowners_with_json_no_store(
    client: TestClient[Litestar], auth: str, accept: str
) -> None:
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
    headers["Accept"] = accept
    response = client.post(OWNER_PATH, json={"mode": "private"}, headers=headers, follow_redirects=False)
    assert response.status_code == 401
    assert_json_no_store(response)
    assert "platform_api_tokens" not in response.json()


@pytest.mark.parametrize("origin", ["https://evil.example", "http://consumer.testzone.local", "null"])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
def test_owner_session_denies_cross_origin(client: TestClient[Litestar], origin: str, accept: str) -> None:
    response = client.post(OWNER_PATH, json={}, headers={"Origin": origin, "Accept": accept})
    assert response.status_code == 401
    assert_json_no_store(response)


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
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
def test_invalid_modes_fail_400(client: TestClient[Litestar], path: str, body: object, accept: str) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("private")
    # httpx's json=None means no body, so encode explicitly to test the JSON null case.
    response = client.post(
        path, content=json.dumps(body), headers={"Content-Type": "application/json", "Accept": accept}
    )
    assert response.status_code == 400
    assert_json_no_store(response)


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
@pytest.mark.parametrize("content", ["", f'{{"mode": {SENTINEL}', "{broken"])
def test_malformed_json_errors_are_sanitized(
    client: TestClient[Litestar], path: str, content: str, accept: str
) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
    response = client.post(path, content=content, headers={"Content-Type": "application/json", "Accept": accept})
    assert response.status_code == 400
    assert_json_no_store(response)
    assert SENTINEL not in response.text


@pytest.mark.parametrize("requested", ["sharing", "private"])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
def test_requested_manifest_grant_is_not_approval_and_spoofed_headers_do_not_authorize(
    client: TestClient[Litestar], requested: str, monkeypatch: pytest.MonkeyPatch, accept: str
) -> None:
    use_app_token(client)
    monkeypatch.setattr(app_definitions, "export_app_definitions", lambda *a, **kw: pytest.fail("unapproved export"))
    response = client.post(
        SERVICE_PATH,
        json={"mode": requested},
        headers={
            "Accept": accept,
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
        assert ("platform_api_tokens" in response.json()) == (mode == "private")
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
@pytest.mark.parametrize("empty", [False, True])
def test_private_includes_api_records_after_auth_and_sharing_never_queries_them(
    client: TestClient[Litestar], path: str, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    with closing(get_db()) as db:
        db.execute("DELETE FROM api_tokens")
        db.commit()
        expected = (
            []
            if empty
            else [
                seed_api_token(db, "duplicate", SENTINEL, "2000-01-01T00:00:00+00:00"),
                seed_api_token(db, "duplicate", API_TOKEN),
            ]
        )
    queries: list[str] = []
    original_export = app_definitions.export_app_definitions

    async def tracked_export(db: sqlite3.Connection, apps_dir: str, mode: ExportMode) -> str:
        db.set_trace_callback(queries.append)
        try:
            return await original_export(db, apps_dir, mode)
        finally:
            db.set_trace_callback(None)

    monkeypatch.setattr(app_definitions, "export_app_definitions", tracked_export)
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("sharing")
        assert client.post(path, json={"mode": "private"}).status_code == 403
        assert not queries
        approve("private")
    response = client.post(path, json={"mode": "sharing"})
    assert response.status_code == 200
    assert SENTINEL not in response.text
    sharing = response.json()
    assert "platform_api_tokens" not in sharing
    assert not any("api_tokens" in query for query in queries)
    response = client.post(path, json={"mode": "private"})
    assert response.status_code == 200
    assert response.json()["mode"] == "private"
    assert response.json()["apps"] == sharing["apps"]
    assert response.json()["platform_api_tokens"] == sorted(expected, key=lambda token: token["token_hash"])
    assert_json_no_store(response)
    assert sum("FROM api_tokens" in query for query in queries) == 1
    assert SENTINEL not in response.text
    assert API_TOKEN not in response.text
    assert APP_TOKEN not in response.text


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (sqlite3.OperationalError(SENTINEL), 500),
        (RuntimeError(SENTINEL), 500),
        (HTTPException(status_code=503, detail=SENTINEL), 503),
    ],
)
def test_export_error_details_and_trace_never_escape(
    client: TestClient[Litestar],
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    status: int,
    accept: str,
) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("private")

    async def failed_export(db: sqlite3.Connection, apps_dir: str, mode: ExportMode) -> str:
        raise failure

    monkeypatch.setattr(app_definitions, "export_app_definitions", failed_export)
    response = client.post(path, json={"mode": "private"}, headers={"Accept": accept})
    assert response.status_code == status
    assert_json_no_store(response)
    assert response.json() == {"error": "App definition export failed."}
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text


def assert_export_headers(response: httpx.Response, document: dict[str, object]) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert "accept" in {part.strip().lower() for part in response.headers["Vary"].split(",")}
    assert response.headers["X-App-Definitions-Mode"] == document["mode"]
    assert response.headers["X-App-Definitions-Schema-Version"] == str(document["schema_version"]) == "2"
    assert "X-App-Definitions-Missing-Count" not in response.headers


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize(
    ("accept", "media_type"),
    [
        (None, "application/json"),
        ("*/*", "application/json"),
        ("application/*", "application/json"),
        ("application/json", "application/json"),
        ("application/yaml", "application/yaml"),
        ("text/html", "application/json"),
        ("application/yaml;q=0.9,application/json;q=1", "application/json"),
        ("application/json;q=0.2,application/yaml;q=0.8", "application/yaml"),
        ("application/yaml;q=0.001", "application/yaml"),
        ("application/json;q=0.101,application/yaml;q=0.109", "application/yaml"),
        ("application/yaml;profile=special;q=0,application/yaml;q=1,application/json;q=0.5", "application/yaml"),
        ("application/yaml;profile=special", "application/json"),
        ("application/yaml;profile=special;q=1,application/yaml;q=0", "application/json"),
        ("application/yaml;q=0", "application/json"),
        ("application/yaml;q=0,application/json;q=0.5", "application/json"),
        ("application/json;q=0,application/yaml;q=0.5", "application/yaml"),
        ("application/yaml;q=0,*/*;q=1", "application/json"),
        ("application/json;q=0,*/*;q=1", "application/yaml"),
        ("application/json;q=0.1,application/yaml;q=0.2,*/*;q=1", "application/yaml"),
        ("application/yaml;q=0,application/json;q=0", "application/json"),
        ("*/*;q=0", "application/json"),
    ],
)
def test_export_content_negotiation(
    client: TestClient[Litestar], path: str, accept: str | None, media_type: str
) -> None:
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("sharing")
    client.headers.pop("Accept", None)
    response = client.post(path, json={}, headers={"Accept": accept} if accept else {})
    assert response.status_code == 200
    assert response.headers["Content-Type"].split(";")[0] == media_type
    document = yaml.safe_load(response.text) if media_type == "application/yaml" else response.json()
    assert document["mode"] == "sharing"
    assert_export_headers(response, document)
    if media_type == "application/yaml":
        with pytest.raises(json.JSONDecodeError):
            response.json()


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_yaml_and_json_export_the_same_document(client: TestClient[Litestar], path: str, mode: str) -> None:
    names = ["", "1e3", "first\nsecond\n\n", "true", "a\r\nb\u2028c"]
    with closing(get_db()) as db:
        db.execute("DELETE FROM api_tokens")
        records = [seed_api_token(db, name, f"{SENTINEL}-{index}") for index, name in enumerate(names)]
        seed_app(db, "remote", repo_url=f"https://user:{SENTINEL}@example.com/app?token={SENTINEL}")
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('remote', '1e3', 8080, 30000)"
        )
        db.commit()
    if path == SERVICE_PATH:
        use_app_token(client)
        approve(mode)
    json_response = client.post(path, json={"mode": mode}, headers={"Accept": "application/json"})
    yaml_response = client.post(path, json={"mode": mode}, headers={"Accept": "application/yaml"})
    assert json_response.status_code == yaml_response.status_code == 200
    assert yaml_response.headers["Content-Type"].split(";")[0] == "application/yaml"
    document = yaml.safe_load(yaml_response.text)
    assert document == json_response.json()
    assert_export_headers(json_response, document)
    assert_export_headers(yaml_response, document)
    if mode == "private":
        assert document["platform_api_tokens"] == sorted(records, key=lambda token: token["name"])
    else:
        assert "platform_api_tokens" not in document
    assert SENTINEL not in yaml_response.text


@pytest.mark.parametrize("path", [OWNER_PATH, SERVICE_PATH])
@pytest.mark.parametrize("accept", ["application/json", "application/yaml"])
@pytest.mark.parametrize("requested", ["sharing", "private"])
def test_success_headers_describe_exported_document_instead_of_request(
    client: TestClient[Litestar], path: str, accept: str, requested: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {"mode": "private" if requested == "sharing" else "sharing", "schema_version": 2, "apps": []}
    if document["mode"] == "private":
        document.update(platform_api_tokens=[])

    async def export(db: sqlite3.Connection, apps_dir: str, mode: ExportMode) -> str:
        assert mode == requested
        return json.dumps(document)

    monkeypatch.setattr(app_definitions, "export_app_definitions", export)
    if path == SERVICE_PATH:
        use_app_token(client)
        approve("private")
    response = client.post(path, json={"mode": requested}, headers={"Accept": accept})
    assert response.status_code == 200
    assert yaml.safe_load(response.text) == document
    assert_export_headers(response, document)
