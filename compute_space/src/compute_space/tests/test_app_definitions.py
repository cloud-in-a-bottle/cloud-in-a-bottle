import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import attr
import httpx
import pytest

from compute_space.core.app_definitions import ExportMode
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.app_definitions import portable_source
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


def seed_api_token(db: sqlite3.Connection, name: str, raw_token: str, expires_at: str = "") -> dict[str, object]:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db.execute(
        "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)", (name, token_hash, expires_at)
    )
    db.commit()
    return {"name": name, "token_hash": token_hash, "expires_at": expires_at or None}


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
@pytest.mark.parametrize("mode", ["sharing", "private"])
async def test_export_is_read_only_deterministic_allow_list_without_http(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, mode: ExportMode
) -> None:
    seed_app(db, "zulu", repo_url=f"https://user:{SENTINEL}@github.com/acme/zulu@main?secret={SENTINEL}")
    seed_app(db, "alpha", repo_url=f"file://{APPS_DIR}/file_browser", status="stopped")
    seed_app(db, "secrets", status="error")
    db.executemany(
        "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('zulu', ?, ?, ?)",
        [("z-port", 90, 9090), ("a-port", 80, 8080)],
    )
    db.execute("INSERT INTO app_tokens VALUES ('zulu', ?)", (SENTINEL,))
    db.execute("INSERT INTO users (username, password_hash) VALUES ('owner', ?)", (SENTINEL,))
    db.execute("INSERT INTO sessions VALUES (?, last_insert_rowid(), '')", (SENTINEL,))
    db.execute(
        "INSERT INTO permissions_v2 (consumer_app_id, service_url, grant_payload) VALUES ('zulu', ?, ?)",
        ("github.com/imbue-openhost/openhost/services/secrets", f"invalid JSON {SENTINEL}"),
    )
    expected_token = seed_api_token(db, "owner key", SENTINEL)
    before = list(db.iterdump())
    reads: set[tuple[str, str]] = set()

    def authorize(action: int, table: str, column: str, *args: object) -> int:
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            allowed = {
                "apps": {"app_id", "name", "repo_url"},
                "app_port_mappings": {"app_id", "label", "container_port", "host_port"},
            }
            if mode == "private":
                allowed["api_tokens"] = {"name", "token_hash", "expires_at"}
            reads.add((table, column))
            if column not in allowed.get(table, set()):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    monkeypatch.setattr(httpx.AsyncClient, "request", lambda *a, **kw: pytest.fail("export made an HTTP request"))
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("export made an HTTP request"))
    db.set_authorizer(authorize)
    try:
        first = await export_app_definitions(db, APPS_DIR, mode)
        assert first == await export_app_definitions(db, APPS_DIR, mode)
    finally:
        db.set_authorizer(None)
    assert list(db.iterdump()) == before
    assert not db.in_transaction
    assert first == json.dumps(json.loads(first), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert SENTINEL not in first
    document = json.loads(first)
    assert set(document) == {"schema_version", "mode", "apps"} | (
        {"platform_api_tokens"} if mode == "private" else set()
    )
    assert document["schema_version"] == 2
    assert document["mode"] == mode
    assert [app["name"] for app in document["apps"]] == ["alpha", "secrets", "zulu"]
    for app in document["apps"]:
        assert set(app) == {"name", "source", "port_mappings"}
    assert document["apps"][2]["port_mappings"] == [
        {"label": "a-port", "container_port": 80, "host_port": 8080},
        {"label": "z-port", "container_port": 90, "host_port": 9090},
    ]
    assert any(table == "api_tokens" for table, _ in reads) == (mode == "private")
    if mode == "private":
        assert document["platform_api_tokens"] == [expected_token]
    else:
        assert expected_token["token_hash"] not in first


@pytest.mark.asyncio
async def test_private_preserves_expired_noexpiry_and_duplicate_names_in_stable_order(db: sqlite3.Connection) -> None:
    records = [
        seed_api_token(db, "zulu", "synthetic-z", "2999-01-01T00:00:00+00:00"),
        seed_api_token(db, "duplicate", "synthetic-b", "2000-01-01T12:34:56.123456+05:30"),
        seed_api_token(db, "duplicate", "synthetic-a"),
        seed_api_token(db, "alpha", "synthetic-c", "2001-01-01T00:00:00Z"),
    ]
    first = await export_app_definitions(db, APPS_DIR, "private")
    document = json.loads(first)
    assert document["platform_api_tokens"] == sorted(records, key=lambda token: (token["name"], token["token_hash"]))
    assert document["apps"] == []
    # Physical insertion order and generated IDs must not affect the file.
    db.execute("DELETE FROM api_tokens")
    for record in reversed(records):
        db.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
            (record["name"], record["token_hash"], record["expires_at"] or ""),
        )
    db.commit()
    assert await export_app_definitions(db, APPS_DIR, "private") == first


@pytest.mark.asyncio
async def test_private_always_includes_empty_token_list(db: sqlite3.Connection) -> None:
    assert json.loads(await export_app_definitions(db, APPS_DIR, "private")) == {
        "schema_version": 2,
        "mode": "private",
        "apps": [],
        "platform_api_tokens": [],
    }


@pytest.mark.asyncio
async def test_apps_ports_and_tokens_share_one_sqlite_snapshot(tmp_path: Path) -> None:
    _make_test_config(tmp_path)
    with closing(get_db()) as db:
        seed_app(db, "consumer", repo_url="https://github.com/acme/original")
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('consumer', 'web', 80, 8080)"
        )
        token = seed_api_token(db, "original", "synthetic-original")
        changed = False

        def concurrent_writer(sql: str) -> None:
            nonlocal changed
            if "FROM app_port_mappings" in sql and not changed:
                changed = True
                with closing(get_db()) as writer:
                    writer.execute("UPDATE apps SET repo_url='https://github.com/acme/changed'")
                    writer.execute("UPDATE app_port_mappings SET host_port=9090")
                    writer.execute("UPDATE api_tokens SET name='changed', expires_at='2000-01-01T00:00:00+00:00'")
                    writer.commit()

        db.set_trace_callback(concurrent_writer)
        document = json.loads(await export_app_definitions(db, APPS_DIR, "private"))
        db.set_trace_callback(None)
        assert changed
        assert not db.in_transaction
        assert document["apps"][0]["source"]["repo_url"] == "https://github.com/acme/original"
        assert document["apps"][0]["port_mappings"][0]["host_port"] == 8080
        assert document["platform_api_tokens"] == [token]
        assert db.execute("SELECT name FROM api_tokens").fetchone()[0] == "changed"
