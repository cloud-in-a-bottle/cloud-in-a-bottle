import sqlite3
from typing import Any
from urllib.parse import urlparse

from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import revoke_session
from compute_space.core.auth.auth import validate_password
from compute_space.core.domains import Domain
from compute_space.web.auth.auth import authenticate
from compute_space.web.auth.auth import require_same_origin
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie
from compute_space.web.helpers.zone import zone_for_request


def _validated_next(next_url: str, db: sqlite3.Connection) -> str | None:
    """Return ``next_url`` if it's a safe post-login redirect target, else None.

    Accepts either a path-relative URL or an absolute URL under any configured domain
    (router or app subdomain).  Anything else is rejected so a hostile ``?next=`` can't
    bounce the operator off to a phishing page.
    """
    if not next_url:
        return None
    parsed = urlparse(next_url)
    if not parsed.scheme and not parsed.netloc:
        return next_url
    if parsed.hostname is not None and Domain.match(db, parsed.hostname) is not None:
        return next_url
    return None


@get("/login")
async def login_get(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    next_param = request.query_params.get("next", "")
    if authenticate(request, db=db) is not None:
        return Redirect(path=_validated_next(next_param, db) or "/")
    return Template(template_name="login.html", context={"next": next_param})


# Also unauthenticated and state-changing (it mints a session), so guard it against
# cross-site POSTs the same way /logout is.
@post("/login", status_code=200, guards=[require_same_origin])
async def login_post(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    form = await request.form()
    password = form.get("password")
    next_url = form.get("next", "")
    if password is None or not (user_id := validate_password(password, db)):
        return Template(template_name="login.html", context={"error": "Invalid password", "next": next_url})

    session_token = create_session(user_id, db)
    db.commit()

    dest = _validated_next(next_url, db) or "/"
    response = Redirect(path=dest)
    # Scope the cookie to the domain the login arrived on (covers its `*.domain` app
    # subdomains too), so a login on `.local` stays on `.local` and one on the public
    # domain stays there — rather than always the canonical zone.
    response.set_cookie(build_session_cookie(session_token, zone_for_request(request)))
    return response


# /logout has no owner-auth guard (it must work for any session state), so guard it against
# cross-site POSTs to prevent forced-logout CSRF.
@post("/logout", status_code=200, guards=[require_same_origin])
async def logout(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
) -> Response[Any]:
    if session_token := request.cookies.get(SESSION_COOKIE_NAME):
        revoke_session(session_token, db)
        db.commit()

    response: Response[Any] = Redirect(path="/login")
    response.set_cookie(clear_session_cookie(zone_for_request(request)))
    return response


# excluded due to user-facing html page rather than api
pages_login_routes = Router(path="/", route_handlers=[login_get, login_post, logout], include_in_schema=False)
