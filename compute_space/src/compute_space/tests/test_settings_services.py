"""Settings ▸ Services: page render + the default-provider contract the UI uses."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.api.services_v2 import api_services_v2_routes
from compute_space.web.routes.pages.settings import settings_page

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    set_active_config(config)
    yield config


def _build_app(cfg: Any) -> Litestar:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[settings_page, api_services_v2_routes],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _seed_provider(db_path: str, app_id: str, name: str, port: int, service_url: str, version: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES (?, ?, '1.0', ?, ?, 'running')",
            (app_id, name, f"/tmp/{name}", port),
        )
        conn.execute(
            "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, '/')",
            (service_url, app_id, version),
        )
        conn.commit()
    finally:
        conn.close()


SERVICE = "github.com/x/mailer"


def _default_app_ids(client: TestClient[Any]) -> list[tuple[str, str]]:
    """What the settings page reads to preselect each service's provider."""
    return [(p["service_url"], p["app_id"]) for p in client.get("/api/services/v2").json() if p["is_default"]]


def test_settings_page_renders_services_section(cfg: Any) -> None:
    cookie = auth_cookie(cfg)
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Services" in resp.text
    assert 'id="services-status"' in resp.text
    assert "js/services.js" in resp.text


def test_list_services_returns_all_providers(cfg: Any) -> None:
    _seed_provider(cfg.db_path, "appone000001", "mailer-a", 21001, SERVICE, "1.0")
    _seed_provider(cfg.db_path, "apptwo000002", "mailer-b", 21002, SERVICE, "2.0")
    cookie = auth_cookie(cfg)
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/api/services/v2")
    assert resp.status_code == 200
    names = sorted(p["app_name"] for p in resp.json())
    assert names == ["mailer-a", "mailer-b"]


def test_set_and_clear_default_provider(cfg: Any) -> None:
    _seed_provider(cfg.db_path, "appone000001", "mailer-a", 21001, SERVICE, "1.0")
    _seed_provider(cfg.db_path, "apptwo000002", "mailer-b", 21002, SERVICE, "2.0")
    cookie = auth_cookie(cfg)
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        # With nothing chosen, the highest version serves.
        assert _default_app_ids(client) == [(SERVICE, "apptwo000002")]

        # Set (what the UI's "Save" with a provider selected sends) — the older one, so the
        # choice is distinguishable from the fallback.
        r = client.post("/api/services/v2/defaults", json={"service_url": SERVICE, "app_id": "appone000001"})
        assert r.status_code == 200
        assert _default_app_ids(client) == [(SERVICE, "appone000001")]

        # Clear (what "Save" with "(no default)" selected sends) — back to the highest version.
        r = client.request("DELETE", "/api/services/v2/defaults", json={"service_url": SERVICE})
        assert r.status_code == 200
        assert _default_app_ids(client) == [(SERVICE, "apptwo000002")]


def test_set_default_rejects_non_provider(cfg: Any) -> None:
    _seed_provider(cfg.db_path, "appone000001", "mailer-a", 21001, SERVICE, "1.0")
    cookie = auth_cookie(cfg)
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        # An app that doesn't provide this service can't be made its default.
        r = client.post("/api/services/v2/defaults", json={"service_url": SERVICE, "app_id": "apptwo000002"})
    assert r.status_code == 404
