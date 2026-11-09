import sqlite3

import pytest

from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.web.routes.pages.permissions_v2 import approve_permissions_v2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "return_to",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\nscript:alert(1)",
        " javascript:alert(1)",
        "data:text/html,test",
        "javascript://app.example.test/%0aalert(1)",
        "https://elsewhere.test/",
        "//elsewhere.test/",
        "///elsewhere.test/",
        "/\\elsewhere.test/",
        "https://example.test@elsewhere.test/",
        "https://[invalid/",
    ],
)
async def test_approval_omits_non_navigation_return_urls(db: sqlite3.Connection, monkeypatch, return_to: str) -> None:
    seed_domains(db, Domain("example.test", tls=True), [])
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES ('app', 'consumer', '1.0', '/tmp/consumer', 9000, 'running')"
    )
    monkeypatch.setattr("compute_space.web.routes.pages.permissions_v2.get_granted_permissions_v2", lambda *_: [])
    response = await approve_permissions_v2.fn(db, "app", "service", '"read"', return_to)
    assert response.context["redirect_url"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("return_to", [None, "/apps?tab=services", "./done", "https://app.example.test:8443/done"])
async def test_approval_keeps_local_navigation(db: sqlite3.Connection, monkeypatch, return_to: str | None) -> None:
    seed_domains(db, Domain("example.test", tls=True), [])
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES ('app', 'consumer', '1.0', '/tmp/consumer', 9000, 'running')"
    )
    monkeypatch.setattr("compute_space.web.routes.pages.permissions_v2.get_granted_permissions_v2", lambda *_: [])
    response = await approve_permissions_v2.fn(db, "app", "service", '"read"', return_to)
    assert response.context["redirect_url"] == return_to
