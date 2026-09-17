import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core import app_definition_secrets
from compute_space.core.app_definition_loader import MAX_DEFINITION_BYTES
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definition_secrets import SecretImportError
from compute_space.core.app_definition_secrets import import_secret_values
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.proxy_target import LocalPort
from compute_space.db import get_db
from compute_space.tests.test_app_definition_loader import document
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import fake_secrets
from compute_space.tests.test_app_definitions import seed_provider
from compute_space.tests.test_app_definitions_routes import API_TOKEN
from compute_space.tests.test_app_definitions_routes import APP_TOKEN
from compute_space.tests.test_app_definitions_routes import SERVICE_PATH
from compute_space.tests.test_app_definitions_routes import approve
from compute_space.tests.test_app_definitions_routes import assert_json_no_store
from compute_space.tests.test_app_definitions_routes import client as client
from compute_space.web.helpers.app_definition_export import dump_export_yaml
from compute_space.web.routes.api import app_definition_loader

PARSE = "/api/app-definitions/parse"
IMPORT = "/api/app-definitions/import-secrets"


@pytest.mark.parametrize("path", [PARSE, IMPORT])
@pytest.mark.parametrize("auth", ["anonymous", "app", "private-grant", "spoofed", "bad-token"])
def test_nonowners_cannot_even_parse(
    client: TestClient[Litestar], path: str, auth: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.cookies.clear()
    headers = {"Accept": "application/yaml"}
    if auth in {"app", "private-grant"}:
        headers["Authorization"] = f"Bearer {APP_TOKEN}"
        if auth == "private-grant":
            approve("private")
    elif auth == "spoofed":
        headers.update({"X-OpenHost-Is-Owner": "true", "X-OpenHost-Consumer-Id": ROUTER_APP_ID})
    elif auth == "bad-token":
        headers["Authorization"] = "Bearer invalid"
    monkeypatch.setattr(app_definition_loader, "parse_definition", lambda *a: pytest.fail("unauthorized parse"))
    response = client.post(
        path, json={"content": SENTINEL, "replace_existing": True}, headers=headers, follow_redirects=False
    )
    assert response.status_code == 401
    assert_json_no_store(response)
    assert SENTINEL not in response.text


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
def test_owner_api_token_accepted_without_provider_io(
    client: TestClient[Litestar], path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("empty import provider IO"))
    client.cookies.clear()
    response = client.post(
        path, json={"content": dump_export_yaml(document())}, headers={"Authorization": f"Bearer {API_TOKEN}"}
    )
    assert response.status_code == 200
    assert_json_no_store(response)


def test_service_grant_cannot_reach_loader(client: TestClient[Litestar]) -> None:
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {APP_TOKEN}"
    approve("private")
    for action in ("parse", "import-secrets"):
        response = client.post(
            SERVICE_PATH.replace("export", action),
            json={"content": dump_export_yaml(document()), "replace_existing": True},
        )
        assert response.status_code == 404


def test_private_parse_has_no_effects_or_values_and_runs_in_thread(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = None
    with closing(get_db()) as db:
        before = list(db.iterdump())
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("parse contacted provider"))
    threads = []

    def parse(content: str):
        threads.append(threading.current_thread().name)
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return parse_definition(content)

    monkeypatch.setattr(app_definition_loader, "parse_definition", parse)
    response = client.post(PARSE, json={"content": dump_export_yaml(document(private=True))})
    assert response.status_code == 200
    assert_json_no_store(response)
    assert SENTINEL not in response.text
    assert threads
    plan = response.json()
    assert set(plan) == {"schema_version", "mode", "apps", "secret_keys", "missing_secret_keys"}
    assert plan["secret_keys"] == ["TOKEN"] and plan["missing_secret_keys"] == ["MISSING"]
    assert set(plan["apps"][0]) == {"name", "source_label", "status", "secret_keys", "install"}
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
        {"content": "{}", "replace_existing": 1},
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


@pytest.mark.parametrize("confirmation", [None, False, "true", 1])
def test_nonempty_values_require_explicit_boolean_confirmation(
    client: TestClient[Litestar], confirmation: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("unconfirmed provider IO"))
    body = {"content": dump_export_yaml(document(private=True)), "replace_existing": confirmation}
    if confirmation is None:
        body.pop("replace_existing")
    response = client.post(IMPORT, json=body)
    assert response.status_code == 400
    assert_json_no_store(response)


def test_entire_file_revalidated_before_any_secret_write(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("invalid document provider IO")
    )
    body = document(private=True)
    body["apps"].append({"name": "bad"})
    response = client.post(IMPORT, json={"content": dump_export_yaml(body), "replace_existing": True})
    assert response.status_code == 400
    assert_json_no_store(response)


def test_invalid_unicode_in_later_value_fails_before_any_write(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("invalid Unicode provider IO")
    )
    body = document(private=True) | {"secret_values": {"A": "valid", "B": "\ud800"}}
    response = client.post(IMPORT, json={"content": dump_export_yaml(body), "replace_existing": True})
    assert response.status_code == 400
    assert_json_no_store(response)


@pytest.mark.parametrize("existing_description", ["keep existing", None])
def test_exact_value_upserts_preserve_descriptions_and_never_delete_missing(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, existing_description: str | None
) -> None:
    with closing(get_db()) as db:
        seed_provider(db)
    values = {"EMPTY": "", "MULTILINE": "a\r\nb\n\n", "UNICODE": "日本語 🔑\u2028", "<<": "ordinary key"}
    saved = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/secrets"
        assert request.headers["X-OpenHost-Is-Owner"] == "true"
        assert request.headers["X-OpenHost-Consumer-Id"] == ROUTER_APP_ID
        assert json.loads(request.headers["X-OpenHost-Permissions"]) == []
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"key": "EMPTY", "description": existing_description},
                    {"name": "MULTILINE", "description": "keep\nUnicode 🔑"},
                    {"key": "MISSING", "description": "untouched"},
                    {"key": "UNRELATED", "description": None},
                ],
            )
        assert request.method == "POST"
        saved.append(json.loads(request.content))
        return httpx.Response(201 if len(saved) % 2 else 200, json={"ok": True})

    fake_secrets(monkeypatch, handle)
    body = document(private=True) | {"secret_values": values}
    response = client.post(IMPORT, json={"content": dump_export_yaml(body), "replace_existing": True})
    assert response.status_code == 200
    assert_json_no_store(response)
    assert response.json() == {"ok": True, "saved_secret_count": len(values)}
    assert {entry["key"]: entry["value"] for entry in saved} == values
    assert {entry["key"]: entry["description"] for entry in saved} == {
        "EMPTY": existing_description,
        "MULTILINE": "keep\nUnicode 🔑",
        "UNICODE": "",
        "<<": "",
    }
    with closing(get_db()) as db:
        assert db.execute("SELECT 1 FROM apps WHERE name='demo'").fetchone() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [123, False, [], {}])
