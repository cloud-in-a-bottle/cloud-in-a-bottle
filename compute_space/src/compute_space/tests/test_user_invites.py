"""User invites end to end: owner API, invite signup, login, and what the proxy lets a user reach."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bcrypt
import httpx
import pytest
import pytest_asyncio
from litestar import Litestar
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.db import provide_db
from compute_space.tests._litestar_helpers import BROWSER_FETCH_METADATA
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_http_scope
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web import app as web_app
from compute_space.web.app import _template_globals
from compute_space.web.auth.auth import verify_app_auth
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.invites import api_invites_routes
from compute_space.web.routes.api.settings import set_owner_username
from compute_space.web.routes.pages import login
from compute_space.web.routes.pages.apps import dashboard
from compute_space.web.routes.pages.invite import pages_invite_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.routes.pages.settings import settings_page

ZONE = "host.example.com"
_JSON_ONLY = {"Accept": "application/json"}
ROUTER = f"https://{ZONE}"


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        names = ("X-OpenHost-Is-Owner", "X-OpenHost-User-Id", "X-OpenHost-Username")
        body = json.dumps({k: self.headers.get(k) for k in names}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def backend_ports() -> Iterator[list[int]]:
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _Echo) for _ in range(3)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield [srv.server_address[1] for srv in servers]
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _seed_app(cfg: Config, name: str, port: int, public_paths: list[str], user_paths: list[str]) -> str:
    app_id = new_app_id()
    with closing(open_db(cfg)) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, public_paths, user_paths)"
            " VALUES (?, ?, '1', '/tmp/x', ?, 'running', ?, ?)",
            (app_id, name, port, json.dumps(public_paths), json.dumps(user_paths)),
        )
        db.commit()
    return app_id


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return _make_test_config(tmp_path, zone_domain=ZONE, tls_enabled=True)


@pytest.fixture
def apps(cfg: Config, backend_ports: list[int]) -> dict[str, str]:
    shared, private, public = backend_ports
    return {
        # Users get /view and the public /pub; /admin stays owner-only.
        "shared": _seed_app(cfg, "shared", shared, public_paths=["/pub"], user_paths=["/view"]),
        "private": _seed_app(cfg, "private", private, public_paths=[], user_paths=["/"]),
        # Fully public, and declares no user_paths, so it can't be shared.
        "public": _seed_app(cfg, "public", public, public_paths=["/"], user_paths=[]),
    }


@pytest_asyncio.fixture
async def client(cfg: Config, apps: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    web_dir = Path(web_app.__file__).parent
    litestar_app = Litestar(
        route_handlers=[
            api_invites_routes,
            pages_invite_routes,
            pages_login_routes,
            dashboard,
            settings_page,
            set_owner_username,
        ],
        template_config=TemplateConfig(directory=web_dir / "templates", engine=JinjaTemplateEngine),
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
        exception_handlers={NotAuthorizedException: web_app._auth_required_handler},
        openapi_config=None,
    )
    assert isinstance(litestar_app.template_engine, JinjaTemplateEngine)
    litestar_app.template_engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=SubdomainProxyMiddleware(litestar_app), client=("127.0.0.1", 1)),
        base_url=ROUTER,
        headers=BROWSER_FETCH_METADATA,
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_bcrypt_and_fresh_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda: gensalt(4))
    monkeypatch.setattr(login, "_failed_logins", login.RateLimiter(limit=20, window_seconds=60))


async def _as_owner(cfg: Config, c: httpx.AsyncClient) -> None:
    c.cookies.update(auth_cookie(cfg))


async def _create_invite(
    c: httpx.AsyncClient, app_ids: list[str], max_uses: int = 5, expiry_hours: float = 1
) -> tuple[int, str]:
    r = await c.post(
        "/api/invites",
        json={"name": "hack", "app_ids": app_ids, "max_uses": max_uses, "expiry_hours": expiry_hours},
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith(f"{ROUTER}/invite/")
    return r.json()["id"], r.json()["url"].removeprefix(ROUTER)


async def _sign_up(c: httpx.AsyncClient, path: str, username: str, password: str = "password1") -> httpx.Response:
    c.cookies.clear()
    return await c.post(path, data={"username": username, "password": password})


async def _echo(c: httpx.AsyncClient, app: str, path: str = "/view") -> httpx.Response:
    return await c.get(f"https://{app}.{ZONE}{path}")


def _links(html: str) -> list[str]:
    """The app links on a user's home page."""
    return re.findall(r'<li><a href="([^"]+)">', html)


