from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from litestar import Litestar
from litestar import get
from litestar.testing import TestClient

from compute_space.config import Config
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.core.root_app import RootAppNotFoundError
from compute_space.core.root_app import clear_root_app_if
from compute_space.core.root_app import get_root_app_id
from compute_space.core.root_app import set_root_app_id
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.test_multidomain_proxy_integration import _client
from compute_space.tests.test_multidomain_proxy_integration import _router_health
from compute_space.tests.test_multidomain_proxy_integration import _seed_app
from compute_space.tests.test_multidomain_proxy_integration import backend  # noqa: F401
from compute_space.tests.test_multidomain_proxy_integration import backend_port  # noqa: F401
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.root_app import api_root_app_routes

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local", tls=False, mdns=True)


@get("/.well-known/openhost-identity", sync_to_thread=False)
def _router_identity() -> str:
    return "router-identity"


@pytest.fixture
def root_config(tmp_path: Path, backend_port: int) -> Config:  # noqa: F811
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [DomainRecord(LOCAL.name, LOCAL.tls, LOCAL.mdns)])
    _seed_app(cfg.db_path, "site", backend_port, public_paths=["/health", "/.well-known/webfinger"])
    # Never proxied to; app ports are unique in the DB.
    _seed_app(cfg.db_path, "other", backend_port + 1, public_paths=[])
    return cfg


@pytest.fixture
def wrapped_app(root_config: Config) -> Any:
    return SubdomainProxyMiddleware(Litestar(route_handlers=[_router_health, _router_identity], openapi_config=None))


def _app_id(cfg: Config, name: str) -> str:
    with closing(open_db(cfg)) as db:
        return str(db.execute("SELECT app_id FROM apps WHERE name = ?", (name,)).fetchone()["app_id"])


def _set_root(cfg: Config, name: str | None) -> None:
    with closing(open_db(cfg)) as db:
        set_root_app_id(db, _app_id(cfg, name) if name is not None else None)


# --- domain helpers -------------------------------------------------------------------


def test_router_host_and_url() -> None:
    assert PRIMARY.router_host == "bottle.host.example.com"
    assert PRIMARY.router_url == "https://bottle.host.example.com"
    # A port baked into the configured name is kept.
    assert Domain(name="lvh.me:8080").router_url == "http://bottle.lvh.me:8080"


def test_router_subdomain_is_not_an_app_subdomain() -> None:
    assert PRIMARY.is_router_subdomain("bottle.host.example.com:8443")
    assert not PRIMARY.looks_like_app_subdomain("bottle.host.example.com")
    assert PRIMARY.app_name_from_hostname("bottle.host.example.com") is None
    assert PRIMARY.looks_like_app_subdomain("site.host.example.com")


def test_router_subdomain_is_a_reserved_app_name() -> None:
    assert "/bottle" in RESERVED_PATHS


# --- setting ----------------------------------------------------------------------------


def test_default_is_dashboard(root_config: Config) -> None:
    with closing(open_db(root_config)) as db:
        assert get_root_app_id(db) is None
        assert get_app_from_hostname("host.example.com", db) is None


def test_bare_domain_resolves_to_root_app_on_every_domain(root_config: Config) -> None:
    _set_root(root_config, "site")
    with closing(open_db(root_config)) as db:
        for host in ("host.example.com", "myhost.local:8080"):
            app = get_app_from_hostname(host, db)
            assert app is not None and app.name == "site", host
        assert get_app_from_hostname("bottle.host.example.com", db) is None


def test_unknown_app_is_refused(root_config: Config) -> None:
    with closing(open_db(root_config)) as db:
        with pytest.raises(RootAppNotFoundError):
            set_root_app_id(db, "nope")


def test_removing_root_app_hands_root_back(root_config: Config) -> None:
    _set_root(root_config, "site")
    with closing(open_db(root_config)) as db:
        clear_root_app_if(db, _app_id(root_config, "other"))
        assert get_root_app_id(db) == _app_id(root_config, "site")
        clear_root_app_if(db, _app_id(root_config, "site"))
        assert get_root_app_id(db) is None


