"""Tests for provider selection via X-OpenHost-Provider header and app-auth on the discovery endpoint."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.services_v2 import api_services_v2_routes
from compute_space.web.routes.services_v2 import services_v2_routes

SVC_DATA = "github.com/org/repo/services/data"

CONSUMER_TOKEN = "test-consumer-token"
CONSUMER_APP_ID = "ConsumerApp01"
CONSUMER_MANIFEST = f"""
[app]
name = "data-aggregator"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "{SVC_DATA}"
shortname = "data"
version = ">=0.1.0"
grants = []
"""


def _seed_consumer(db_path: str) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (CONSUMER_APP_ID, "data-aggregator", "0.1.0", "/tmp/data-aggregator", 19500, "running", CONSUMER_MANIFEST),
        )
        token_hash = hashlib.sha256(CONSUMER_TOKEN.encode()).hexdigest()
        db.execute("INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)", (CONSUMER_APP_ID, token_hash))
        db.commit()
    finally:
        db.close()


def _seed_providers(db_path: str) -> tuple[str, str]:
    """Seed two providers for SVC_DATA. Returns (provider_a_id, provider_b_id)."""
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        a_id = new_app_id()
        b_id = new_app_id()
        for app_id, name, port, version in [
            (a_id, "data-provider-a", 19001, "0.2.0"),
            (b_id, "data-provider-b", 19002, "0.3.0"),
        ]:
            db.execute(
                """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (app_id, name, "0.0.0", f"/tmp/{name}", port, "running"),
            )
            db.execute(
                "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, ?)",
                (SVC_DATA, app_id, version, "/api/"),
            )
        # provider A is the default
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, ?)", (SVC_DATA, a_id))
        db.commit()
        return a_id, b_id
    finally:
        db.close()


