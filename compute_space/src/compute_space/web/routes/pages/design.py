import attr
from litestar import Router
from litestar import get
from litestar.response import Template

from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class DemoApp:
    """Stand-in for an apps-table row, so the gallery can show states (error,
    long name, alias) that a live instance rarely has all at once."""

    app_id: str
    name: str
    status: str
    manifest_name: str = ""


_DEMO_APPS = (
    DemoApp(app_id="demo-1", name="health-dashboard", status="running"),
    DemoApp(app_id="demo-2", name="claude-workbench", status="stopped", manifest_name="workbench"),
    DemoApp(app_id="demo-3", name="a-very-long-application-name-that-should-truncate", status="error"),
    DemoApp(app_id="demo-4", name="openhost-minecraft-server", status="building"),
)


@get("/design", guards=[require_owner_auth])
async def design() -> Template:
    return Template(template_name="design.html", context={"demo_apps": _DEMO_APPS})


pages_design_routes = Router(path="/", route_handlers=[design])
