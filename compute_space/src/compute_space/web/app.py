import atexit
import sqlite3
from pathlib import Path
from typing import Any

from litestar import HttpMethod
from litestar import Litestar
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import route
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions.responses import create_exception_response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.types import ASGIApp

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.config import get_config
from compute_space.config import provide_config
from compute_space.core import archive_backend
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.identity import load_identity_keys
from compute_space.core.git_ops import get_github_source_url
from compute_space.core.image_pruner import start_image_pruner
from compute_space.core.logging import logger
from compute_space.core.startup import check_app_status
from compute_space.core.startup import retry_pending_default_apps
from compute_space.core.storage import start_storage_guard
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.web.auth.auth import login_required_redirect
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
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


def _make_static_url(static_dir: Path) -> Any:
    """Build a Jinja ``static_url`` global that appends ``?v=<mtime>`` for cache-busting.

    Browsers aggressively cache static JS/CSS, so a deploy that ships a new
    template + JS would otherwise leave returning visitors running stale JS
    against new HTML.  Appending the file's mtime forces a fresh fetch.
    """

    def static_url(filename: str) -> str:
        base = f"/static/{filename}"
        try:
            mtime = int((static_dir / filename).stat().st_mtime)
        except OSError:
            return base
        return f"{base}?v={mtime}"

    return static_url


def _template_globals(config: Config, static_dir: Path) -> dict[str, Any]:
    zone_domain = config.zone_domain
    zone_name = zone_domain.split(".")[0] if zone_domain else None

    def app_url(app_name: str) -> str:
        proto = "https" if config.tls_enabled else "http"
        return f"{proto}://{app_name}.{zone_domain}/"

    def owner_name() -> str | None:
        """The owner's configured username, or None if unset / pre-setup.

        Read live (not cached) because the owner can change it from Settings at
        any time. Exposed as a callable template global so templates can prefer
        it over ``zone_name`` for headings.
        """
        try:
            db = get_db()
            try:
                return read_owner_username(db)
            finally:
                db.close()
        except Exception:
            # Never let a heading lookup break page rendering (e.g. pre-init DB).
            logger.exception("failed to read owner username for template")
            return None

    # Resolved once at startup: the running checkout's branch/fork doesn't change
    # without a restart, and a "view source" link needn't be recomputed per request.
    source_url = get_github_source_url(OPENHOST_PROJECT_DIR)

    return {
        "zone_name": zone_name,
        "zone_domain": zone_domain,
        "app_url": app_url,
        "owner_name": owner_name,
        "static_url": _make_static_url(static_dir),
        "source_url": source_url,
    }


def _full_app_bootstrap(config: Config) -> None:
    """Side-effects required before the full app handles requests.

    DB / keys / logging are already initialized in ``start.py``; this only covers the
    heavier setup steps that don't make sense for the setup-only app.
    """
    db = sqlite3.connect(config.db_path)
    try:
        archive_backend.attach_on_startup(config, db)
    finally:
        db.close()
    check_app_status(config)
    load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
    start_image_pruner(config)
    retry_pending_default_apps(config)


@route("/setup", http_method=[HttpMethod.GET, HttpMethod.POST], status_code=403, sync_to_thread=False)
def setup_already_done() -> Response[str]:
    return Response(
        content="This instance has already been set up.",
        status_code=403,
        media_type=MediaType.TEXT,
    )


def _login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /login; JSON clients get 401.

    websocket-type requests should never get here - they start as HTTP requests with `Upgrade: websocket`, and should fail then.
    """
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    return login_required_redirect(request)


def _log_unhandled_exception(request: Request[Any, Any, Any], exc: Exception) -> Response[Any]:
    """Log a traceback for any exception not caught by a more specific handler.

    Litestar's default behaviour serialises the exception into a 500 JSON response
    but doesn't log it, so genuine bugs disappear silently.  Stay quiet for
    intentional 4xx HTTPException responses; log everything else (including 5xx
    HTTPException like NoRouteMatchFoundException which wraps real bugs).
    """
    status_code = getattr(exc, "status_code", 500)
    if not isinstance(exc, HTTPException) or status_code >= 500:
        logger.opt(exception=exc).error("Unhandled exception in {} {}", request.method, request.url.path)
    return create_exception_response(request=request, exc=exc)


def _reject_app_subdomain_requests(request: Request[Any, Any, Any]) -> Response[Any] | None:
    """Defense-in-depth: refuse any request whose Host is an app subdomain.

    App-subdomain traffic is supposed to be intercepted by SubdomainProxyMiddleware
    (outer ASGI) before Litestar ever sees it.  If a request reaches Litestar with
    a ``*.zone_domain`` Host — e.g. the middleware was bypassed in a test or a
    deployment variant — refuse it rather than accidentally serve a router route
    (like /health) under the app's hostname.
    """
    host = request.url.netloc.split(":", 1)[0]
    zone = get_config().zone_domain
    if zone and host.endswith("." + zone):
        return Response(content=None, status_code=404)
    return None


def create_app(config: Config) -> ASGIApp:
    """Build the full router ASGI app.  The returned app is the Litestar app wrapped
    in ``SubdomainProxyMiddleware`` so app-subdomain requests are diverted to backend
    containers before Litestar attempts any routing.  Caller must have already
    initialized DB, keys, logging, and config."""
    _full_app_bootstrap(config)

    web_dir = Path(__file__).parent
    static_dir = web_dir / "static"
    template_dir = web_dir / "templates"

    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
    )

    def _install_template_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(config, static_dir))

    static_router = create_static_files_router(path="/static", directories=[static_dir])

    atexit.register(cleanup_terminal)

    litestar_app = Litestar(
        route_handlers=[
            static_router,
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
            setup_already_done,
        ],
        template_config=template_config,
        before_request=_reject_app_subdomain_requests,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        exception_handlers={
            NotAuthorizedException: _login_required_redirect,
            Exception: _log_unhandled_exception,
        },
        on_startup=[_install_template_globals],
    )
    return SubdomainProxyMiddleware(litestar_app)