async def test_malformed_description_fails_before_writes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, description: object
) -> None:
    seed_provider(db)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=[{"key": "TOKEN", "description": description}])

    fake_secrets(monkeypatch, handle)
    with pytest.raises(SecretImportError) as error:
        await import_secret_values(db, {"TOKEN": SENTINEL})
    assert error.value.saved_secret_count == 0
    assert calls == ["GET"]


@pytest.mark.parametrize("bad_key", ["B", "UNRELATED"])
def test_description_encoding_checked_before_any_write_only_for_requested_keys(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad_key: str
) -> None:
    with closing(get_db()) as db:
        seed_provider(db)
    calls = []
    saved = []
    caplog.set_level(logging.DEBUG)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            # A real JSON response can carry escaped surrogates that HTTPX cannot encode in a POST.
            return httpx.Response(
                200,
                content=json.dumps(
                    [
                        {"key": "A", "description": "keep existing"},
                        {"key": "C", "description": None},
                        {"key": bad_key, "description": f"{SENTINEL}\ud800"},
                    ]
                ),
                headers={"Content-Type": "application/json"},
            )
        saved.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    fake_secrets(monkeypatch, handle)
    values = {"A": "replacement A", "B": "replacement B", "C": ""}
    body = document(private=True) | {"secret_values": values}
    response = client.post(IMPORT, json={"content": dump_export_yaml(body), "replace_existing": True})
    assert_json_no_store(response)
    assert SENTINEL not in response.text + caplog.text
    if bad_key == "B":
        assert response.status_code == 502
        assert response.json() == {
            "error": "Selected Secrets provider could not complete its owner API request. "
            "Some values may already have been saved. Check Secrets before retrying.",
            "saved_secret_count": 0,
        }
        assert calls == ["GET"]
        assert not saved
    else:
        assert response.status_code == 200
        assert response.json() == {"ok": True, "saved_secret_count": 3}
        assert {entry["key"]: entry["value"] for entry in saved} == values
        assert {entry["key"]: entry["description"] for entry in saved} == {
            "A": "keep existing",
            "B": "",
            "C": None,
        }


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_no_values_needs_no_provider_or_confirmation(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(
        app_definition_secrets, "resolve_provider", lambda *a, **kw: pytest.fail("unneeded resolution")
    )
    body = document(private=mode == "private")
    if mode == "private":
        body["secret_values"] = {}
    response = client.post(IMPORT, json={"content": dump_export_yaml(body)})
    assert response.json() == {"ok": True, "saved_secret_count": 0}


@pytest.mark.parametrize(("status", "version"), [(None, "0.1.0"), ("stopped", "0.1.0"), ("running", "2.0.0")])
def test_selected_provider_must_be_available(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, status: str | None, version: str
) -> None:
    if status:
        with closing(get_db()) as db:
            seed_provider(db, status=status, version=version)
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("unavailable provider IO"))
    response = client.post(
        IMPORT, json={"content": dump_export_yaml(document(private=True)), "replace_existing": True}
    )
    assert response.status_code == 502
    assert response.json()["saved_secret_count"] == 0
    assert "compatible selected Secrets provider" in response.json()["error"]


