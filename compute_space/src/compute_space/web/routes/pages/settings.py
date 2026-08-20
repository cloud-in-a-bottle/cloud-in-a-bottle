from litestar import Router
from litestar import get
from litestar.response import Template

from compute_space.web.auth.auth import require_owner_auth


@get("/settings", guards=[require_owner_auth])
async def settings_page() -> Template:
    return Template(template_name="settings.html")


@get("/updating", guards=[require_owner_auth])
async def updating_page() -> Template:
    return Template(template_name="updating.html")


pages_settings_routes = Router(path="/", route_handlers=[settings_page, updating_page])
