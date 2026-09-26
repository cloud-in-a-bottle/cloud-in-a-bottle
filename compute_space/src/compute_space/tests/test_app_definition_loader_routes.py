import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.app_definition_loader import MAX_DEFINITION_BYTES
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.auth.auth import validate_api_token
from compute_space.db import get_db
from compute_space.tests.test_app_definition_loader import INVALID_TOKENS
from compute_space.tests.test_app_definition_loader import app_document
from compute_space.tests.test_app_definition_loader import document
from compute_space.tests.test_app_definition_loader import token_record
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import seed_api_token
from compute_space.tests.test_app_definitions import seed_app
from compute_space.tests.test_app_definitions_routes import API_TOKEN
from compute_space.tests.test_app_definitions_routes import APP_TOKEN
from compute_space.tests.test_app_definitions_routes import OWNER_PATH
from compute_space.tests.test_app_definitions_routes import SERVICE_PATH
from compute_space.tests.test_app_definitions_routes import approve
from compute_space.tests.test_app_definitions_routes import assert_json_no_store
from compute_space.tests.test_app_definitions_routes import client as client
from compute_space.web.helpers.app_definition_export import dump_export_yaml
from compute_space.web.routes.api import app_definition_loader

PARSE = "/api/app-definitions/parse"
IMPORT = "/api/app-definitions/import-private"


@pytest.mark.parametrize("path", [PARSE, IMPORT])
def test_malformed_null_expiry_rejected_before_any_token_write(client: TestClient[Litestar], path: str) -> None:
    body = document(private=True, apps=[]) | {
        "platform_api_tokens": [
            token_record(raw="valid-first"),
            token_record(raw="expired-second", expiry="malformed-null-marker"),
        ]
    }
    content = dump_export_yaml(body).replace(
        "expires_at: malformed-null-marker", "expires_at: !!null 2000-01-01T00:00:00Z"
    )
    with closing(get_db()) as db:
        before = list(db.iterdump())
    response = client.post(path, json={"content": content})
    assert response.status_code == 400
    assert_json_no_store(response)
    assert response.json() == {"error": "Invalid YAML null value."}
    with closing(get_db()) as db:
        assert list(db.iterdump()) == before
        assert validate_api_token("valid-first", db) is None
        assert validate_api_token("expired-second", db) is None


