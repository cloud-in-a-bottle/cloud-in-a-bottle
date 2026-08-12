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
from compute_space.web.routes.pages.apps import add_app

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config
from .conftest import primary_of


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
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
        route_handlers=[add_app],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _seed_catalog_app(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('catalogappid', 'catalog', '0.1.0', '/tmp/catalog', 19123, 'running')"
        )
        conn.commit()
    finally:
        conn.close()


def test_callout_links_to_catalog_when_installed(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_catalog_app(cfg.db_path)

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/add_app")
    assert resp.status_code == 200
    assert "Explore the App Catalog" in resp.text
    assert f"http://catalog.{primary_of(cfg).name}/" in resp.text
    assert "Install the catalog" not in resp.text
    assert "Available Built-in Apps" not in resp.text


def test_callout_offers_install_when_catalog_missing(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/add_app")
    assert resp.status_code == 200
    assert "Explore the App Catalog" in resp.text
    assert "Install the catalog" in resp.text
    assert "https://github.com/imbue-openhost/bottled-catalog" in resp.text
    assert f"http://catalog.{primary_of(cfg).name}/" not in resp.text
