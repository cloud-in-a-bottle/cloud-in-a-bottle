from litestar.datastructures import Cookie

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import SESSION_TTL_SECONDS
from compute_space.core.domains import Domain


def clear_session_cookie(zone: Domain) -> Cookie:
    """Build a cookie that overwrites the existing session cookie with a zero max-age,
    scoped to the domain the request arrived on."""
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value="",
        domain=zone.name_no_port,
        max_age=0,
        secure=zone.tls,
        httponly=True,
        samesite="lax",
    )


def build_session_cookie(session_token: str, zone: Domain) -> Cookie:
    """Build the cookie to set after login, scoped to the domain the request arrived on.

    Scoping to ``zone.name_no_port`` (e.g. ``host.example.com``) covers that domain plus
    its ``*.domain`` app subdomains, so one login works across the router and every app on
    that domain.  ``Secure`` is set only for https domains, so a plain-http ``.local`` login
    cookie isn't dropped by the browser.
    """
    return Cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        domain=zone.name_no_port,
        max_age=SESSION_TTL_SECONDS,
        secure=zone.tls,
        # makes it so this cookie may not be read by client-side JavaScript.
        httponly=True,
        samesite="lax",
    )
