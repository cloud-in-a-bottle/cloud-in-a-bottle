from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from litestar import Litestar
from litestar import get

from compute_space.config import Config
from compute_space.core.apps import validate_manifest
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.root_app import AppsBusyForDashboardMoveError
from compute_space.core.root_app import RootAppNotFoundError
from compute_space.core.root_app import RootKind
from compute_space.core.root_app import RootSetting
from compute_space.core.root_app import RootSettingError
from compute_space.core.root_app import apply_root_setting
from compute_space.core.root_app import clear_root_app_if
from compute_space.core.root_app import load_root_setting
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.test_multidomain_proxy_integration import _client
from compute_space.tests.test_multidomain_proxy_integration import _router_health
from compute_space.tests.test_multidomain_proxy_integration import _seed_app
from compute_space.tests.test_multidomain_proxy_integration import backend  # noqa: F401
from compute_space.tests.test_multidomain_proxy_integration import backend_port  # noqa: F401
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.discovery import discovery_routes

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local:8080", tls=False)


@get("/.well-known/openhost-identity", sync_to_thread=False)
def _router_identity() -> str:
    return "router-identity"


@pytest.fixture
def root_config(tmp_path: Path, backend_port: int) -> Config:  # noqa: F811
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [DomainRecord(LOCAL.name, LOCAL.tls)])
    _seed_app(cfg.db_path, "site", backend_port, public_paths=["/health", "/.well-known/webfinger"])
    # Never proxied to; app ports are unique in the DB.
    _seed_app(cfg.db_path, "other", backend_port + 1, public_paths=[])
    return cfg


@pytest.fixture
def wrapped_app(root_config: Config) -> Any:
    return SubdomainProxyMiddleware(
        Litestar(route_handlers=[_router_health, _router_identity, discovery_routes], openapi_config=None)
    )


def _app_id(cfg: Config, name: str) -> str:
    with closing(open_db(cfg)) as db:
        return str(db.execute("SELECT app_id FROM apps WHERE name = ?", (name,)).fetchone()["app_id"])


def _apply(cfg: Config, kind: RootKind, app: str | None = None, subdomain: str = "dash") -> bool:
    with closing(open_db(cfg)) as db:
        app_id = _app_id(cfg, app) if app is not None else None
        return apply_root_setting(db, RootSetting(kind=kind, app_id=app_id, router_subdomain=subdomain))


def _move(cfg: Config, kind: RootKind, app: str | None = None) -> None:
    """Apply, then stand in for the restart that brings the apps back up."""
    _apply(cfg, kind, app=app)
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE apps SET status = 'running', error_message = NULL")
        db.commit()


def _setting(cfg: Config) -> RootSetting:
    with closing(open_db(cfg)) as db:
        return load_root_setting(db)


# --- the setting ----------------------------------------------------------------------------


def test_default_is_dashboard_at_the_bare_domain(root_config: Config) -> None:
    setting = _setting(root_config)
    assert setting == RootSetting()
    assert setting.router_label is None
    assert setting.router_url(PRIMARY) == "https://host.example.com"
    assert setting.router_url(LOCAL) == "http://myhost.local:8080"


def test_router_moves_to_the_chosen_subdomain() -> None:
    setting = RootSetting(kind=RootKind.NOTHING, router_subdomain="dash")
    assert setting.router_host(PRIMARY) == "dash.host.example.com"
    assert setting.router_url(LOCAL) == "http://dash.myhost.local:8080"
    assert setting.is_router_host(PRIMARY, "dash.host.example.com:8443")
    assert not setting.is_router_host(PRIMARY, "host.example.com")


def test_unknown_app_is_refused(root_config: Config) -> None:
    with closing(open_db(root_config)) as db:
        with pytest.raises(RootAppNotFoundError):
            apply_root_setting(db, RootSetting(kind=RootKind.APP, app_id="nope"))


@pytest.mark.parametrize("subdomain", ["site", "Bad_Name", "-x"])
def test_dashboard_subdomain_must_be_free_and_valid(root_config: Config, subdomain: str) -> None:
    with pytest.raises(RootSettingError):
        _apply(root_config, RootKind.NOTHING, subdomain=subdomain)


def test_dashboard_subdomain_is_only_checked_while_the_dashboard_is_on_it(root_config: Config) -> None:
    # "site" is an app, but the subdomain is unused while the dashboard is at the bare domain.
    assert _apply(root_config, RootKind.DASHBOARD, subdomain="site") is False


def test_moving_the_dashboard_restarts_running_apps(root_config: Config) -> None:
    assert _apply(root_config, RootKind.NOTHING) is True
    with closing(open_db(root_config)) as db:
        statuses = {row["name"]: row["status"] for row in db.execute("SELECT name, status FROM apps")}
    assert statuses == {"site": "starting", "other": "starting"}


def test_changing_the_root_without_moving_the_dashboard_restarts_nothing(root_config: Config) -> None:
    _apply(root_config, RootKind.NOTHING)
    with closing(open_db(root_config)) as db:
        db.execute("UPDATE apps SET status = 'running'")
        db.commit()
    assert _apply(root_config, RootKind.APP, app="site") is False
    with closing(open_db(root_config)) as db:
        assert {row["status"] for row in db.execute("SELECT status FROM apps")} == {"running"}


