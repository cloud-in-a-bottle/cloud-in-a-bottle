import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import attr
import httpx
import pytest

from compute_space.core import app_definition_secrets
from compute_space.core.app_definition_secrets import SECRETS_SERVICE_URL
from compute_space.core.app_definition_secrets import ExportError
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.app_definitions import portable_source
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.proxy_target import LocalPort
from compute_space.core.proxy_target import client_for
from compute_space.db import get_db
from compute_space.tests.conftest import _make_test_config

SENTINEL = "synthetic-credential-DO-NOT-EXPORT"
APPS_DIR = "/platform/bundled/apps"


def seed_app(
    db: sqlite3.Connection,
    name: str,
    *,
    repo_url: str | None = None,
    status: str = "running",
    manifest: str = f"malformed manifest containing {SENTINEL}",
) -> str:
    port = 20000 + db.execute("SELECT count(*) FROM apps").fetchone()[0]
    db.execute(
        """INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status, manifest_raw,
                            description, container_id, error_message)
           VALUES (?, ?, '0.1.0', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, name, f"/private/{SENTINEL}", repo_url, port, status, manifest, SENTINEL, SENTINEL, SENTINEL),
    )
    db.commit()
    return name


def seed_grant(
    db: sqlite3.Connection,
    consumer: str,
    payload: object,
    *,
    scope: str = "global",
    provider: str = "",
    service: str = SECRETS_SERVICE_URL,
) -> None:
    db.execute(
        """INSERT OR IGNORE INTO permissions_v2
           (consumer_app_id, service_url, grant_payload, scope, provider_app_id) VALUES (?, ?, ?, ?, ?)""",
        (consumer, service, json.dumps(payload), scope, provider),
    )
    db.commit()


def seed_provider(
    db: sqlite3.Connection, name: str = "store", version: str = "0.1.0", status: str = "running"
) -> None:
    seed_app(db, name, status=status)
    db.execute(
        "INSERT INTO service_providers_v2 VALUES (?, ?, ?, '/_service_v2/')", (SECRETS_SERVICE_URL, name, version)
    )
    db.execute("INSERT OR REPLACE INTO service_defaults VALUES (?, ?)", (SECRETS_SERVICE_URL, name))
    db.commit()


def fake_secrets(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    def fake_client(target: LocalPort, timeout: float, *, trust_env: bool = True) -> tuple[httpx.AsyncClient, str]:
        assert isinstance(target, LocalPort)
        assert trust_env is False
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ), f"http://127.0.0.1:{target.port}"

    monkeypatch.setattr(app_definition_secrets, "client_for", fake_client)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, {"kind": "unknown"}),
        ("", {"kind": "unknown"}),
        (f"/private/{SENTINEL}", {"kind": "local"}),
        (f"./{SENTINEL}", {"kind": "local"}),
        (f"~/{SENTINEL}", {"kind": "local"}),
        (f"file:///private/{SENTINEL}", {"kind": "local"}),
        (f"file://elsewhere{APPS_DIR}/file_browser", {"kind": "local"}),
        (f"file://{APPS_DIR}/file_browser", {"kind": "builtin", "identifier": "file_browser"}),
        (f"file://{APPS_DIR}/file_browser/../{SENTINEL}", {"kind": "local"}),
        (
            "https://github.com/acme/demo.git",
            {"kind": "remote", "repo_url": "https://github.com/acme/demo.git", "ref": None},
        ),
        (
            "github.com/acme/demo@feature/export",
            {"kind": "remote", "repo_url": "https://github.com/acme/demo", "ref": "feature/export"},
        ),
        (
            f"https://user:{SENTINEL}@github.com/acme/demo.git@main?token={SENTINEL}#{SENTINEL}",
            {"kind": "remote", "repo_url": "https://github.com/acme/demo.git", "ref": "main"},
        ),
        (
            f"oauth2:{SENTINEL}@gitlab.com/acme/demo.git@v1",
            {"kind": "remote", "repo_url": "https://gitlab.com/acme/demo.git", "ref": "v1"},
        ),
        (
            f"http://user:{SENTINEL}@example.com:8080/demo;token={SENTINEL}?key={SENTINEL}",
            {"kind": "remote", "repo_url": "http://example.com:8080/demo", "ref": None},
        ),
        (
            f"https://git.example/team;token={SENTINEL}/repo.git@main",
            {"kind": "remote", "repo_url": "https://git.example/team/repo.git", "ref": "main"},
        ),
        (
            f"https://git.example/repo.git@feature;token={SENTINEL}/export",
            {"kind": "remote", "repo_url": "https://git.example/repo.git", "ref": "feature/export"},
        ),
        (
            f"https://git.example/team;token=user@{SENTINEL}/repo.git",
            {"kind": "remote", "repo_url": "https://git.example/team/repo.git", "ref": None},
        ),
        (
            f"https://git.example/repo.git@feature/branch;token=user@{SENTINEL}/export",
            {"kind": "remote", "repo_url": "https://git.example/repo.git", "ref": "feature/branch/export"},
        ),
        (
            f"https://{SENTINEL}@[::1]:9999/demo@v1",
            {"kind": "remote", "repo_url": "https://[::1]:9999/demo", "ref": "v1"},
        ),
        (f"ssh://git:{SENTINEL}@example.com/demo", {"kind": "unknown"}),
        ("git@github.com:acme/demo", {"kind": "unknown"}),
        (f"https://user:{SENTINEL}@[invalid/demo", {"kind": "unknown"}),
        (f"https://example.com:{SENTINEL}/demo", {"kind": "unknown"}),
        (f"ftp://user:{SENTINEL}@example.com/demo", {"kind": "unknown"}),
    ],
)
def test_sources_are_portable_and_strip_credentials(url: str | None, expected: dict[str, object]) -> None:
    actual = attr.asdict(portable_source(url, APPS_DIR))
    assert actual == expected
    assert SENTINEL not in json.dumps(actual)


@pytest.mark.asyncio
async def test_sharing_is_deterministic_allow_list_and_never_reads_manifest_or_secrets(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_app(db, "zulu", repo_url=f"https://user:{SENTINEL}@github.com/acme/zulu@main?secret={SENTINEL}")
    seed_app(db, "alpha", repo_url=f"file://{APPS_DIR}/file_browser", status="stopped")
    seed_grant(db, "zulu", {"key": "DB_URL", "description": SENTINEL})
    seed_grant(db, "zulu", {"key": "API_KEY"})
    seed_grant(db, "alpha", {"command": SENTINEL})
    seed_grant(db, "alpha", {"key": "NOT_A_SECRET_SERVICE"}, service="example.com/opaque")
    db.executemany(
        "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('zulu', ?, ?, ?)",
        [("z-port", 90, 9090), ("a-port", 80, 8080)],
    )
    db.commit()

    def authorize(action: int, table: str, column: str, *args: object) -> int:
        if action == sqlite3.SQLITE_READ and table == "apps":
            assert column in {"app_id", "name", "repo_url", "created_at", ""}
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    monkeypatch.setattr(
        app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("sharing contacted Secrets")
    )
    first = await export_app_definitions(db, APPS_DIR, "sharing")
    assert first == await export_app_definitions(db, APPS_DIR, "sharing")
    assert first == json.dumps(json.loads(first), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert SENTINEL not in first
    document = json.loads(first)
    assert set(document) == {"schema_version", "mode", "apps"}
    assert document["schema_version"] == 1
    assert document["mode"] == "sharing"
    assert [app["name"] for app in document["apps"]] == ["alpha", "zulu"]
    for app in document["apps"]:
        assert set(app) == {"name", "source", "port_mappings", "secret_keys"}
    assert document["apps"][1]["secret_keys"] == ["API_KEY", "DB_URL"]
    assert document["apps"][1]["port_mappings"] == [
        {"label": "a-port", "container_port": 80, "host_port": 8080},
        {"label": "z-port", "container_port": 90, "host_port": 9090},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [False, True])
async def test_private_without_approved_keys_never_contacts_store(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, provider: bool
) -> None:
    seed_app(
        db,
        "consumer",
        manifest=f'[[services.v2.consumes]]\nservice = "{SECRETS_SERVICE_URL}"\ngrants = [{{key="REQUESTED_ONLY"}}]',
    )
    if provider:
        seed_provider(db, status="stopped")
    seed_grant(db, "consumer", {"key": "WRONG_PROVIDER"}, scope="app", provider="other")
    monkeypatch.setattr(app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("read unreferenced store"))
    document = json.loads(await export_app_definitions(db, APPS_DIR, "private"))
    assert document["secret_values"] == {}
    assert document["missing_secret_keys"] == []
    assert document["mode"] == "private"


@pytest.mark.asyncio
async def test_private_uses_approved_keys_selected_provider_and_preserves_values(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_app(db, "consumer")
    seed_app(db, "second")
    seed_provider(db, "old")
    seed_provider(db, "selected")
    expected = {"EMPTY": "", "MULTILINE": "line1\nline2\n", "UNICODE": "密钥🔑", "ключ": "значение"}
    for key in expected:
        seed_grant(db, "consumer", {"key": key})
    seed_grant(db, "second", {"key": "EMPTY"}, scope="app", provider="selected")
    seed_grant(db, "consumer", {"key": "WRONG_STORE"}, scope="app", provider="old")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/_service_v2/get"
        assert request.url.port == db.execute("SELECT local_port FROM apps WHERE name='selected'").fetchone()[0]
        assert json.loads(request.content) == {"keys": sorted(expected)}
        grants = json.loads(request.headers["X-OpenHost-Permissions"])
        assert grants == [{"grant": {"key": k}, "scope": "global"} for k in sorted(expected)]
        assert "Authorization" not in request.headers
        assert request.headers["X-OpenHost-Consumer-Id"] == ROUTER_APP_ID
        return httpx.Response(200, json={"secrets": {**expected, "EXTRA": SENTINEL}, "missing": []})

    fake_secrets(monkeypatch, handle)
    exported = await export_app_definitions(db, APPS_DIR, "private")
    assert SENTINEL not in exported
    document = json.loads(exported)
    assert document["secret_values"] == expected
    assert document["missing_secret_keys"] == []
    assert len(requests) == 1
    assert document["apps"][0]["secret_keys"] == sorted(expected)
    assert next(a for a in document["apps"] if a["name"] == "second")["secret_keys"] == ["EMPTY"]


@pytest.mark.asyncio
@pytest.mark.parametrize("all_missing", [False, True])
async def test_private_exports_explicit_absence_with_identical_definition_metadata(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, all_missing: bool
) -> None:
    seed_app(db, "consumer", repo_url="https://github.com/acme/original@main")
    seed_provider(db)
    keys = {"EMPTY", "VALUE", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "ключ"}
    for key in keys:
        seed_grant(db, "consumer", {"key": key})
    expected_values = {} if all_missing else {"EMPTY": "", "VALUE": "密钥\nline2\n"}
    expected_missing = sorted(keys - expected_values.keys())
    calls = []
    caplog.set_level(logging.DEBUG)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert json.loads(request.content) == {"keys": sorted(keys)}
        return httpx.Response(
            200,
            json={"secrets": {**expected_values, "EXTRA": SENTINEL}, "missing": list(reversed(expected_missing))},
            extensions={"reason_phrase": SENTINEL.encode()},
        )

    fake_secrets(monkeypatch, handle)
    sharing = json.loads(await export_app_definitions(db, APPS_DIR, "sharing"))
    assert not calls
    assert "secret_values" not in sharing
    assert "missing_secret_keys" not in sharing
    exported = await export_app_definitions(db, APPS_DIR, "private")
    assert json.loads(exported) == {
        **sharing,
        "mode": "private",
        "secret_values": expected_values,
        "missing_secret_keys": expected_missing,
    }
    assert exported == await export_app_definitions(db, APPS_DIR, "private")
    assert len(calls) == 2
    assert SENTINEL not in exported
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_wildcard_is_expanded_once_on_pinned_target_and_metadata_snapshot(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, deleted: bool
) -> None:
    seed_app(db, "consumer", repo_url="https://github.com/acme/original@main")
    seed_provider(db, "selected")
    selected_port = db.execute("SELECT local_port FROM apps WHERE name='selected'").fetchone()[0]
    seed_provider(db, "other")
    db.execute("UPDATE service_defaults SET app_id='selected'")
    db.commit()
    seed_grant(db, "consumer", {"key": "*"}, scope="app", provider="selected")
    seed_grant(db, "consumer", {"key": "EXPLICIT"})
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert not db.in_transaction
        assert request.url.port == selected_port
        calls.append(request.url.path)
        if request.method == "GET":
            db.execute("UPDATE service_defaults SET app_id='other'")
            db.execute("UPDATE apps SET repo_url='https://github.com/acme/changed' WHERE name='consumer'")
            db.execute("DELETE FROM permissions_v2")
            db.commit()
            return httpx.Response(
                200, json={"keys": [{"key": "B", "description": SENTINEL}, {"key": "B"}, {"key": "A"}]}
            )
        assert json.loads(request.content) == {"keys": ["A", "B", "EXPLICIT"]}
        assert {p["grant"]["key"] for p in json.loads(request.headers["X-OpenHost-Permissions"])} == {
            "A",
            "B",
            "EXPLICIT",
        }
        values = {"A": "a", "EXPLICIT": "e"} if deleted else {"A": "a", "B": "b", "EXPLICIT": "e"}
        return httpx.Response(200, json={"secrets": values, "missing": ["B"] if deleted else []})

    fake_secrets(monkeypatch, handle)
    exported = await export_app_definitions(db, APPS_DIR, "private")
    document = json.loads(exported)
    assert document["apps"][0]["source"]["repo_url"] == "https://github.com/acme/original"
    assert document["apps"][0]["secret_keys"] == ["*", "EXPLICIT"]
    assert document["secret_values"] == (
        {"A": "a", "EXPLICIT": "e"} if deleted else {"A": "a", "B": "b", "EXPLICIT": "e"}
    )
    assert document["missing_secret_keys"] == (["B"] if deleted else [])
    assert calls == ["/_service_v2/list", "/_service_v2/get"]
    assert SENTINEL not in exported


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "stopped", "error", "incompatible"])
async def test_private_fails_for_unavailable_provider(db: sqlite3.Connection, state: str) -> None:
    seed_app(db, "consumer")
    seed_grant(db, "consumer", {"key": "KEY"})
    if state != "missing":
        seed_provider(
            db,
            version="0.2.0" if state == "incompatible" else "0.1.0",
            status=state if state in {"stopped", "error"} else "running",
        )
    with pytest.raises(ExportError, match="compatible Secrets provider"):
        await export_app_definitions(db, APPS_DIR, "private")
    assert not db.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"secrets": {}}),
        httpx.Response(200, json={"secrets": {}, "missing": []}),
        httpx.Response(200, json={"secrets": {"KEY": None}}),
        httpx.Response(200, json={"secrets": {"KEY": 123}}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": ["KEY"]}),
        httpx.Response(200, json={"secrets": {"KEY": None}, "missing": ["KEY"]}),
        httpx.Response(200, json={"secrets": {}, "missing": ["KEY", "KEY"]}),
        httpx.Response(200, json={"secrets": {}, "missing": ["KEY", SENTINEL]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [SENTINEL]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": None}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": "KEY"}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [None]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [1]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [False]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [{}]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": [[]]}),
        httpx.Response(200, json={"secrets": {"KEY": SENTINEL}, "missing": {}}),
        httpx.Response(200, json={"secrets": [SENTINEL]}),
        httpx.Response(200, json=[SENTINEL]),
        httpx.Response(200, text=SENTINEL),
        httpx.Response(403, json={"error": SENTINEL}),
        httpx.Response(500, text=SENTINEL),
        httpx.Response(302, headers={"Location": f"https://example.com/{SENTINEL}"}),
    ],
)
async def test_incomplete_or_invalid_secret_responses_fail_without_leaking_values(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, response: httpx.Response
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "KEY"})
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    fake_secrets(monkeypatch, handle)
    with pytest.raises(ExportError) as error:
        await export_app_definitions(db, APPS_DIR, "private")
    assert SENTINEL not in str(error.value)
    assert SENTINEL not in caplog.text
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"secrets": {}, "missing": ["A"]}, {"secrets": {"A": ""}, "missing": []}])
async def test_provider_must_account_for_every_requested_key(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "A"})
    seed_grant(db, "consumer", {"key": "B"})
    fake_secrets(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(ExportError):
        await export_app_definitions(db, APPS_DIR, "private")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, RuntimeError])
async def test_provider_exceptions_are_sanitized(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "KEY"})

    def handle(request: httpx.Request) -> httpx.Response:
        raise failure(SENTINEL)

    fake_secrets(monkeypatch, handle)
    with pytest.raises(ExportError) as error:
        await export_app_definitions(db, APPS_DIR, "private")
    assert SENTINEL not in str(error.value)
    assert error.value.__suppress_context__
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing",
    [
        {},
        {"keys": None},
        {"keys": [SENTINEL]},
        {"keys": [{"key": 5}]},
        {"keys": [{"key": ""}]},
        {"keys": [{"key": "*"}]},
    ],
)
async def test_invalid_wildcard_listing_fails_before_get(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, listing: object
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "*"})

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=listing)

    fake_secrets(monkeypatch, handle)
    with pytest.raises(ExportError):
        await export_app_definitions(db, APPS_DIR, "private")


@pytest.mark.asyncio
async def test_empty_approved_wildcard_store_skips_get(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "*"})

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"keys": []})

    fake_secrets(monkeypatch, handle)
    document = json.loads(await export_app_definitions(db, APPS_DIR, "private"))
    assert document["secret_values"] == {}
    assert document["missing_secret_keys"] == []


@pytest.mark.asyncio
async def test_secret_client_explicitly_disables_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://example.com:32123")
    monkeypatch.setenv("ALL_PROXY", "http://example.com:32123")
    monkeypatch.setenv("NO_PROXY", "")
    http, base = client_for(LocalPort(12345), 1, trust_env=False)
    async with http:
        assert base == "http://127.0.0.1:12345"
        assert not http.trust_env
        assert not http._mounts


@pytest.mark.asyncio
async def test_secret_transport_logs_are_filtered_without_hiding_concurrent_requests(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "*"})
    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    continue_request = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("response headers: %s", SENTINEL)
        logging.getLogger("httpcore.connection").debug("connection exception: %s", SENTINEL)
        if request.method == "GET":
            started.set()
            await continue_request.wait()
            return httpx.Response(
                200, json={"keys": [{"key": "KEY"}]}, extensions={"reason_phrase": SENTINEL.encode()}
            )
        return httpx.Response(500, text=SENTINEL, extensions={"reason_phrase": SENTINEL.encode()})

    def fake_client(target: LocalPort, timeout: float, *, trust_env: bool) -> tuple[httpx.AsyncClient, str]:
        return httpx.AsyncClient(transport=httpx.MockTransport(handle)), "http://127.0.0.1:12345"

    monkeypatch.setattr(app_definition_secrets, "client_for", fake_client)
    task = asyncio.create_task(export_app_definitions(db, APPS_DIR, "private"))
    await asyncio.wait_for(started.wait(), 5)
    logging.getLogger("httpx").info("unrelated concurrent request")
    continue_request.set()
    with pytest.raises(ExportError):
        await task
    logging.getLogger("httpx").info("request after export failure")
    assert SENTINEL not in caplog.text
    assert "unrelated concurrent request" in caplog.text
    assert "request after export failure" in caplog.text


@pytest.mark.asyncio
async def test_metadata_and_grants_share_one_sqlite_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_test_config(tmp_path)
    with closing(get_db()) as db:
        seed_app(db, "consumer", repo_url="https://github.com/acme/original")
        seed_provider(db, "other")
        seed_provider(db, "selected")
        original_port = db.execute("SELECT local_port FROM apps WHERE name='selected'").fetchone()[0]
        seed_grant(db, "consumer", {"key": "ORIGINAL"}, scope="app", provider="selected")
        changed = False

        def concurrent_writer(sql: str) -> None:
            nonlocal changed
            if "SELECT app_id FROM service_defaults" in sql and not changed:
                changed = True
                with closing(get_db()) as writer:
                    writer.execute("UPDATE apps SET repo_url='https://github.com/acme/changed' WHERE name='consumer'")
                    writer.execute("UPDATE service_defaults SET app_id='other'")
                    writer.execute("DELETE FROM permissions_v2")
                    writer.commit()
                    seed_grant(writer, "consumer", {"key": "CHANGED"}, scope="app", provider="other")

        db.set_trace_callback(concurrent_writer)

        def handle(request: httpx.Request) -> httpx.Response:
            assert not db.in_transaction
            assert request.url.port == original_port
            assert json.loads(request.content) == {"keys": ["ORIGINAL"]}
            # A separate connection can write while the exporter is awaiting its provider.
            with closing(get_db()) as writer:
                writer.execute("UPDATE apps SET name='changed' WHERE name='consumer'")
                writer.commit()
            return httpx.Response(200, json={"secrets": {"ORIGINAL": "original value"}})

        fake_secrets(monkeypatch, handle)
        document = json.loads(await export_app_definitions(db, APPS_DIR, "private"))
        assert changed
        assert document["apps"][0]["name"] == "consumer"
        assert document["apps"][0]["source"]["repo_url"] == "https://github.com/acme/original"
        assert document["apps"][0]["secret_keys"] == ["ORIGINAL"]
        assert document["secret_values"] == {"ORIGINAL": "original value"}


@pytest.mark.asyncio
async def test_cancellation_closes_transport_and_releases_snapshot(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_app(db, "consumer")
    seed_provider(db)
    seed_grant(db, "consumer", {"key": "KEY"})
    started = asyncio.Event()
    clients = []

    async def handle(request: httpx.Request) -> httpx.Response:
        assert not db.in_transaction
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def fake_client(target: LocalPort, timeout: float, *, trust_env: bool) -> tuple[httpx.AsyncClient, str]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(http)
        return http, "http://127.0.0.1:12345"

    monkeypatch.setattr(app_definition_secrets, "client_for", fake_client)
    task = asyncio.create_task(export_app_definitions(db, APPS_DIR, "private"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].is_closed
    assert not db.in_transaction
