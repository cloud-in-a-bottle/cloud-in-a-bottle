"""Calling a service from inside the router: which provider serves it, and over what."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import post

from compute_space.config import DefaultConfig
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.proxy_target import InProcess
from compute_space.core.proxy_target import LocalPort
from compute_space.core.service_interface import builtin_services
from compute_space.core.service_interface.builtin_services import BuiltinService
from compute_space.core.service_interface.builtin_services import builtin_for
from compute_space.core.service_interface.provider import ProviderVersionError
from compute_space.core.service_interface.registry import providers_for
from compute_space.core.service_interface.resolve import resolve_provider
from compute_space.core.service_interface.service_client import ServiceCallError
from compute_space.core.service_interface.service_client import call_service
from compute_space.db import init_db
from compute_space.tests.conftest import open_db

SERVICE_URL = "github.com/example/svc"


@post("/echo", status_code=200, sync_to_thread=False)
def echo(data: dict[str, Any], request: Request[Any, Any, Any]) -> dict[str, Any]:
    """Reflects what actually arrived, so a test can check the wire rather than the intent."""
    return {"body": data, "headers": {k.lower(): v for k, v in request.headers.items()}}


@post("/boom", status_code=200, sync_to_thread=False)
def boom() -> Response[Any]:
    return Response({"error": "nope", "message": "not today"}, status_code=400)


@post("/partial", status_code=200, sync_to_thread=False)
def partial() -> Response[Any]:
    return Response({"ok": False, "results": []}, status_code=207)


_APP = Litestar(route_handlers=[echo, boom, partial], openapi_config=None)


def _service(version: str = "1.0.0") -> BuiltinService:
    return BuiltinService(service_url=SERVICE_URL, version=version, app=_APP)


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> BuiltinService:
    service = _service()
    monkeypatch.setattr(builtin_services, "BUILTIN_SERVICES", (service,))
    return service


@pytest.fixture
def db(tmp_path: Path) -> Any:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.make_all_dirs()
    init_db(config.db_path)
    with closing(open_db(config)) as conn:
        yield config, conn


# ─── which provider serves it ───


def test_a_builtin_serves_when_no_app_has_claimed_the_service(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    assert builtin_for(SERVICE_URL, conn) is registered
    assert resolve_provider(SERVICE_URL, ">=0", conn).target == InProcess(registered.app)


def test_a_builtin_yields_to_an_app_the_owner_made_default(registered: BuiltinService, db: Any) -> None:
    # Installing a provider app and making it the default is all it should take to switch over.
    _, conn = db
    _install_app(conn, "someapp")
    conn.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'someapp')", (SERVICE_URL,))
    conn.commit()
    assert builtin_for(SERVICE_URL, conn) is None
    assert resolve_provider(SERVICE_URL, ">=0", conn).target == LocalPort(19100)


def test_an_explicit_provider_override_is_honoured(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    assert builtin_for(SERVICE_URL, conn, provider_override=ROUTER_APP_ID) is registered
    assert builtin_for(SERVICE_URL, conn, provider_override="someapp") is None


def test_a_builtin_is_listed_for_the_owner_alongside_the_apps(registered: BuiltinService, db: Any) -> None:
    # The owner picks a provider from this list, so a service the router serves has to appear in
    # it — otherwise there is no way to see what is handling the service, or to switch back.
    _, conn = db
    assert [(p.app_id, p.is_default) for p in providers_for(SERVICE_URL, conn)] == [(ROUTER_APP_ID, True)]

    _install_app(conn, "someapp")
    conn.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'someapp')", (SERVICE_URL,))
    conn.commit()
    assert [(p.app_id, p.is_default) for p in providers_for(SERVICE_URL, conn)] == [
        (ROUTER_APP_ID, False),
        ("someapp", True),
    ]


def test_an_unregistered_service_has_no_builtin(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    assert builtin_for("github.com/example/other", conn) is None


# ─── calling it ───


@pytest.mark.asyncio
async def test_a_builtin_is_served_without_a_socket(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    body = await call_service(SERVICE_URL, "/echo", {"hello": "world"}, [], conn)
    assert body["body"] == {"hello": "world"}


@pytest.mark.asyncio
async def test_the_router_asserts_its_own_identity(registered: BuiltinService, db: Any) -> None:
    # The router has no app token; it is the authority for these headers, so it injects the same
    # ones the proxy would have for a consumer app.
    _, conn = db
    body = await call_service(SERVICE_URL, "/echo", {}, [], conn)
    assert body["headers"]["x-openhost-consumer-id"] == ROUTER_APP_ID


@pytest.mark.asyncio
async def test_permissions_reach_the_provider_untouched(registered: BuiltinService, db: Any) -> None:
    # A grant payload is defined by the service that issues it, so nothing in between may reshape
    # one.
    _, conn = db
    grants = [{"grant": {"anything": "at all"}, "scope": "global"}]
    body = await call_service(SERVICE_URL, "/echo", {}, grants, conn)
    assert body["headers"]["x-openhost-permissions"] == '[{"grant": {"anything": "at all"}, "scope": "global"}]'


@pytest.mark.asyncio
async def test_an_error_status_raises_and_carries_the_body(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    with pytest.raises(ServiceCallError, match="not today") as caught:
        await call_service(SERVICE_URL, "/boom", {}, [], conn)
    assert caught.value.status == 400
    assert caught.value.body["error"] == "nope"


@pytest.mark.asyncio
async def test_a_multi_status_is_not_a_failure(registered: BuiltinService, db: Any) -> None:
    # 207 means some of a fan-out applied; the caller inspects the per-item results.
    _, conn = db
    assert await call_service(SERVICE_URL, "/partial", {}, [], conn) == {"ok": False, "results": []}


@pytest.mark.asyncio
async def test_a_version_the_builtin_cannot_satisfy_is_refused(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    with pytest.raises(ServiceCallError, match="does not match"):
        await call_service(SERVICE_URL, "/echo", {}, [], conn, version=">=2.0.0")
    with pytest.raises(ProviderVersionError):
        resolve_provider(SERVICE_URL, ">=2.0.0", conn)


@pytest.mark.asyncio
async def test_no_provider_at_all_is_a_clear_error(db: Any) -> None:
    # No builtin registered and no app installed: say so, rather than failing at connect time.
    _, conn = db
    with pytest.raises(ServiceCallError, match="no usable provider"):
        await call_service("github.com/example/missing", "/echo", {}, [], conn)


def _install_app(conn: sqlite3.Connection, app_id: str) -> None:
    """An app that is running and provides SERVICE_URL — an alternative to the builtin."""
    conn.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, status, local_port) "
        "VALUES (?, ?, '0.1.0', '/tmp/a', 'running', 19100)",
        (app_id, app_id),
    )
    conn.execute(
        "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, ?)",
        (SERVICE_URL, app_id, "1.0.0", "/"),
    )
