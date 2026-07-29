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

from compute_space.config import Config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import revoke_session
from compute_space.core.auth.auth import validate_password
from compute_space.web.auth.auth import authenticate
from compute_space.web.auth.auth import require_same_origin
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie


def _validated_next(next_url: str, zone_domain: str) -> str | None:
    """Return ``next_url`` if it's a safe post-login redirect target, else None.

    Accepts either a same-zone absolute URL (router or app subdomain) or a path-relative URL.
    Anything else is rejected so a hostile ``?next=`` can't bounce the operator off to a phishing page.
    """
    if not next_url:
        return None
    parsed = urlparse(next_url)
    if not parsed.scheme and not parsed.netloc:
        return next_url
    if parsed.netloc == zone_domain or parsed.netloc.endswith("." + zone_domain):
        return next_url
    return None


@get("/login")
async def login_get(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[Any]:
    next_param = request.query_params.get("next", "")
    if authenticate(request, db=db) is not None:
        return Redirect(path=_validated_next(next_param, config.zone_domain) or "/")
    return Template(template_name="login.html", context={"next": next_param})


@post("/login", status_code=200)
async def login_post(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[Any]:
    form = await request.form()
    password = form.get("password")
    next_url = form.get("next", "")
    if password is None or not (user_id := validate_password(password, db)):
        return Template(template_name="login.html", context={"error": "Invalid password", "next": next_url})

    session_token = create_session(user_id, db)
    db.commit()

    dest = _validated_next(next_url, config.zone_domain) or "/"
    response = Redirect(path=dest)
    # cookie domain is zone_domain_no_port, ie `host.example.com` (no port); this will cover also `app.host.example.com`
    response.set_cookie(build_session_cookie(session_token, cookie_domain=config.zone_domain_no_port))
    return response


# /logout has no owner-auth guard (it must work for any session state), so guard it against
# cross-site POSTs to prevent forced-logout CSRF.
@post("/logout", status_code=200, guards=[require_same_origin])
async def logout(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[Any]:
    if session_token := request.cookies.get(SESSION_COOKIE_NAME):
        revoke_session(session_token, db)
        db.commit()

    response: Response[Any] = Redirect(path="/login")
    response.set_cookie(clear_session_cookie(cookie_domain=config.zone_domain_no_port))
    return response


pages_login_routes = Router(path="/", route_handlers=[login_get, login_post, logout])
