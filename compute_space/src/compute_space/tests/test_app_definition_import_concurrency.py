import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_definitions import PlatformApiToken
from compute_space.db import get_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_app_definition_loader import document
from compute_space.tests.test_app_definitions_routes import client as client
from compute_space.web.helpers.app_definition_export import dump_export_yaml
from compute_space.web.routes.api import app_definition_loader


def test_import_write_contention_does_not_block_other_requests(
    client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    original = app_definition_loader.import_platform_api_tokens

    def tracked_import(db: sqlite3.Connection, tokens: tuple[PlatformApiToken, ...]) -> int:
        started.set()
        return original(db, tokens)

    monkeypatch.setattr(app_definition_loader, "import_platform_api_tokens", tracked_import)
    with closing(get_db()) as writer, ThreadPoolExecutor(max_workers=2) as pool:
        writer.execute("BEGIN IMMEDIATE")
        importing = pool.submit(
            client.post,
            "/api/app-definitions/import-private",
            json={"content": dump_export_yaml(document(private=True, apps=[]))},
        )
        try:
            assert started.wait(5)
            parsing = pool.submit(
                client.post, "/api/app-definitions/parse", json={"content": dump_export_yaml(document(apps=[]))}
            )
            assert parsing.result(timeout=2).status_code == 200
            assert not importing.done()
        finally:
            writer.rollback()
        assert importing.result(timeout=5).status_code == 200


@pytest.mark.asyncio
async def test_cancelled_request_does_not_close_the_import_workers_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_test_config(tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    connections: list[sqlite3.Connection] = []
    errors: list[BaseException] = []
    original = app_definition_loader.import_platform_api_tokens

    def own_connection() -> sqlite3.Connection:
        connection = get_db()
        connections.append(connection)
        return connection

    def paused_import(connection: sqlite3.Connection, tokens: tuple[PlatformApiToken, ...]) -> int:
        started.set()
        assert release.wait(5)
        return original(connection, tokens)

    def run_worker() -> None:
        try:
            app_definition_loader._import_tokens((PlatformApiToken("cancelled request", "a" * 64, None),))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(app_definition_loader, "get_db", own_connection)
    monkeypatch.setattr(app_definition_loader, "import_platform_api_tokens", paused_import)
    task = asyncio.create_task(asyncio.to_thread(run_worker))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert not errors
    with closing(get_db()) as db:
        row = db.execute("SELECT name FROM api_tokens WHERE token_hash = ?", ("a" * 64,)).fetchone()
        assert row is not None and row["name"] == "cancelled request"
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
