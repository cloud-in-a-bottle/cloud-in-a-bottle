from litestar import Response
from litestar.enums import MediaType
from litestar.exceptions import NotAuthorizedException

from compute_space.web.helpers.templates import STANDALONE_JINJA_ENV


class NavigationConfirmationRequired(NotAuthorizedException):
    """A valid owner cookie on a plain-HTTP domain arrived without origin evidence; see `confirm_navigation_response`."""


def confirm_navigation_response() -> Response[str]:
    """An inert "Continue to this page" document, served in place of the requested page.

    On plain HTTP we can't tell a real navigation from an app loading the URL in an `<iframe>`. Serving owner pages
    to such frames would allow clickjacking: an app frames, say, an approval page invisibly and tricks the owner into
    clicking its button, and that click submits from the framed page's own origin, so it passes the cookie check.

    This page can't be clickjacked itself: `frame-ancestors 'none'` and `X-Frame-Options: DENY` stop browsers
    rendering it in a frame at all. In a real tab, the link reloads the URL with a same-origin Referer, which passes.

    The alternative is to serve the real page and send those anti-framing headers on owner responses. That stops
    clickjacking, but apps could still silently hit other apps' GET routes with `<img>` or `<script>`, which matters
    for apps whose GETs have side effects.
    """
    return Response(
        content=STANDALONE_JINJA_ENV.get_template("confirm_navigation.html").render(),
        status_code=401,
        media_type=MediaType.HTML,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "frame-ancestors 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Cache-Control": "no-store",
        },
    )