def _seen(is_owner: str | None, user_id: object, username: str | None) -> dict[str, str | None]:
    uid = None if user_id is None else str(user_id)
    return {"X-OpenHost-Is-Owner": is_owner, "X-OpenHost-User-Id": uid, "X-OpenHost-Username": username}


@pytest.mark.asyncio
async def test_user_reaches_only_shared_apps(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    _, path = await _create_invite(client, [apps["shared"]])
    with closing(open_db(cfg)) as db:
        owner_id = db.execute("SELECT user_id FROM users WHERE username = 'owner'").fetchone()[0]
    for p in ("/view", "/pub", "/admin"):
        assert (await _echo(client, "shared", p)).json() == _seen("true", owner_id, "owner")

    assert (await client.get(path)).status_code == 200
    r = await _sign_up(client, path, "alice")
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in client.cookies

    home = await client.get("/")
    assert home.status_code == 200 and "Hi, alice" in home.text
    assert _links(home.text) == ["https://shared.host.example.com/view"]
    assert "private" not in home.text

    with closing(open_db(cfg)) as db:
        alice_id = db.execute("SELECT user_id FROM users WHERE username = 'alice'").fetchone()[0]
    assert (await _echo(client, "shared")).json() == _seen(None, alice_id, "alice")
    # Public paths of a shared app identify the user too.
    assert (await _echo(client, "shared", "/pub")).json() == _seen(None, alice_id, "alice")
    # Routes outside user_paths stay owner-only: the user is treated like a stranger (this app has public paths,
    # so that means a login redirect rather than a 404).
    assert (await _echo(client, "shared", "/admin")).status_code == 302
    assert (await _echo(client, "shared", "/view/../admin")).status_code == 302
    assert (await _echo(client, "private")).status_code == 404
    # A public app serves a user who wasn't invited to it anonymously.
    assert (await _echo(client, "public")).json() == _seen(None, None, None)


@pytest.mark.asyncio
async def test_user_gets_nothing_owner_only(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    _, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")
    json_only = {"Accept": "application/json"}
    assert (await client.get("/api/invites", headers=json_only)).status_code == 401
    assert (await client.delete("/api/users/1", headers=json_only)).status_code == 401
    assert (await client.get("/settings")).status_code == 302


@pytest.mark.asyncio
async def test_user_cookie_is_not_app_auth(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    """An app's browser code running for a user must not get to call services as that app."""
    await _as_owner(cfg, client)
    _, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")
    cookie = f"{SESSION_COOKIE_NAME}={client.cookies[SESSION_COOKIE_NAME]}"
    scope = make_http_scope("GET", "/x", host=ZONE, cookie=cookie, headers={"Origin": f"https://shared.{ZONE}"})
    with pytest.raises(NotAuthorizedException):
        verify_app_auth(ASGIConnection(scope))


@pytest.mark.asyncio
async def test_invite_limits(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    _, once = await _create_invite(client, [apps["shared"]], max_uses=1)
    closed_id, closed = await _create_invite(client, [apps["shared"]])
    assert (await client.post(f"/api/invites/{closed_id}/close")).status_code == 204
    _, expired = await _create_invite(client, [apps["shared"]])
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00+00:00' WHERE name = 'hack' AND uses_left = 5")
        db.commit()

    assert (await _sign_up(client, once, "alice")).status_code == 302
    for path in (once, closed, expired, "/invite/not-a-token"):
        assert (await _sign_up(client, path, "bob")).status_code == 404
        r = await client.get(path)
        assert r.status_code == 404 and "Invite closed" in r.text


@pytest.mark.asyncio
async def test_signup_validation(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    _, path = await _create_invite(client, [apps["shared"]], max_uses=1)
    assert "taken" in (await _sign_up(client, path, "owner")).text
    assert "at least 8" in (await _sign_up(client, path, "alice", password="short")).text
    assert "Username must" in (await _sign_up(client, path, "Not Valid")).text
    # Failed attempts don't spend the invite's one use.
    assert (await _sign_up(client, path, "alice")).status_code == 302


@pytest.mark.asyncio
async def test_owner_cannot_take_a_users_username(
    cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]
) -> None:
    owner = auth_cookie(cfg)
    client.cookies.update(owner)
    _, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")
    client.cookies.clear()
    client.cookies.update(owner)
    r = await client.post("/api/settings/owner_username", json={"username": "alice"})
    assert r.status_code == 400 and "taken" in r.text


@pytest.mark.asyncio
async def test_create_invite_validation(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    for body in (
        {"app_ids": []},
        {"app_ids": ["nope"]},
        {"app_ids": [apps["public"]]},
        {"app_ids": [apps["shared"]], "max_uses": 0},
        {"app_ids": [apps["shared"]], "expiry_hours": 0},
    ):
        r = await client.post("/api/invites", json={"name": "x", "max_uses": 1, "expiry_hours": 1, **body})
        assert r.status_code == 400, body


@pytest.mark.asyncio
async def test_deleting_invite_or_user_revokes_access(
    cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]
) -> None:
    owner = auth_cookie(cfg)
    client.cookies.update(owner)
    invite_id, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")
    alice = dict(client.cookies)
    await _sign_up(client, path, "bob")
    bob = dict(client.cookies)

    client.cookies.clear()
    client.cookies.update(owner)
    listed = (await client.get("/api/invites")).json()
    assert [m["username"] for m in listed[0]["users"]] == ["alice", "bob"]
    assert listed[0]["uses_left"] == 3 and listed[0]["apps"] == ["shared"]

    bob_id = listed[0]["users"][1]["user_id"]
    assert (await client.delete(f"/api/users/{bob_id}")).status_code == 204
    client.cookies.clear()
    client.cookies.update(bob)
    assert (await _echo(client, "shared")).status_code == 302

    client.cookies.clear()
    client.cookies.update(owner)
    assert (await client.delete(f"/api/invites/{invite_id}")).status_code == 204
    client.cookies.clear()
    client.cookies.update(alice)
    assert (await _echo(client, "shared")).status_code == 302
    with closing(open_db(cfg)) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions s JOIN users u USING (user_id)").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_removing_app_unshares_it(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    await _create_invite(client, [apps["shared"], apps["private"]])
    with closing(open_db(cfg)) as db:
        db.execute("DELETE FROM apps WHERE name = 'private'")
        db.commit()
    assert (await client.get("/api/invites")).json()[0]["apps"] == ["shared"]


@pytest.mark.asyncio
async def test_login_by_username(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    await _as_owner(cfg, client)
    _, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")

    client.cookies.clear()
    r = await client.post("/login", data={"username": "alice", "password": "password1"})
    assert r.status_code == 302
    assert "Hi, alice" in (await client.get("/")).text
    assert (await client.post("/logout")).status_code == 302
    assert (await client.get("/")).status_code == 302

    client.cookies.clear()
    assert "Invalid" in (await client.post("/login", data={"username": "alice", "password": "nope"})).text
    # The owner signs in by username too; a blank one is refused before any password check.
    r = await client.post("/login", data={"username": "", "password": "testpass1"})
    assert r.status_code == 200 and "Enter your username" in r.text
    r = await client.post("/login", data={"username": "owner", "password": "testpass1"})
    assert r.status_code == 302
    assert "Hi," not in (await client.get("/")).text


@pytest.mark.asyncio
async def test_failed_logins_are_rate_limited(cfg: Config, client: httpx.AsyncClient) -> None:
    auth_cookie(cfg)
    for _ in range(20):
        assert "Invalid" in (await client.post("/login", data={"username": "owner", "password": "wrong"})).text
    r = await client.post("/login", data={"username": "owner", "password": "testpass1"})
    assert "Too many attempts" in r.text and SESSION_COOKIE_NAME not in r.cookies


def test_single_owner_is_enforced(cfg: Config) -> None:
    auth_cookie(cfg)
    with closing(open_db(cfg)) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO users (username, password_hash) VALUES ('second', 'x')")


@pytest.mark.asyncio
async def test_assign_user_on_app_page(cfg: Config, client: httpx.AsyncClient, apps: dict[str, str]) -> None:
    owner = auth_cookie(cfg)
    client.cookies.update(owner)
    _, path = await _create_invite(client, [apps["shared"]])
    await _sign_up(client, path, "alice")
    alice = dict(client.cookies)
    with closing(open_db(cfg)) as db:
        alice_id = db.execute("SELECT user_id FROM users WHERE username = 'alice'").fetchone()[0]

    async def as_(cookies: dict[str, str]) -> None:
        client.cookies.clear()
        client.cookies.update(cookies)

    async def assign(app: str, user_ids: list[int]) -> httpx.Response:
        await as_(owner)
        return await client.put(f"/api/apps/{apps[app]}/users", json={"user_ids": user_ids})

    await as_(alice)
    assert (await _echo(client, "private")).status_code == 404

    r = await assign("private", [alice_id])
    assert r.status_code == 200
    assert [(u["username"], u["via_invite"], u["assigned"]) for u in r.json()] == [("alice", False, True)]
    await as_(alice)
    assert (await _echo(client, "private")).json() == _seen(None, alice_id, "alice")
    home = (await client.get("/")).text
    assert _links(home) == ["https://private.host.example.com/", "https://shared.host.example.com/view"]

    # Unassigning removes direct access; access through the invite can't be taken away here.
    assert (await assign("private", [])).json()[0]["assigned"] is False
    assert (await assign("shared", [])).json()[0]["via_invite"] is True
    await as_(alice)
    assert (await _echo(client, "private")).status_code == 404
    assert (await _echo(client, "shared")).status_code == 200

    with closing(open_db(cfg)) as db:
        owner_id = db.execute("SELECT user_id FROM users WHERE invite_id IS NULL").fetchone()[0]
    assert (await assign("private", [owner_id])).status_code == 400
    assert (await assign("private", [10**6])).status_code == 400
    assert (await assign("public", [alice_id])).status_code == 400
    await as_(owner)
    assert (await client.put("/api/apps/nope/users", json={"user_ids": []})).status_code == 404
    await as_(alice)
    r = await client.put(f"/api/apps/{apps['private']}/users", json={"user_ids": [alice_id]}, headers=_JSON_ONLY)
    assert r.status_code == 401

    # Deleting the user or the app drops the assignment with it.
    await assign("private", [alice_id])
    await as_(owner)
    assert (await client.delete(f"/api/users/{alice_id}")).status_code == 204
    with closing(open_db(cfg)) as db:
        assert db.execute("SELECT COUNT(*) FROM user_apps").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_narrow_pages_follow_the_owners_theme(cfg: Config, client: httpx.AsyncClient) -> None:
    with closing(open_db(cfg)) as db:
        db.execute("INSERT INTO settings (key, value) VALUES ('theme', '{\"style\": \"dark\"}')")
        db.commit()
    page = (await client.get("/login")).text
    assert '<html lang="en" data-style="dark">' in page and "themes.css" in page