def test_moving_the_dashboard_waits_for_busy_apps(root_config: Config) -> None:
    with closing(open_db(root_config)) as db:
        db.execute("UPDATE apps SET status = 'building' WHERE name = 'other'")
        db.commit()
    with pytest.raises(AppsBusyForDashboardMoveError):
        _apply(root_config, RootKind.NOTHING)
    assert _setting(root_config) == RootSetting()


def test_app_names_refuse_the_dashboard_subdomain_only_while_it_is_used(root_config: Config) -> None:
    manifest = parse_manifest_from_string(
        '[app]\nname = "dash"\nversion = "1.0.0"\n\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    with closing(open_db(root_config)) as db:
        assert validate_manifest(manifest, db) is None
    _apply(root_config, RootKind.NOTHING)
    with closing(open_db(root_config)) as db:
        assert validate_manifest(manifest, db) == "App name 'dash' is the dashboard's subdomain"


def test_removing_the_root_app_leaves_the_bare_domain_empty(root_config: Config) -> None:
    _apply(root_config, RootKind.APP, app="site")
    with closing(open_db(root_config)) as db:
        clear_root_app_if(db, _app_id(root_config, "other"))
        assert load_root_setting(db).kind == RootKind.APP
        clear_root_app_if(db, _app_id(root_config, "site"))
        # The dashboard stays where it is, so apps needn't restart.
        assert load_root_setting(db) == RootSetting(kind=RootKind.NOTHING, router_subdomain="dash")


# --- routing through the middleware -------------------------------------------------------


@pytest.mark.asyncio
async def test_default_serves_the_dashboard_at_the_bare_domain_only(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        root = await c.get("http://host.example.com/health")
        dash = await c.get("http://dash.host.example.com/health")
    assert root.text == "router-ok"
    assert dash.status_code == 404


@pytest.mark.asyncio
async def test_root_app_with_dashboard_on_a_subdomain(wrapped_app: Any, root_config: Config) -> None:
    _move(root_config, RootKind.APP, app="site")
    async with _client(wrapped_app) as c:
        root = await c.get("http://host.example.com/health")
        sub = await c.get("http://site.host.example.com/health")
        dash = await c.get("http://dash.host.example.com/health")
        kept = await c.get("http://host.example.com/.well-known/openhost-identity")
        other = await c.get("http://host.example.com/.well-known/webfinger")
    assert root.text == "backend-ok" and root.headers["X-Backend-Saw-Host"] == "host.example.com"
    assert sub.text == "backend-ok"
    assert dash.text == "router-ok"
    assert kept.text == "router-identity"
    assert other.text == "backend-ok"


@pytest.mark.asyncio
async def test_nothing_at_the_bare_domain(wrapped_app: Any, root_config: Config) -> None:
    _move(root_config, RootKind.NOTHING)
    async with _client(wrapped_app) as c:
        root = await c.get("http://host.example.com/health")
        kept = await c.get("http://host.example.com/.well-known/openhost-identity")
        dash = await c.get("http://dash.host.example.com/health")
    assert root.status_code == 404
    assert kept.text == "router-identity"
    assert dash.text == "router-ok"


@pytest.mark.asyncio
async def test_private_root_paths_redirect_to_login_on_the_dashboard(wrapped_app: Any, root_config: Config) -> None:
    for app in ("site", "other"):  # partly and fully private
        _move(root_config, RootKind.APP, app=app)
        async with _client(wrapped_app) as c:
            r = await c.get("http://host.example.com/x", headers={"Accept": "text/html"})
        assert r.status_code == 302, app
        location = urlsplit(r.headers["location"])
        assert (location.scheme, location.netloc, location.path) == ("https", "dash.host.example.com", "/login")


@pytest.mark.asyncio
async def test_owner_reaches_private_root_path(wrapped_app: Any, root_config: Config) -> None:
    _move(root_config, RootKind.APP, app="site")
    async with _client(wrapped_app) as c:
        c.cookies.update(auth_cookie(root_config))
        r = await c.get("http://host.example.com/x")
    assert r.status_code == 200 and r.text == "backend-ok"


# --- discovery ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_follows_the_dashboard(wrapped_app: Any, root_config: Config) -> None:
    cases = [
        (None, "host.example.com", "https://host.example.com"),
        (None, "myhost.local:8080", "http://myhost.local:8080"),
        (RootKind.APP, "host.example.com", "https://dash.host.example.com"),
        (RootKind.APP, "dash.host.example.com", "https://dash.host.example.com"),
        (RootKind.NOTHING, "myhost.local:8080", "http://dash.myhost.local:8080"),
    ]
    for kind, host, expected in cases:
        if kind is not None:
            _move(root_config, kind, app="site" if kind == RootKind.APP else None)
        async with _client(wrapped_app) as c:
            r = await c.get(f"http://{host}/.well-known/cloud-in-a-bottle")
        assert r.status_code == 200, (kind, host)
        assert r.json() == {"router_url": expected}, (kind, host)
        assert r.headers["Access-Control-Allow-Origin"] == "*"
