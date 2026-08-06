from litestar import Router
from litestar import get
from litestar.response import Template

from compute_space.web.auth.auth import require_owner_auth


@get("/settings", guards=[require_owner_auth])
async def settings_page() -> Template:
    return Template(template_name="settings.html")


# excluded due to user-facing html page rather than api
pages_settings_routes = Router(path="/", route_handlers=[settings_page], include_in_schema=False)
