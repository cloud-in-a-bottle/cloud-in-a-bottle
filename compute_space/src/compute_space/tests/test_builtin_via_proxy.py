"""An app calling a router-provided service through the ordinary v2 proxy.

The point of a builtin is that a consumer app cannot tell one from a provider app: same URL, same
auth, same identity and permission headers, same 403-with-grant_url handling.  These tests drive
the real proxy route, so the only thing standing in for reality is the builtin itself.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.di import Provide
from litestar.exceptions import WebSocketDisconnect
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.service_interface import builtin_services
from compute_space.core.service_interface.builtin_services import BuiltinService
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_services_v2 import _add_provider
from compute_space.web.routes.services_v2 import services_v2_routes

SERVICE_URL = "github.com/imbue-openhost/openhost/services/example"
CONSUMER_TOKEN = "test-consumer-token"
CONSUMER_APP_ID = "ConsumerApp01"
CONSUMER_MANIFEST = f"""
[app]
name = "consumer"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "{SERVICE_URL}"
shortname = "example"
version = ">=0.1.0"
grants = []
"""


@get("/whoami", status_code=200, sync_to_thread=False)
def whoami(request: Request[Any, Any, Any]) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items() if k.lower().startswith("x-openhost-")}


@post("/needs-permission", status_code=200, sync_to_thread=False)
def needs_permission() -> Response[Any]:
    """The 403 shape a provider returns when it wants a grant it hasn't been given."""
    return Response(
        {"code": "permission_required", "required_grant": {"grant": {"capability": "write"}, "scope": "global"}},
        status_code=403,
    )


BUILTIN_APP = Litestar(route_handlers=[whoami, needs_permission], openapi_config=None)
BUILTIN = BuiltinService(service_url=SERVICE_URL, version="1.0.0", app=BUILTIN_APP)


def _seed_consumer(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (CONSUMER_APP_ID, "consumer", "0.1.0", "/tmp/consumer", 19500, "running", CONSUMER_MANIFEST),
        )
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (CONSUMER_APP_ID, hashlib.sha256(CONSUMER_TOKEN.encode()).hexdigest()),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(builtin_services, "BUILTIN_SERVICES", (BUILTIN,))
    cfg = _make_test_config(tmp_path, port=20700)
    init_db(cfg.db_path)
    _seed_consumer(cfg.db_path)
    app = Litestar(
        route_handlers=[services_v2_routes],
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
        openapi_config=None,
    )
    with TestClient(app=app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {CONSUMER_TOKEN}"})
        yield test_client


def test_an_app_can_call_a_builtin_over_the_normal_service_path(client: Any) -> None:
    # The bug this covers: the proxy used to resolve providers from the DB only, so a builtin was
    # unreachable for apps no matter that it was registered.
    response = client.get("/api/services/v2/call/example/whoami")
    assert response.status_code == 200


def test_the_builtin_sees_the_caller_the_same_way_an_app_provider_would(client: Any) -> None:
    headers = client.get("/api/services/v2/call/example/whoami").json()
    assert headers["x-openhost-consumer-id"] == CONSUMER_APP_ID
    assert headers["x-openhost-consumer-name"] == "consumer"
    assert headers["x-openhost-permissions"] == "[]"


def test_a_builtins_403_is_decorated_with_a_grant_url(client: Any) -> None:
    # Injecting grant_url is the proxy's job, so a builtin gets it without doing anything.
    response = client.post("/api/services/v2/call/example/needs-permission")
    assert response.status_code == 403
    grant_url = response.json()["required_grant"]["grant_url"]
    assert "/approve-permissions-v2?" in grant_url
    assert CONSUMER_APP_ID in grant_url


def test_an_app_default_takes_the_service_away_from_the_builtin(client: Any, tmp_path: Path) -> None:
    # No app provides it, so pointing the default elsewhere leaves nothing that can serve.
    db = sqlite3.connect(_make_test_config(tmp_path, port=20700).db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES ('OtherApp001', 'other', '0.1.0', '/tmp/other', 19600, 'running')"
        )
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, 'OtherApp001')", (SERVICE_URL,))
        db.commit()
    finally:
        db.close()

    response = client.get("/api/services/v2/call/example/whoami")
    assert response.status_code == 503


def test_the_provider_header_can_demand_the_builtin(client: Any) -> None:
    response = client.get("/api/services/v2/call/example/whoami", headers={"X-OpenHost-Provider": ROUTER_APP_ID})
    assert response.status_code == 200


@pytest.mark.parametrize("rest", ["%2e%2e/admin", "%252e%252e/admin", "..;x/admin", "%5c..%5cadmin", "%00/admin"])
@pytest.mark.parametrize("websocket", [False, True])
def test_service_calls_require_ordinary_endpoint_paths(client, monkeypatch, rest: str, websocket: bool) -> None:
    with get_db() as db:
        _add_provider(db, SERVICE_URL, "provider", "1.0.0", "/service/")

    def unexpected(*args, **kwargs):
        pytest.fail("An invalid path must not contact the provider")

    monkeypatch.setattr("compute_space.web.routes.services_v2.proxy_http_request", unexpected)
    monkeypatch.setattr("compute_space.web.routes.services_v2.proxy_websocket_request", unexpected)
    path = f"/api/services/v2/call/example/{rest}"
    if websocket:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(path) as socket:
            socket.receive_text()
        assert exc.value.code == 4400
    else:
        assert client.get(path).status_code == 400


def test_service_path_and_query_reach_the_provider(client, monkeypatch) -> None:
    with get_db() as db:
        _add_provider(db, SERVICE_URL, "provider", "1.0.0", "/service/")
    requests = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "compute_space.web.helpers.proxy.client_for",
        lambda *_: (httpx.AsyncClient(transport=httpx.MockTransport(provider)), "http://provider.test"),
    )
    response = client.get("/api/services/v2/call/example/items/caf%C3%A9?q=a%2Fb")
    assert response.status_code == 200
    assert requests[0].url.path == "/service/items/café"
    assert requests[0].url.query == b"q=a%2Fb"
    assert requests[0].headers["x-openhost-consumer-id"] == CONSUMER_APP_ID
