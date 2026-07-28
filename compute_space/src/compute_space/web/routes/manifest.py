"""Router manifest shared by the live app (``app.py``) and the OpenAPI schema
generator (``openapi.py``). What each router contributes to the schema is
decided per-endpoint via ``include_in_schema`` on the handlers/routers, so
the live ``/schema`` and the committed ``openapi.yaml`` cannot diverge."""

from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes
from compute_space.web.routes.api.identity import identity_routes
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_routes
from compute_space.web.routes.api.services_v2 import api_services_v2_routes
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.api.system import system_routes
from compute_space.web.routes.docs import docs_routes
from compute_space.web.routes.pages.apps import pages_apps_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_routes
from compute_space.web.routes.pages.settings import pages_settings_routes
from compute_space.web.routes.pages.system import pages_system_routes
from compute_space.web.routes.services_v2 import services_v2_routes

ALL_ROUTERS = [
    api_apps_routes,
    api_archive_backend_routes,
    api_permissions_v2_routes,
    api_services_v2_routes,
    api_settings_routes,
    system_routes,
    identity_routes,
    docs_routes,
    pages_apps_routes,
    pages_login_routes,
    pages_permissions_v2_routes,
    pages_settings_routes,
    pages_system_routes,
    services_v2_routes,
]