def _make_proxy_app() -> Litestar:
    return Litestar(
        route_handlers=[services_v2_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _make_api_app() -> Litestar:
    return Litestar(
        route_handlers=[api_services_v2_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _auth_headers(token: str = CONSUMER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cfg(tmp_path: Path) -> object:
    return _make_test_config(tmp_path, port=20600)


# ---------------------------------------------------------------------------
# X-OpenHost-Provider header
# ---------------------------------------------------------------------------


class TestProviderSelectionHeader:
    """Verify the X-OpenHost-Provider header routes to a specific provider."""

    def test_call_with_provider_header_routes_to_that_provider(self, cfg: object) -> None:
        init_db(cfg.db_path)  # type: ignore[attr-defined]
        _seed_consumer(cfg.db_path)  # type: ignore[attr-defined]
        _a_id, b_id = _seed_providers(cfg.db_path)  # type: ignore[attr-defined]

        fake_response = ASGIResponse(body=b'{"ok": true}', status_code=200)
        with (
            patch(
                "compute_space.web.routes.services_v2.proxy_http_request",
                new_callable=AsyncMock,
                return_value=fake_response,
            ) as mock_proxy,
            TestClient(app=_make_proxy_app()) as client,
        ):
            resp = client.get(
                "/api/services/v2/call/data/endpoint",
                headers={**_auth_headers(), "X-OpenHost-Provider": b_id},
            )
            assert resp.status_code == 200
            # Verify the proxy was called with provider B's port (19002)
            assert mock_proxy.called
            _, kwargs = mock_proxy.call_args
            assert kwargs["target_port"] == 19002

    def test_call_without_header_uses_default_provider(self, cfg: object) -> None:
        init_db(cfg.db_path)  # type: ignore[attr-defined]
        _seed_consumer(cfg.db_path)  # type: ignore[attr-defined]
        _seed_providers(cfg.db_path)  # type: ignore[attr-defined]

        fake_response = ASGIResponse(body=b'{"ok": true}', status_code=200)
        with (
            patch(
                "compute_space.web.routes.services_v2.proxy_http_request",
                new_callable=AsyncMock,
                return_value=fake_response,
            ) as mock_proxy,
            TestClient(app=_make_proxy_app()) as client,
        ):
            resp = client.get(
                "/api/services/v2/call/data/endpoint",
                headers=_auth_headers(),
            )
            assert resp.status_code == 200
            _, kwargs = mock_proxy.call_args
            # Default provider A is on port 19001
            assert kwargs["target_port"] == 19001

    def test_call_with_unknown_provider_returns_503(self, cfg: object) -> None:
        init_db(cfg.db_path)  # type: ignore[attr-defined]
        _seed_consumer(cfg.db_path)  # type: ignore[attr-defined]
        _seed_providers(cfg.db_path)  # type: ignore[attr-defined]

        with TestClient(app=_make_proxy_app()) as client:
            resp = client.get(
                "/api/services/v2/call/data/endpoint",
                headers={**_auth_headers(), "X-OpenHost-Provider": "nonexistent-app-id"},
            )
            assert resp.status_code == 503
            assert resp.json()["extra"]["code"] == "service_not_available"


# ---------------------------------------------------------------------------
# App auth on discovery endpoint
# ---------------------------------------------------------------------------


class TestDiscoverProvidersAppAuth:
    """Verify that apps can call GET /api/services/v2/providers with a bearer token."""

    def test_app_can_discover_providers(self, cfg: object) -> None:
        init_db(cfg.db_path)  # type: ignore[attr-defined]
        _seed_consumer(cfg.db_path)  # type: ignore[attr-defined]
        a_id, b_id = _seed_providers(cfg.db_path)  # type: ignore[attr-defined]

        with TestClient(app=_make_api_app()) as client:
            resp = client.get(
                "/api/services/v2/providers",
                params={"service": SVC_DATA},
                headers=_auth_headers(),
            )
            assert resp.status_code == 200
            providers = resp.json()["providers"]
            assert len(providers) == 2
            app_ids = {p["app_id"] for p in providers}
            assert a_id in app_ids
            assert b_id in app_ids
            # Verify default flag
            defaults = [p for p in providers if p["is_default"]]
            assert len(defaults) == 1
            assert defaults[0]["app_id"] == a_id

    def test_unauthenticated_discovery_returns_401(self, cfg: object) -> None:
        init_db(cfg.db_path)  # type: ignore[attr-defined]

        with TestClient(app=_make_api_app()) as client:
            resp = client.get(
                "/api/services/v2/providers",
                params={"service": SVC_DATA},
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Authorization stripping on the cross-app service proxy path (OH-231)
# ---------------------------------------------------------------------------


class _EchoHeadersHandler(BaseHTTPRequestHandler):
    """Records the headers of the last request it received."""

    last_headers: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        type(self).last_headers = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args: object) -> None:  # silence stderr noise
        pass


class TestServiceProxyStripsAuthorization:
    """The consumer authenticates to the router with Authorization: Bearer
    <app token>; the router must consume it and NOT forward it to the provider
    (OH-231).  The provider is told the caller's identity via X-OpenHost-*.
    """

    def test_service_proxy_strips_authorization(self, cfg: object) -> None:
        # Real backend so we exercise the full proxy_http_request path (no mock).
        server = HTTPServer(("127.0.0.1", 0), _EchoHeadersHandler)
        provider_port = server.server_address[1]
        _EchoHeadersHandler.last_headers = {}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            init_db(cfg.db_path)  # type: ignore[attr-defined]
            _seed_consumer(cfg.db_path)  # type: ignore[attr-defined]
            # Register a single provider for SVC_DATA pointing at the echo server.
            db = sqlite3.connect(cfg.db_path)  # type: ignore[attr-defined]
            try:
                provider_id = new_app_id()
                db.execute(
                    """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (provider_id, "data-provider", "0.1.0", "/tmp/data-provider", provider_port, "running"),
                )
                db.execute(
                    "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) "
                    "VALUES (?, ?, ?, ?)",
                    (SVC_DATA, provider_id, "0.1.0", "/api/"),
                )
                db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, ?)", (SVC_DATA, provider_id))
                db.commit()
            finally:
                db.close()

            with TestClient(app=_make_proxy_app()) as client:
                resp = client.get(
                    "/api/services/v2/call/data/endpoint",
                    headers=_auth_headers(),  # Authorization: Bearer <consumer app token>
                )
            assert resp.status_code == 200

            received = _EchoHeadersHandler.last_headers
            # The consumer's app token must not reach the provider backend.
            assert "authorization" not in received
            assert CONSUMER_TOKEN not in received.get("authorization", "")
            # ...but the router still identifies the caller to the provider.
            assert received.get("x-openhost-consumer-id") == CONSUMER_APP_ID
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
