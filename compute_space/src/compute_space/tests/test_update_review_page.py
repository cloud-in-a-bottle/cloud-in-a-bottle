from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import update_review

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config


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
        route_handlers=[update_review],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _seed_app(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('someappid00', 'myapp', '1.0.0', '/tmp/myapp', 19124, 'running')"
        )
        conn.commit()
    finally:
        conn.close()


def test_review_page_renders_for_known_app(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    _seed_app(cfg.db_path)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/update_review/myapp", cookies=cookie)
    assert resp.status_code == 200
    assert "Review Update" in resp.text
    assert "/reload_app/someappid00" in resp.text
    assert "/app_detail/myapp" in resp.text
    assert "js/update-review.js" in resp.text


def test_review_page_404_for_unknown_app(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        resp = client.get("/update_review/ghost", cookies=cookie)
    assert resp.status_code == 404
