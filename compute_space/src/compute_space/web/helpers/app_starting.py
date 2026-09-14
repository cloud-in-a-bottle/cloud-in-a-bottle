import re
from pathlib import Path
from typing import Any

from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Request
from litestar.datastructures.headers import MediaTypeHeader
from litestar.response.base import ASGIResponse

from compute_space.core.domains import Domain
from compute_space.core.domains import host_with_request_port
from compute_space.web.helpers.static import make_static_url

RETRY_SECONDS = 3
_WEB_DIR = Path(__file__).resolve().parent.parent
_JINJA_ENV = Environment(loader=FileSystemLoader(str(_WEB_DIR / "templates")), autoescape=True)
_STATIC_URL = make_static_url(_WEB_DIR / "static")
_QUALITY_VALUE = re.compile(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)")


def _is_html_navigation(request: Request[Any, Any, Any]) -> bool:
    if request.method != "GET" or request.headers.get("sec-fetch-dest", "") not in ("", "document", "iframe", "frame"):
        return False
    # The most specific range determines each representation's quality, including
    # explicit q=0 exclusions. Litestar's Accept.best_match ignores those exclusions.
    try:
        values = ",".join(request.headers.getall("accept", [])).lower().split(",")
        for value in values:
            for parameter in value.split(";")[1:]:
                name, _, quality = parameter.strip().partition("=")
                if name.strip() == "q":
                    if name != "q" or _QUALITY_VALUE.fullmatch(quality) is None:
                        return False
        accepted = [MediaTypeHeader(value) for value in values]

        def preference(media_type: str) -> tuple[int, int]:
            provided = MediaTypeHeader(media_type)
            matches = [item for item in accepted if item.match(provided)]
            if not matches:
                return (0, -1)
            best = max(matches, key=lambda item: (item.priority[1], float(item.params.get("q", "1"))))
            # HTTP weights allow three decimals; MediaTypeHeader.priority truncates to two.
            return (round(float(best.params.get("q", "1")) * 1000), best.priority[1])

        html = preference("text/html; charset=utf-8")
        return html[0] > 0 and html > max(preference("text/plain; charset=utf-8"), preference("application/json"))
    except (ValueError, OverflowError):
        return False


def app_starting_response(
    request: Request[Any, Any, Any], *, app_name: str, zone: Domain, is_owner: bool
) -> ASGIResponse:
    headers = {"Cache-Control": "no-store", "Retry-After": str(RETRY_SECONDS), "Referrer-Policy": "no-referrer"}
    if not _is_html_navigation(request):
        return ASGIResponse(
            body=b"" if request.method == "HEAD" else b"Your app is coming up. Please try again shortly.",
            status_code=503,
            media_type="text/plain",
            headers=headers,
        )

    router_host = host_with_request_port(zone.name_no_port, request.url.netloc)
    router_url = f"{zone.scheme}://{router_host}"

    def static_url(filename: str) -> str:
        # Relative assets would be routed back into the starting app.
        return router_url + _STATIC_URL(filename)

    body = _JINJA_ENV.get_template("app_starting.html").render(
        app_name=app_name,
        details_url=f"{router_url}/app_detail/{app_name}" if is_owner else None,
        static_url=static_url,
        retry_seconds=RETRY_SECONDS,
    )
    return ASGIResponse(body=body.encode(), status_code=503, media_type="text/html", headers=headers)
