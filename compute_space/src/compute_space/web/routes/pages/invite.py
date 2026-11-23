import asyncio
import sqlite3
from typing import Any

import bcrypt
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.params import FromPath
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.auth.invites import create_member
from compute_space.core.auth.invites import is_open_invite
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.helpers.zone import zone_for_request

MIN_PASSWORD_LENGTH = 8


def _closed_invite() -> Template:
    return Template(template_name="invite.html", context={"closed": True}, status_code=404)


@get("/invite/{token:str}")
async def invite_get(token: FromPath[str], db: NamedDependency[sqlite3.Connection]) -> Response[Any]:
    if not is_open_invite(db, token):
        return _closed_invite()
    return Template(template_name="invite.html")


@post("/invite/{token:str}", status_code=200)
async def invite_post(
    request: Request[Any, Any, Any], token: FromPath[str], db: NamedDependency[sqlite3.Connection]
) -> Response[Any]:
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")

    def _error(msg: str) -> Template:
        return Template(template_name="invite.html", context={"error": msg, "username": username})

    if not is_open_invite(db, token):
        return _closed_invite()
    if error := validate_owner_username(username):
        return _error(error)
    if len(password) < MIN_PASSWORD_LENGTH:
        return _error(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        return _error("That username is taken.")

    password_hash = (await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())).decode()
    try:
        user_id = create_member(db, token, username, password_hash)
    except sqlite3.IntegrityError:
        db.rollback()
        return _error("That username is taken.")
    if user_id is None:
        db.rollback()
        return _closed_invite()
    session_token = create_session(user_id, db)
    db.commit()

    response = Redirect(path="/")
    response.set_cookie(build_session_cookie(session_token, zone_for_request(request)))
    return response


pages_invite_routes = Router(path="/", route_handlers=[invite_get, invite_post])
