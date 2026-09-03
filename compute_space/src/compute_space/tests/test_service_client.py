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
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.proxy_target import InProcess
from compute_space.core.proxy_target import LocalPort
from compute_space.core.service_interface import builtin_services
from compute_space.core.service_interface.builtin_services import BuiltinService
from compute_space.core.service_interface.builtin_services import builtin_by_url
from compute_space.core.service_interface.provider import ProviderVersionError
from compute_space.core.service_interface.resolve import resolve_provider
from compute_space.core.service_interface.service_client import ServiceCallError
from compute_space.core.service_interface.service_client import call_service
from compute_space.core.service_interface.services import default_provider_id_for_service
from compute_space.core.service_interface.services import list_all_service_providers
from compute_space.core.service_interface.services import register_services_provided_by_app
from compute_space.core.service_interface.services import set_default
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
    assert default_provider_id_for_service(SERVICE_URL, conn) == ROUTER_APP_ID
    assert resolve_provider(SERVICE_URL, ">=0", conn).target == InProcess(registered.app)


def test_a_builtin_yields_to_an_app_the_owner_made_default(registered: BuiltinService, db: Any) -> None:
    # Installing a provider app and making it the default is all it should take to switch over.
    _, conn = db
    _install_app(conn, "someapp")
    conn.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'someapp')", (SERVICE_URL,))
    conn.commit()
    assert default_provider_id_for_service(SERVICE_URL, conn) == "someapp"
    assert resolve_provider(SERVICE_URL, ">=0", conn).target == LocalPort(19100)


def test_an_explicit_provider_override_is_honoured(registered: BuiltinService, db: Any) -> None:
    # An override beats the default in both directions: it can demand the builtin, and it can name
    # an app while the builtin still holds the service.
    _, conn = db
    _install_app(conn, "someapp")
    conn.commit()
    assert resolve_provider(SERVICE_URL, ">=0", conn, provider_app_id=ROUTER_APP_ID).target == InProcess(
        registered.app
    )
    assert resolve_provider(SERVICE_URL, ">=0", conn, provider_app_id="someapp").target == LocalPort(19100)


def test_a_builtin_is_listed_for_the_owner_alongside_the_apps(registered: BuiltinService, db: Any) -> None:
    # The owner picks a provider from this list, so a service the router serves has to appear in
    # it — otherwise there is no way to see what is handling the service, or to switch back.
    _, conn = db
    assert [(p.app_id, p.is_default) for p in list_all_service_providers(conn, SERVICE_URL)] == [(ROUTER_APP_ID, True)]

    _install_app(conn, "someapp")
    conn.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'someapp')", (SERVICE_URL,))
    conn.commit()
    assert [(p.app_id, p.is_default) for p in list_all_service_providers(conn, SERVICE_URL)] == [
        (ROUTER_APP_ID, False),
        ("someapp", True),
    ]


def test_the_router_can_be_chosen_as_the_default_like_any_other_provider(registered: BuiltinService, db: Any) -> None:
    # Callers pass ROUTER_APP_ID the same way they pass an app_id; that it is stored as the absence
    # of a row rather than a row naming the router stays inside these two functions.
    _, conn = db
    _install_app(conn, "someapp")
    set_default(SERVICE_URL, "someapp", conn)
    assert default_provider_id_for_service(SERVICE_URL, conn) == "someapp"

    set_default(SERVICE_URL, ROUTER_APP_ID, conn)
    assert default_provider_id_for_service(SERVICE_URL, conn) == ROUTER_APP_ID


def test_choosing_the_router_survives_the_provider_app_restarting(registered: BuiltinService, db: Any) -> None:
    # Registration runs on every install, start and reload.  If it claimed the service whenever no
    # default row existed, choosing the router — which is stored as the absence of that row — would
    # last exactly until the next reboot.
    _, conn = db
    _install_app(conn, "someapp")
    set_default(SERVICE_URL, "someapp", conn)
    set_default(SERVICE_URL, ROUTER_APP_ID, conn)

    register_services_provided_by_app("someapp", _manifest_providing(SERVICE_URL), conn)
    conn.commit()

    assert default_provider_id_for_service(SERVICE_URL, conn) == ROUTER_APP_ID
    assert resolve_provider(SERVICE_URL, ">=0", conn).target == InProcess(registered.app)


def test_the_router_cannot_be_made_default_for_a_service_it_does_not_provide(db: Any) -> None:
    _, conn = db
    with pytest.raises(LookupError, match="does not provide"):
        set_default("github.com/example/other", ROUTER_APP_ID, conn)


def test_an_unregistered_service_has_no_provider_at_all(registered: BuiltinService, db: Any) -> None:
    _, conn = db
    assert builtin_by_url("github.com/example/other") is None
    assert default_provider_id_for_service("github.com/example/other", conn) is None


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


def _manifest_providing(service_url: str) -> Any:
    """The manifest an installed provider app registers from."""
    return parse_manifest_from_string(
        f"""
[app]
name = "someapp"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.provides]]
service = "{service_url}"
version = "1.0.0"
endpoint = "/"
"""
    )


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
