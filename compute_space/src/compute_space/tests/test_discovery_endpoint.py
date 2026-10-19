from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar

from compute_space.config import Config
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.test_multidomain_proxy_integration import _client
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.discovery import discovery_routes

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local:8080", tls=False)


@pytest.fixture
def wrapped_app(tmp_path: Path) -> Any:
    cfg: Config = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [DomainRecord(LOCAL.name, LOCAL.tls)])
    return SubdomainProxyMiddleware(Litestar(route_handlers=[discovery_routes], openapi_config=None))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("host.example.com", "https://host.example.com"),
        ("myhost.local:8080", "http://myhost.local:8080"),
    ],
)
async def test_discovery_reports_the_router_on_the_arriving_domain(wrapped_app: Any, host: str, expected: str) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get(f"http://{host}/.well-known/cloud-in-a-bottle")
    assert r.status_code == 200
    assert r.json() == {"router_url": expected}
    # Public, so the my. redirect page can read it cross-origin.
    assert r.headers["Access-Control-Allow-Origin"] == "*"
