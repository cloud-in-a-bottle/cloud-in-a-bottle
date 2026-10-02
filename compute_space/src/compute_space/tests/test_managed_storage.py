import asyncio
import json
from contextlib import closing

import httpx
import pytest
from litestar.testing import TestClient

import compute_space.web.routes.api.managed_storage as routes
from compute_space.core import archive_backend
from compute_space.core import managed_storage
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.settings_store import set_setting
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes
from compute_space.web.routes.api.managed_storage import managed_usage
from compute_space.web.routes.pages.settings import settings_page

ALLOCATION = "a" * 32
BINDING = {
    "allocation_id": ALLOCATION,
    "service_url": "https://storage.example",
    "s3_bucket": "managed-bucket",
    "s3_endpoint": "https://objects.example",
}
IDENTITY = KeycloakClientCredentials("https://identity.example/realms/instances", "instance", "private-secret")


def snapshot():
    return {
        "version": 1,
        "allocation_id": ALLOCATION,
        "phase": "ready",
        "capacity_bytes": 100 * 1024**3,
        "desired_access": "read_write",
        "applied_access": "read_write",
        "reason": "within_allowance",
        "enforcement_enabled": True,
        "stale": False,
        "observed_at": 1790852400,
        "applied_at": 1790852400,
        "reported_at": 1790852410,
        "usage": {
            "used_bytes": 25 * 1024**3,
            "operation_microcents": 25000000,
            "storage_microcents": 5000000,
            "read_only_at_microcents": 100000000,
            "suspend_at_microcents": 200000000,
            "sample_at": 1790852399,
            "period_start": "2026-10-01",
            "resets_at": "2026-11-01",
        },
    }


@pytest.fixture
def cfg(tmp_path):
    return _make_test_config(tmp_path)


def bind(cfg, raw=None):
    with closing(open_db(cfg)) as db:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket=?, s3_endpoint=?",
            (BINDING["s3_bucket"], BINDING["s3_endpoint"]),
        )
        db.commit()
        set_setting(db, managed_storage.SETTING_KEY, json.dumps(BINDING) if raw is None else raw)
        set_instance_identity(db, IDENTITY)


@pytest.fixture
def transport(monkeypatch):
    result = {"status": 200, "body": snapshot(), "calls": [], "clients": []}
    real_client = httpx.AsyncClient

    def handle(request):
        result["calls"].append(request)
        if request.url.host == "identity.example":
            assert request.method == "POST"
            assert b"client_secret=private-secret" in request.content
            return httpx.Response(200, json={"access_token": "private-bearer", "expires_in": 300})
        assert request.url.host == "storage.example"
        assert request.headers["Authorization"] == "Bearer private-bearer"
        return httpx.Response(result["status"], json=result["body"])

    def client(**kwargs):
        instance = real_client(transport=httpx.MockTransport(handle), **kwargs)
        result["clients"].append(instance)
        return instance

    monkeypatch.setattr(managed_storage.httpx, "AsyncClient", client)
    return result


def test_local_and_byo_storage_do_not_call_backend(cfg, transport):
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.get("/api/storage/managed_usage").json() == {"managed": False, "status": None, "error": None}
        with closing(open_db(cfg)) as db:
            db.execute(
                "UPDATE archive_backend SET backend='s3', s3_bucket='bottle-looking-name', s3_endpoint='https://objects.example'"
            )
            db.commit()
        assert not client.get("/api/storage/managed_usage").json()["managed"]
    assert not transport["calls"]


def test_owner_auth_is_required(cfg, transport):
    bind(cfg)
    with TestClient(make_test_app(managed_usage)) as client:
        response = client.get("/api/storage/managed_usage")
        assert response.status_code in (401, 403)
    assert not transport["calls"]


def test_authenticated_proxy_redacts_secrets_and_closes_clients(cfg, transport):
    bind(cfg)
    transport["body"]["unexpected_secret"] = "private-upstream-value"
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/managed_usage")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["status"]["usage"]["used_bytes"] == 25 * 1024**3
    for secret in ("private-secret", "private-bearer", "private-upstream-value"):
        assert secret not in response.text
    assert all(client.is_closed for client in transport["clients"])


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 429, 500, 503])
def test_upstream_errors_are_generic(cfg, transport, status):
    bind(cfg)
    transport.update(status=status, body={"error": "private-secret-detail"})
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/managed_usage")
    assert response.status_code == 503 and response.json()["managed"]
    assert "private-secret-detail" not in response.text
    assert len(transport["calls"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("capacity_bytes", -1),
        ("capacity_bytes", True),
        ("stale", "false"),
        ("applied_access", "admin"),
        ("phase", "unknown"),
        ("allocation_id", "b" * 32),
        ("reported_at", 1.5),
    ],
)
def test_bad_snapshot_is_not_forwarded(cfg, transport, field, value):
    bind(cfg)
    transport["body"][field] = value
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/managed_usage")
    assert response.status_code == 503 and response.json()["status"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("used_bytes", None),
        ("operation_microcents", -1),
        ("read_only_at_microcents", 0),
        ("suspend_at_microcents", 1),
        ("resets_at", "invalid"),
        ("period_start", "2027-01-01"),
    ],
)
def test_bad_usage_is_not_rendered_as_zero(cfg, transport, field, value):
    bind(cfg)
    transport["body"]["usage"][field] = value
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.get("/api/storage/managed_usage").status_code == 503


