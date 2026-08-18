import atexit
from contextlib import closing
from pathlib import Path
from typing import Any

from jinja2 import pass_context
from jinja2.runtime import Context
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import PermissionDeniedException
from litestar.exceptions.responses import create_exception_response
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import Redirect
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.types import ASGIApp

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core import archive_backend
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.identity import load_identity_keys
from compute_space.core.domains import Domain
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.git_ops import SOURCE_URL
from compute_space.core.image_pruner import start_image_pruner
from compute_space.core.logging import logger
from compute_space.core.memory_guard import ensure_memory_guard
from compute_space.core.org_rename import reconcile_app_repo_urls
from compute_space.core.process_stream import cleanup_all as cleanup_process_streams
from compute_space.core.startup import check_app_status
from compute_space.core.startup import retry_pending_default_apps
from compute_space.core.storage import start_storage_guard
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.web.auth.auth import auth_required_response
from compute_space.web.helpers.static import make_static_url
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes
from compute_space.web.routes.api.domains import api_domains_routes
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


def _template_globals(config: Config, static_dir: Path) -> dict[str, Any]:
    def primary() -> Domain | None:
        with closing(get_db()) as db:
            return primary_domain_or_none(db)

    @pass_context
    def app_url(context: Context, app_name: str) -> str:
        """Absolute URL to an app, on the domain the current request arrived on.
        Falls back to the live primary when the render had no proxied request."""
        request = context.get("request")
        stashed = request.scope.get(ZONE_SCOPE_KEY) if request is not None else None
        zone = stashed if isinstance(stashed, Domain) else primary()
        if zone is None:
            return f"//{app_name}/"  # pre-seed only: no primary yet, emit a scheme/host-relative link
        return f"{zone.scheme}://{app_name}.{zone.name}/"

    def zone_domain() -> str:
        p = primary()
        return p.name if p else ""

    def zone_name() -> str | None:
        zd = zone_domain()
        return zd.split(".")[0] if zd else None

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

    return {
        "zone_name": zone_name,
        "zone_domain": zone_domain,
        "app_url": app_url,
        "owner_name": owner_name,
        "static_url": make_static_url(static_dir),
        "source_url": SOURCE_URL,
    }


def _full_app_bootstrap(config: Config) -> None:
    """Side-effects required before the full app handles requests.

    DB / keys / logging are already initialized in ``start.py``; this only covers the
    heavier setup steps that don't make sense for the setup-only app.
    """
    db = get_db()  # row_factory=Row; the archive derives its per-zone volume from the domains store
    try:
        archive_backend.attach_on_startup(config, db)
        # Move any app repo URLs still pointing at the pre-rename GitHub owner.
        # Idempotent, and inert until org_rename.ORG_RENAME_COMPLETE is flipped;
        # see that module for why this is a per-boot reconcile rather than a
        # versioned migration.
        reconcile_app_repo_urls(db)
    finally:
        db.close()
    check_app_status(config)
    load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
    start_image_pruner(config)
    ensure_memory_guard(config)
    retry_pending_default_apps(config)
    # The DB `domains` table is the source of truth.  Seed it once (+ claim token) from
    # first_boot.toml; everything reads the primary live from the DB thereafter.
    seed_first_boot(config)


@get("/setup", sync_to_thread=False)
def setup_already_done_get() -> Response[None]:
    """The claim link (``/setup?claim=...``) printed by ``openhost up`` should keep working after setup,
    so redirect instead of 403 — the dashboard guard bounces to /login unless the owner is signed in."""
    return Redirect(path="/")


@post("/setup", sync_to_thread=False, raises=[PermissionDeniedException])
def setup_already_done_post() -> None:
    raise PermissionDeniedException(detail="This instance has already been set up.")


def _auth_required_handler(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler for an unauthorized request.

    JSON clients get 401.  HTML clients get a /login redirect for navigational GET/HEAD, but a 403 for
    unsafe methods — a 302→/login on a POST/PUT/PATCH/DELETE is lossy (the browser follows it as a
    bodyless GET and the target answers 405), so ``auth_required_response`` refuses those honestly.

    websocket-type requests should never get here - they start as HTTP requests with `Upgrade: websocket`, and should fail then.
    """
    if "application/json" in request.headers.get("Accept", "") or (exc.extra and "authorize_url" in exc.extra):
        content: dict[str, Any] = {"error": exc.detail}
        if exc.extra:
            content["extra"] = exc.extra
        return Response(content=content, status_code=401)

    return auth_required_response(request)


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
    an app-subdomain Host of any configured domain — e.g. the middleware was
    bypassed in a test or a deployment variant — refuse it rather than accidentally
    serve a router route (like /health) under the app's hostname.
    """
    netloc = request.url.netloc
    stashed = request.scope.get(ZONE_SCOPE_KEY)
    if isinstance(stashed, Domain):
        matched: Domain | None = stashed
    else:
        with closing(get_db()) as db:
            matched = Domain.match(db, netloc)
    if matched is not None and matched.is_app_subdomain(netloc):
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
        # Same reasoning as setup_app: the engine is the one configured below,
        # and skipping the globals would 500 on the first template render
        # rather than here.
        assert isinstance(engine, JinjaTemplateEngine), f"expected a Jinja engine, got {type(engine)}"
        engine.engine.globals.update(_template_globals(config, static_dir))

    static_router = create_static_files_router(path="/static", directories=[static_dir])

    atexit.register(cleanup_terminal)
    atexit.register(cleanup_process_streams)

    litestar_app = Litestar(
        route_handlers=[
            static_router,
            api_apps_routes,
            api_archive_backend_routes,
            api_domains_routes,
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
            setup_already_done_get,
            setup_already_done_post,
        ],
        template_config=template_config,
        before_request=_reject_app_subdomain_requests,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        exception_handlers={
            NotAuthorizedException: _auth_required_handler,
            Exception: _log_unhandled_exception,
        },
        on_startup=[_install_template_globals],
    )
    return SubdomainProxyMiddleware(litestar_app)