@pytest.mark.parametrize("path", [PARSE, IMPORT])
@pytest.mark.parametrize(
    "auth",
    ["anonymous", "app", "private-grant", "spoofed", "app-spoofed", "bad-token", "expired-api", "expired-session"],
)
def test_nonowners_cannot_even_parse(
    client: TestClient[Litestar], path: str, auth: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if auth != "expired-session":
        client.cookies.clear()
    headers = {"Accept": "application/yaml"}
    if auth in {"app", "private-grant", "app-spoofed"}:
        headers["Authorization"] = f"Bearer {APP_TOKEN}"
        if auth != "app":
            approve("private")
    if auth in {"spoofed", "app-spoofed"}:
        headers.update(
            {
                "X-OpenHost-Is-Owner": "true",
                "X-OpenHost-Consumer-Id": ROUTER_APP_ID,
                "X-OpenHost-Permissions": '[{"grant":{"mode":"private"},"scope":"global"}]',
            }
        )
    if auth == "bad-token":
        headers["Authorization"] = "Bearer invalid"
    if auth in {"expired-api", "expired-session"}:
        with closing(get_db()) as db:
            table = "api_tokens" if auth == "expired-api" else "sessions"
            db.execute(f"UPDATE {table} SET expires_at='2000-01-01T00:00:00Z'")
            db.commit()
        if auth == "expired-api":
            headers["Authorization"] = f"Bearer {API_TOKEN}"
    monkeypatch.setattr(app_definition_loader, "parse_definition", lambda *a: pytest.fail("unauthorized parse"))
    with closing(get_db()) as db:
        before = list(db.iterdump())
    response = client.post(
        path, json={"content": dump_export_yaml(document(private=True))}, headers=headers, follow_redirects=False
    )
    assert response.status_code == 401
    assert_json_no_store(response)
    with closing(get_db()) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("path", [PARSE, IMPORT])
@pytest.mark.parametrize("origin", ["https://evil.example", "http://consumer.testzone.local", "null"])
def test_origin_auth_precedes_parse(
    client: TestClient[Litestar], path: str, origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_definition_loader, "parse_definition", lambda *a: pytest.fail("cross-origin parse"))
    response = client.post(path, json={"content": SENTINEL}, headers={"Origin": origin})
    assert response.status_code == 401
    assert_json_no_store(response)


@pytest.mark.parametrize("path", [PARSE, IMPORT])
def test_owner_api_token_accepted_without_http(
    client: TestClient[Litestar], path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "request", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    client.cookies.clear()
    response = client.post(
        path,
        json={"content": dump_export_yaml(document(private=True))},
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )
    assert response.status_code == 200
    assert_json_no_store(response)
    if path == IMPORT:
        assert response.json() == {"ok": True, "added_api_token_count": 1, "existing_api_token_count": 0}


def test_service_grant_cannot_reach_loader(client: TestClient[Litestar]) -> None:
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {APP_TOKEN}"
    approve("private")
    for action in ("parse", "import-private"):
        response = client.post(
            SERVICE_PATH.replace("export", action), json={"content": dump_export_yaml(document(private=True))}
        )
        assert response.status_code == 404


def test_private_parse_has_no_effects_or_hashes_and_runs_in_thread(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(get_db()) as db:
        before = list(db.iterdump())
    monkeypatch.setattr(httpx.AsyncClient, "request", lambda *a, **kw: pytest.fail("parse made HTTP request"))
    threads = []

    def parse(content: str):
        threads.append(threading.current_thread().name)
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return parse_definition(content)

    monkeypatch.setattr(app_definition_loader, "parse_definition", parse)
    body = document(private=True) | {"platform_api_tokens": [token_record(), token_record(raw="second-key")]}
    response = client.post(PARSE, json={"content": dump_export_yaml(body)})
    assert response.status_code == 200
    assert_json_no_store(response)
    assert SENTINEL not in response.text
    assert all(token["token_hash"] not in response.text for token in body["platform_api_tokens"])
    assert threads
    plan = response.json()
    assert set(plan) == {"schema_version", "mode", "apps", "platform_api_token_names"}
    assert plan["schema_version"] == 2
    assert plan["platform_api_token_names"] == ["owner key", "owner key"]
    assert set(plan["apps"][0]) == {"name", "source_label", "status", "install"}
    assert set(plan["apps"][0]["install"]) == {"repo_url", "app_name", "port_overrides"}
    with closing(get_db()) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("path", [PARSE, IMPORT])
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"content": 123},
        {"content": SENTINEL},
        {"content": "{}", "target_url": SENTINEL},
        {"content": "{}", "replace_existing": True},
        {"content": "{}", "import_api_tokens": True},
    ],
)
def test_invalid_requests_sanitized(
    client: TestClient[Litestar], path: str, body: object, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert_json_no_store(response)
    assert SENTINEL not in response.text + caplog.text


@pytest.mark.parametrize("path", [PARSE, IMPORT])
def test_malformed_json_and_size_caps_sanitized(
    client: TestClient[Litestar], path: str, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post(path, content=f'{{"content": {SENTINEL}', headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert_json_no_store(response)
    response = client.post(path, json={"content": "#" + "é" * (MAX_DEFINITION_BYTES // 2)})
    assert response.status_code == 400
    assert_json_no_store(response)
    response = client.post(
        path, content=" " * (6 * MAX_DEFINITION_BYTES + 2048), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert_json_no_store(response)
    assert SENTINEL not in response.text + caplog.text


@pytest.mark.parametrize("bad", INVALID_TOKENS)
def test_all_tokens_validated_before_writes(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        app_definition_loader, "import_platform_api_tokens", lambda *a: pytest.fail("validation must precede writer")
    )
    body = document(private=True) | {"platform_api_tokens": [token_record(raw="valid-first"), bad]}
    with closing(get_db()) as db:
        before = list(db.iterdump())
    response = client.post(IMPORT, json={"content": dump_export_yaml(body)})
    assert response.status_code == 400
    assert_json_no_store(response)
    assert SENTINEL not in response.text + caplog.text
    with closing(get_db()) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("bad_part", ["app", "duplicate-hash", "old-version", "builtin-escape"])
def test_entire_file_and_builtin_containment_revalidated(
    client: TestClient[Litestar], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_part: str
) -> None:
    monkeypatch.setattr(
        app_definition_loader, "import_platform_api_tokens", lambda *a: pytest.fail("invalid file reached writer")
    )
    body = document(private=True)
    original_config = provide_config()
    if bad_part == "app":
        body["apps"].append({"name": "bad"})
    elif bad_part == "duplicate-hash":
        body["platform_api_tokens"].append(token_record(name="different name"))
    elif bad_part == "old-version":
        body["schema_version"] = 1
    else:
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        (bundled / "escape").symlink_to(tmp_path, target_is_directory=True)
        set_active_config(original_config.evolve(apps_dir_override=str(bundled)))
        body["apps"].append(app_document({"kind": "builtin", "identifier": "escape"}) | {"name": "consumer"})
        # Even an already-existing app must undergo containment validation before token writes.
    try:
        response = client.post(IMPORT, json={"content": dump_export_yaml(body)})
        assert response.status_code == 400
        assert_json_no_store(response)
    finally:
        set_active_config(original_config)


def test_import_atomic_failure_sanitizes_database_error(
    client: TestClient[Litestar], caplog: pytest.LogCaptureFixture
) -> None:
    with closing(get_db()) as db:
        db.execute(
            f"CREATE TRIGGER fail_second BEFORE INSERT ON api_tokens WHEN NEW.name='fail' BEGIN SELECT RAISE(ABORT, '{SENTINEL}'); END"
        )
        db.commit()
        before = list(db.iterdump())
    body = document(private=True) | {"platform_api_tokens": [token_record(raw="first-key"), token_record(name="fail")]}
    caplog.set_level(logging.DEBUG)
    response = client.post(IMPORT, json={"content": dump_export_yaml(body)}, headers={"Accept": "application/yaml"})
    assert response.status_code == 500
    assert_json_no_store(response)
    assert response.json() == {"error": "App definition loading failed."}
    assert SENTINEL not in response.text + str(response.headers) + caplog.text
    with closing(get_db()) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_empty_import_has_no_platform_record_queries_or_writes(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    real_import = app_definition_loader.import_platform_api_tokens

    def guarded_import(db, tokens):
        def authorize(action, table, *args):
            if table == "api_tokens" or action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        try:
            return real_import(db, tokens)
        finally:
            db.set_authorizer(None)

    monkeypatch.setattr(app_definition_loader, "import_platform_api_tokens", guarded_import)
    body = document(private=mode == "private")
    if mode == "private":
        body["platform_api_tokens"] = []
    response = client.post(IMPORT, json={"content": dump_export_yaml(body)})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "added_api_token_count": 0, "existing_api_token_count": 0}


def test_add_only_counts_and_existing_credentials_survive(client: TestClient[Litestar]) -> None:
    with closing(get_db()) as db:
        seed_api_token(db, "existing expired", SENTINEL, "2000-01-01T00:00:00Z")
        original = [tuple(row) for row in db.execute("SELECT * FROM api_tokens ORDER BY id")]
    body = document(private=True) | {
        "platform_api_tokens": [token_record(name="rename attempt"), token_record(name="test", raw="new-key")]
    }
    for expected in [(1, 1), (0, 2)]:
        response = client.post(IMPORT, json={"content": dump_export_yaml(body)})
        assert response.status_code == 200
        assert_json_no_store(response)
        assert response.json() == {
            "ok": True,
            "added_api_token_count": expected[0],
            "existing_api_token_count": expected[1],
        }
        with closing(get_db()) as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM api_tokens ORDER BY id LIMIT 2")] == original
            assert validate_api_token(API_TOKEN, db) is not None
            assert validate_api_token(SENTINEL, db) is None
            assert validate_api_token("new-key", db) is not None
            assert db.execute("SELECT count(*) FROM api_tokens WHERE name='test'").fetchone()[0] == 2


def test_parent_export_owner_routes_roundtrip_and_ordinary_app_data(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "request", lambda *a, **kw: pytest.fail("unexpected provider HTTP"))
    config = provide_config()
    data_path = Path(config.persistent_data_dir) / "secrets.sqlite"
    with closing(sqlite3.connect(data_path)) as data:
        data.execute("CREATE TABLE secrets (key TEXT, value TEXT)")
        data.execute("INSERT INTO secrets VALUES ('arbitrary', ?)", (SENTINEL,))
        data.commit()
    app_bytes = data_path.read_bytes()
    with closing(get_db()) as db:
        seed_app(db, "secrets", repo_url="https://example.com/secrets")
        before = list(db.iterdump())
    for mode in ("sharing", "private"):
        exported = client.post(OWNER_PATH, json={"mode": mode}, headers={"Accept": "application/yaml"})
        assert exported.status_code == 200
        parsed = client.post(PARSE, json={"content": exported.text})
        assert parsed.status_code == 200
        secrets_app = next(app for app in parsed.json()["apps"] if app["name"] == "secrets")
        assert secrets_app == {
            "name": "secrets",
            "source_label": "https://example.com/secrets",
            "status": "existing",
            "app_id": "secrets",
        }
        response = client.post(IMPORT, json={"content": exported.text})
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "added_api_token_count": 0,
            "existing_api_token_count": int(mode == "private"),
        }
    # An ordinary Secrets app source receives exactly the normal install payload when absent.
    body = document(
        apps=[
            app_document({"kind": "remote", "repo_url": "https://example.com/secrets", "ref": None})
            | {"name": "another-secrets"}
        ]
    )
    plan = client.post(PARSE, json={"content": dump_export_yaml(body)}).json()
    assert set(plan["apps"][0]["install"]) == {"repo_url", "app_name", "port_overrides"}
    with closing(get_db()) as db:
        # AUTOINCREMENT may advance on a conflict; every actual record must stay identical.
        after = list(db.iterdump())
        assert [line for line in after if "sqlite_sequence" not in line] == [
            line for line in before if "sqlite_sequence" not in line
        ]
    assert data_path.read_bytes() == app_bytes
