from __future__ import annotations

import re
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
from compute_space.web.routes.pages.system import diagnostics_page

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
        route_handlers=[diagnostics_page],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _render(cfg: Any) -> str:
    set_active_config(cfg)
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        resp = client.get("/diagnostics/")
    assert resp.status_code == 200
    return resp.text


def _tag(html: str, element_id: str) -> str:
    match = re.search(rf"<[^>]*\bid=\"{element_id}\"[^>]*>", html)
    assert match is not None, f"no element with id={element_id!r} in the page"
    return match.group(0)


def test_diagnostics_page_renders(cfg: Any) -> None:
    html = _render(cfg)
    assert "js/diagnostics.js" in html
    assert "/api/diagnostics" in html


def test_snapshot_actions_start_disabled(cfg: Any) -> None:
    """Copy and Refresh act on a snapshot the page has not fetched yet, so they ship disabled.

    diagnostics.js enables them once the first fetch lands.
    """
    html = _render(cfg)
    assert "disabled" in _tag(html, "copy-btn")
    assert "disabled" in _tag(html, "refresh-btn")


def test_download_link_starts_enabled(cfg: Any) -> None:
    """Download is a plain server request, so it keeps working even if the script never runs."""
    html = _render(cfg)
    download = _tag(html, "download-btn")
    assert "disabled" not in download
    assert "/api/diagnostics?download=1" in download