# --- routing through the middleware -----------------------------------------------------


@pytest.mark.asyncio
async def test_router_subdomain_always_serves_router(wrapped_app: Any, root_config: Config) -> None:
    for root in (None, "site"):
        _set_root(root_config, root)
        async with _client(wrapped_app) as c:
            r = await c.get("http://bottle.host.example.com/health")
        assert r.status_code == 200 and r.text == "router-ok", root


@pytest.mark.asyncio
async def test_bare_domain_serves_router_by_default(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://host.example.com/health")
    assert r.status_code == 200 and r.text == "router-ok"


@pytest.mark.asyncio
async def test_bare_domain_serves_root_app_and_its_subdomain_still_works(
    wrapped_app: Any, root_config: Config
) -> None:
    _set_root(root_config, "site")
    async with _client(wrapped_app) as c:
        root = await c.get("http://host.example.com/health")
        sub = await c.get("http://site.host.example.com/health")
    assert root.status_code == 200 and root.text == "backend-ok"
    assert root.headers["X-Backend-Saw-Host"] == "host.example.com"
    assert sub.status_code == 200 and sub.text == "backend-ok"


@pytest.mark.asyncio
async def test_router_keeps_reserved_well_known_paths(wrapped_app: Any, root_config: Config) -> None:
    _set_root(root_config, "site")
    async with _client(wrapped_app) as c:
        kept = await c.get("http://host.example.com/.well-known/openhost-identity")
        other = await c.get("http://host.example.com/.well-known/webfinger")
    assert kept.text == "router-identity"
    assert other.text == "backend-ok"


@pytest.mark.asyncio
async def test_private_root_path_redirects_to_login_on_router_subdomain(wrapped_app: Any, root_config: Config) -> None:
    _set_root(root_config, "site")
    async with _client(wrapped_app) as c:
        r = await c.get("http://host.example.com/x", headers={"Accept": "text/html"})
    assert r.status_code == 302
    location = urlsplit(r.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("https", "bottle.host.example.com", "/login")


@pytest.mark.asyncio
async def test_owner_reaches_private_root_path(wrapped_app: Any, root_config: Config) -> None:
    _set_root(root_config, "site")
    async with _client(wrapped_app) as c:
        c.cookies.update(auth_cookie(root_config))
        r = await c.get("http://host.example.com/x")
    assert r.status_code == 200 and r.text == "backend-ok"


@pytest.mark.asyncio
async def test_fully_private_root_app_redirects_to_login_rather_than_404(
    wrapped_app: Any, root_config: Config
) -> None:
    # A fully private app 404s on its subdomain, to hide that it exists; the bare domain always answers.
    _set_root(root_config, "other")
    async with _client(wrapped_app) as c:
        root = await c.get("http://host.example.com/", headers={"Accept": "text/html"})
        sub = await c.get("http://other.host.example.com/", headers={"Accept": "text/html"})
    assert root.status_code == 302
    assert urlsplit(root.headers["location"]).netloc == "bottle.host.example.com"
    assert sub.status_code == 404


# --- settings API ------------------------------------------------------------------------


def test_settings_api_lists_sets_and_resets(root_config: Config) -> None:
    site_id = _app_id(root_config, "site")
    with TestClient(app=make_test_app(api_root_app_routes), base_url="http://host.example.com") as client:
        assert client.get("/api/settings/root-app").status_code == 401
        client.cookies.update(auth_cookie(root_config))

        initial = client.get("/api/settings/root-app").json()
        assert initial["app_id"] is None
        assert [a["name"] for a in initial["apps"]] == ["other", "site"]
        assert initial["router_url"] == "https://bottle.host.example.com"

        chosen = client.post("/api/settings/root-app", json={"app_id": site_id})
        assert chosen.status_code == 200 and chosen.json()["app_id"] == site_id

        assert client.post("/api/settings/root-app", json={"app_id": "nope"}).status_code == 404
        assert client.get("/api/settings/root-app").json()["app_id"] == site_id

        reset = client.post("/api/settings/root-app", json={"app_id": None})
        assert reset.status_code == 200 and reset.json()["app_id"] is None
