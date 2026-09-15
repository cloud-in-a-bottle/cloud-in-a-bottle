from typing import Any

from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Request
from litestar.response.base import ASGIResponse

from compute_space.core.domains import host_with_request_port
from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import WEB_DIR
from compute_space.web.helpers.static import make_static_url
from compute_space.web.helpers.zone import zone_for_request

RETRY_SECONDS = 3
_JINJA_ENV = Environment(loader=FileSystemLoader(str(WEB_DIR / "templates")), autoescape=True)
_JINJA_ENV.globals["static_url"] = make_static_url(STATIC_DIR)


def app_starting_response(request: Request[Any, Any, Any]) -> ASGIResponse:
    headers = {"Cache-Control": "no-store", "Retry-After": str(RETRY_SECONDS), "Referrer-Policy": "no-referrer"}
    fetch_mode = request.headers.get("sec-fetch-mode")
    # Browsers identify document/frame loads directly. Plain-HTTP browsers may
    # omit Fetch Metadata; their usual Accept header starts with unqualified HTML.
    is_navigation = (
        fetch_mode == "navigate"
        if fetch_mode is not None
        else request.headers.get("accept", "").split(",", 1)[0].strip().lower() == "text/html"
    )
    if request.method != "GET" or not is_navigation:
        return ASGIResponse(
            body=b"" if request.method == "HEAD" else b"Your app is coming up. Please try again shortly.",
            status_code=503,
            media_type="text/plain",
            headers=headers,
        )

    zone = zone_for_request(request)
    router_host = host_with_request_port(zone.name_no_port, request.url.netloc)
    router_url = f"{zone.scheme}://{router_host}"

    body = _JINJA_ENV.get_template("app_starting.html").render(
        router_url=router_url,
        retry_seconds=RETRY_SECONDS,
    )
    return ASGIResponse(body=body.encode(), status_code=503, media_type="text/html", headers=headers)