def test_migration_away_hides_old_binding(cfg, transport):
    bind(cfg)
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE archive_backend SET s3_bucket='my-own-bucket'")
        db.commit()
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert not client.get("/api/storage/managed_usage").json()["managed"]
    assert not transport["calls"]


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "null",
        "[]",
        "{}",
        json.dumps({**BINDING, "service_url": "http://unsafe"}),
        json.dumps({**BINDING, "service_url": "https://user:secret@host"}),
    ],
)
def test_invalid_binding_is_not_used(cfg, transport, raw):
    bind(cfg, raw)
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/managed_usage")
    assert response.status_code == 503
    assert not transport["calls"]


def test_archive_state_exposes_only_explicit_matching_binding(cfg, monkeypatch):
    bind(cfg)
    monkeypatch.setattr(archive_backend, "list_meta_dumps", lambda *args: None)
    with TestClient(make_test_app(api_archive_backend_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.get("/api/storage/archive_backend").json()["managed_storage_allocation_id"] == ALLOCATION
        with closing(open_db(cfg)) as db:
            db.execute("UPDATE archive_backend SET s3_endpoint='https://other.example'")
            db.commit()
        assert client.get("/api/storage/archive_backend").json()["managed_storage_allocation_id"] is None


def test_archive_state_reports_invalid_managed_configuration(cfg, monkeypatch):
    bind(cfg, "{bad json")
    monkeypatch.setattr(archive_backend, "list_meta_dumps", lambda *args: None)
    with TestClient(make_test_app(api_archive_backend_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/archive_backend").json()
    assert response["managed_storage_allocation_id"] is None
    assert response["state_message"] == "Managed storage connection needs attention."


def test_oversized_response_is_bounded(cfg, transport):
    bind(cfg)
    transport["body"]["unexpected"] = "x" * 70000
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.get("/api/storage/managed_usage").status_code == 503
    assert all(client.is_closed for client in transport["clients"])


def test_operation_only_snapshot_preserves_unknown_storage(cfg, transport):
    bind(cfg)
    transport["body"]["usage"].update(
        used_bytes=None, sample_at=None, storage_microcents=None, operations_observed_at=1790852400
    )
    transport["body"]["stale"] = True
    with TestClient(make_test_app(managed_usage)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/api/storage/managed_usage")
    assert response.status_code == 200
    usage = response.json()["status"]["usage"]
    assert usage["used_bytes"] is None and usage["sample_at"] is None and usage["storage_microcents"] is None
    assert usage["operation_microcents"] == 25000000 and usage["operations_observed_at"] == 1790852400


@pytest.mark.asyncio
async def test_cancellation_and_total_deadline_close_clients(monkeypatch):
    clients = []
    entered = asyncio.Event()
    real = httpx.AsyncClient

    async def wait(request):
        entered.set()
        await asyncio.Event().wait()

    def client(**kwargs):
        value = real(transport=httpx.MockTransport(wait), **kwargs)
        clients.append(value)
        return value

    monkeypatch.setattr(managed_storage.httpx, "AsyncClient", client)
    binding = managed_storage.ManagedStorageBinding(**BINDING)
    task = asyncio.create_task(managed_storage.fetch_status(binding, IDENTITY))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(value.is_closed for value in clients)
    monkeypatch.setattr(managed_storage, "STATUS_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(managed_storage.ManagedStorageError):
        await managed_storage.fetch_status(binding, IDENTITY)
    assert all(value.is_closed for value in clients)


@pytest.mark.asyncio
async def test_configuration_change_during_fetch_discards_old_snapshot(cfg, monkeypatch):
    bind(cfg)

    async def fetch(*args):
        with closing(open_db(cfg)) as db:
            db.execute("UPDATE archive_backend SET s3_bucket='other'")
            db.commit()
        return managed_storage._converter.structure(snapshot(), managed_storage.ManagedStatus)

    monkeypatch.setattr(routes, "fetch_status", fetch)
    with closing(open_db(cfg)) as db:
        response = await routes.managed_usage.fn(db=db, config=cfg)
    assert response.status_code == 503 and response.content.status is None


@pytest.mark.asyncio
async def test_settings_bootstrap_only_reads_local_binding(cfg, monkeypatch):
    bind(cfg)

    def forbidden(*args):
        raise AssertionError("settings bootstrap must not query object storage")

    monkeypatch.setattr(archive_backend, "list_meta_dumps", forbidden)
    with closing(open_db(cfg)) as db:
        response = await settings_page.fn(db=db)
    assert response.context["managed_storage_allocation_id"] == ALLOCATION
    assert response.context["managed_storage_error"] is None


@pytest.mark.asyncio
async def test_settings_reports_invalid_binding_without_blocking_page(cfg):
    bind(cfg, "null")
    with closing(open_db(cfg)) as db:
        response = await settings_page.fn(db=db)
    assert response.context["managed_storage_allocation_id"] is None
    assert response.context["managed_storage_error"] == "Managed storage connection needs attention."