@pytest.mark.parametrize("failure", ["readonly", "invalid-list", "redirect", "bad-ok", "malformed-json", "exception"])
def test_provider_failures_are_sanitized_and_stop_writes(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    with closing(get_db()) as db:
        seed_provider(db)
    calls = []
    caplog.set_level(logging.DEBUG)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        logging.getLogger("httpcore.http11").debug("provider response %s", SENTINEL)
        if failure == "exception":
            raise httpx.ReadError(SENTINEL)
        if failure == "readonly":
            return httpx.Response(404, text=SENTINEL)
        if failure == "invalid-list":
            return httpx.Response(200, json={"secrets": SENTINEL})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if len(calls) == 2:
            return httpx.Response(200, json={"ok": True})
        if failure == "redirect":
            return httpx.Response(
                307,
                headers={"location": f"https://example.com/{SENTINEL}"},
                extensions={"reason_phrase": SENTINEL.encode()},
            )
        if failure == "malformed-json":
            return httpx.Response(200, text=SENTINEL, extensions={"reason_phrase": SENTINEL.encode()})
        return httpx.Response(200, json={"ok": SENTINEL}, extensions={"reason_phrase": SENTINEL.encode()})

    fake_secrets(monkeypatch, handle)
    body = document(private=True) | {"secret_values": {"A": SENTINEL, "B": "", "C": "later"}}
    response = client.post(IMPORT, json={"content": dump_export_yaml(body), "replace_existing": True})
    assert response.status_code == 502
    assert_json_no_store(response)
    assert response.json()["saved_secret_count"] == (0 if failure in {"readonly", "invalid-list", "exception"} else 1)
    assert "owner API" in response.json()["error"] and "may already" in response.json()["error"]
    assert len(calls) == (1 if failure in {"readonly", "invalid-list", "exception"} else 3)
    assert SENTINEL not in response.text + str(response.headers) + caplog.text


@pytest.mark.asyncio
async def test_selected_provider_target_pinned_once(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_provider(db, "other")
    seed_provider(db, "selected")
    port = db.execute("SELECT local_port FROM apps WHERE name='selected'").fetchone()[0]
    resolutions = []
    resolve = app_definition_secrets.resolve_provider

    def capture(*args, **kwargs):
        resolutions.append(True)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(app_definition_secrets, "resolve_provider", capture)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.port == port
        assert request.url.path == "/api/secrets"
        assert not db.in_transaction
        if request.method == "GET":
            db.execute("UPDATE service_defaults SET app_id='other'")
            db.execute("UPDATE apps SET local_port=23456 WHERE name='selected'")
            db.commit()
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    fake_secrets(monkeypatch, handle)
    assert await import_secret_values(db, {"A": "", "B": SENTINEL}) == 2
    assert len(resolutions) == 1 and len(calls) == 3


@pytest.mark.asyncio
async def test_import_logging_filter_is_context_local_and_restored(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed_provider(db)
    started = asyncio.Event()
    proceed = asyncio.Event()
    caplog.set_level(logging.DEBUG)

    async def handle(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.connection").debug("connection: %s", SENTINEL)
        logging.getLogger("httpcore.http11").debug("headers: %s", SENTINEL)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        started.set()
        await proceed.wait()
        return httpx.Response(500, text=SENTINEL, extensions={"reason_phrase": SENTINEL.encode()})

    def fake_client(target: LocalPort, timeout: float, *, trust_env: bool):
        assert trust_env is False
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False), "http://127.0.0.1:12345"

    monkeypatch.setattr(app_definition_secrets, "client_for", fake_client)
    task = asyncio.create_task(import_secret_values(db, {"KEY": SENTINEL}))
    await asyncio.wait_for(started.wait(), 5)
    logging.getLogger("httpx").info("unrelated concurrent request")
    proceed.set()
    with pytest.raises(SecretImportError) as error:
        await task
    assert error.value.__suppress_context__
    logging.getLogger("httpx").info("after import failure")
    assert SENTINEL not in caplog.text
    assert "unrelated concurrent request" in caplog.text and "after import failure" in caplog.text
