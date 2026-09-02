"""Tests for the phone-sized nav (``_components/nav_menu.html``).

Below the layout breakpoint the icon row is hidden on every page and the dashboard's tiles go with
it, so the dashboard's hamburger is the only way through the space. That makes its contents
load-bearing: every destination the icons and tiles reach has to appear in the panel or a phone
visitor is stranded. Every other page's phone nav is the back link alone.

These tests pin both halves of that, plus the catalog entry's two forms.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

import compute_space.web.app as web_app
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.domains import Domain
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.helpers.zone import RequestOrigin
from compute_space.web.helpers.zone import set_request_origin
from compute_space.web.routes.pages.apps import CATALOG_APP_NAME
from compute_space.web.routes.pages.apps import dashboard

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config

# Everything the icon row and the tiles reach, as (label, href) pairs.
NAV_DESTINATIONS = (
    ("Deploy app", "/add_app"),
    ("Docs", "/docs/"),
    ("Terminal", "/terminal/"),
    ("System info", "/system/"),
    ("Settings", "/settings"),
)


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="alice-zone.example.com")
    init_db(config.db_path)
    yield config


def _install_catalog_app(cfg: Any) -> None:
    """Register the catalog in the apps table, the way a default-app install would."""
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status)"
            " VALUES ('catalogappid', ?, '0.1.0', '/tmp/catalog', 19123, 'running')",
            (CATALOG_APP_NAME,),
        )
        conn.commit()
    finally:
        conn.close()


def _squash(html: str) -> str:
    """Collapse runs of whitespace so assertions pin markup, not template line breaks."""
    return re.sub(r"\s+", " ", html)


def _build_dashboard_app(cfg: Any) -> Litestar:
    web_dir = Path(web_app.__file__).resolve().parent
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[dashboard],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _render_dashboard(cfg: Any) -> str:
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")
    with TestClient(app=_build_dashboard_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    return resp.text


def _render_icon_nav() -> str:
    """The nav as any page other than the dashboard renders it."""
    web_dir = Path(web_app.__file__).resolve().parent
    env = Environment(loader=FileSystemLoader(str(web_dir / "templates")), autoescape=True)
    env.globals["static_url"] = lambda name: f"/static/{name}"
    return env.from_string('{% from "_components/icon_nav.html" import icon_nav %}{{ icon_nav() }}').render()


@pytest.mark.parametrize(("label", "href"), NAV_DESTINATIONS)
def test_menu_carries_every_destination(cfg: Any, label: str, href: str) -> None:
    """Each icon-row and tile destination has a labelled entry in the panel."""
    body = _squash(_render_dashboard(cfg))
    assert f'<li><a href="{href}">{label}</a></li>' in body


def test_menu_panel_starts_closed(cfg: Any) -> None:
    """The panel ships ``hidden`` so a visitor without JS gets a shut menu rather than a list
    dumped over the page — nav-menu.js is what takes the attribute off."""
    body = _squash(_render_dashboard(cfg))
    assert '<div class="nav-menu__panel" id="nav-menu-panel" hidden>' in body
    assert 'aria-expanded="false"' in body
    assert 'aria-controls="nav-menu-panel"' in body


def test_dashboard_carries_both_nav_forms(cfg: Any) -> None:
    """The dashboard's markup carries the icons and the hamburger; CSS alone picks between them, so
    there is no server-side user-agent guess to get wrong."""
    body = _squash(_render_dashboard(cfg))
    assert 'class="icon-nav__icons"' in body
    assert 'class="nav-menu__toggle"' in body
    assert "img/icons/menu.svg" in body


def test_dashboard_loads_the_menu_script(cfg: Any) -> None:
    """The driver ships with the only page that has a menu to drive."""
    assert "js/nav-menu.js" in _render_dashboard(cfg)


def test_dashboard_nav_row_carries_the_heading(cfg: Any) -> None:
    """The tiles are hidden on a phone and take their "Dashboard" heading with them, so the nav row
    carries it instead -- a real heading, so the mobile page keeps a navigable outline."""
    body = _squash(_render_dashboard(cfg))
    assert '<h2 class="section-label icon-nav__label">Dashboard</h2>' in body
    nav = body.split('<div class="icon-nav', 1)[1].split("</div>", 1)[0]
    assert "icon-nav__label" in nav


def test_other_pages_have_no_hamburger() -> None:
    """Off the dashboard the phone nav is the back link alone, so there is no menu and no heading —
    and the row carries the modifier the stylesheet needs to hide it at that width."""
    rendered = _render_icon_nav()
    assert "nav-menu" not in rendered
    assert "icon-nav__label" not in rendered
    assert 'class="icon-nav icon-nav--icons-only"' in rendered


def test_other_pages_keep_the_icons() -> None:
    """Hiding the row is a phone-width concern; the markup still carries the icons for wide screens."""
    rendered = _render_icon_nav()
    assert 'class="icon-nav__icons"' in rendered
    assert 'href="/terminal/"' in rendered
    assert 'href="/settings"' in rendered


def test_menu_links_catalog_app_when_installed(cfg: Any) -> None:
    """An installed catalog is a real app on its own subdomain, so the entry leaves the space —
    matching the tile it stands in for."""
    _install_catalog_app(cfg)
    body = _squash(_render_dashboard(cfg))
    assert '<a href="http://catalog.alice-zone.example.com/" target="_blank" rel="noopener"> Catalog' in body


def test_menu_links_add_app_when_catalog_missing(cfg: Any) -> None:
    """With no catalog installed the entry points at the page that installs it."""
    body = _squash(_render_dashboard(cfg))
    assert "catalog.alice-zone.example.com" not in body
    # In-space page, so no new tab and no external marker.
    assert '<li><a href="/add_app">Catalog</a></li>' in body


def test_app_url_carries_origin_without_request_in_context(cfg: Any) -> None:
    """app_url must carry the arriving domain *and* access port even when the Jinja
    context has no ``request`` — the situation for links rendered inside *imported*
    macros (app_row, nav_menu), which is where the domain and port silently went
    missing.

    It reads the origin the middleware records, not ``context["request"]``, so a plain
    render with no request still gets it.
    """
    set_active_config(cfg)
    web_dir = Path(web_app.__file__).resolve().parent
    env = Environment(loader=FileSystemLoader(str(web_dir / "templates")), autoescape=True)
    env.globals.update(_template_globals(cfg, web_dir / "static"))

    # Arrived on a non-default port (SSH tunnel / NAT forward): links carry it.
    set_request_origin(
        RequestOrigin(zone=Domain("alice-zone.example.com", tls=False), netloc="alice-zone.example.com:8088")
    )
    out = env.from_string('{{ app_url("foo") }}').render()  # no `request` in context
    assert out == "http://foo.alice-zone.example.com:8088/"

    # No origin recorded: fall back to the live primary, no port appended.
    set_request_origin(None)
    out_default = env.from_string('{{ app_url("foo") }}').render()
    assert out_default == "http://foo.alice-zone.example.com/"
