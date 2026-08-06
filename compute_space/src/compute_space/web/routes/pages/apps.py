import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from litestar import Request
from litestar import Router
from litestar import get
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.params import FromPath
from litestar.params import FromQuery
from litestar.response import Template

from compute_space.config import Config
from compute_space.core.app_id import is_valid_app_name
from compute_space.core.apps import deserialize_links
from compute_space.core.apps import manifest_ungranted_permissions_v2
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.containers import get_docker_logs
from compute_space.core.domains import Domain
from compute_space.core.git_ops import UnsupportedRepoUrlError
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import get_remote_url
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.services_v2 import resolve_provider
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.helpers.zone import zone_for_request

EDIT_APP_SERVICE_URL = "github.com/imbue-openhost/claude-code-container/services/open-workspace"
EDIT_APP_VERSION_SPEC = "<1.0"

# The app catalog ships as a default app (see Config.default_apps); the Deploy
# page links to it when installed and offers to install it otherwise.
CATALOG_APP_NAME = "catalog"
CATALOG_REPO_URL = "https://github.com/imbue-openhost/openhost-catalog"


@get(["/", "/dashboard"], guards=[require_owner_auth])
async def dashboard(db: NamedDependency[sqlite3.Connection]) -> Template:
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return Template(template_name="dashboard.html", context={"apps": apps_list})


@get("/app_detail/{app_name:str}", guards=[require_owner_auth])
async def app_detail(
    request: Request[Any, Any, Any],
    app_name: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
    next: FromQuery[str] = "",
) -> Template:
    if not is_valid_app_name(app_name):
        raise HTTPException(detail="Invalid app name", status_code=400)
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        raise HTTPException(detail="App not found", status_code=404)
    app_id = app_row["app_id"]
    databases = db.execute("SELECT * FROM app_databases WHERE app_id = ?", (app_id,)).fetchall()
    port_mappings = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ? ORDER BY label",
        (app_id,),
    ).fetchall()
    services_provided = db.execute(
        "SELECT service_url, service_version FROM service_providers_v2 WHERE app_id = ? ORDER BY service_url",
        (app_id,),
    ).fetchall()
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])

    # User-facing links the app advertised in its manifest's [[links]].
    links = deserialize_links(app_row["links"])

    # Permissions: granted + manifest-declared but not yet granted. The
    # ungranted diff uses the same shared helper the update/reload gate uses,
    # so the "needs approval" set the owner sees here matches what an update
    # would refuse to apply.
    granted_records = get_all_permissions_v2(consumer_app_id=app_id)
    granted_perms = [{"service_url": p.service_url, "grant": p.grant, "scope": p.scope} for p in granted_records]
    ungranted_perms: list[dict[str, object]] = []
    manifest_raw = app_row["manifest_raw"]
    if manifest_raw:
        try:
            manifest = parse_manifest_from_string(manifest_raw)
            shortname_by_service = {c.service: c.shortname for c in manifest.consumes_services_v2}
            for pg in manifest_ungranted_permissions_v2(manifest, granted_records):
                ungranted_perms.append(
                    {
                        "service_url": pg.service_url,
                        "grant": pg.grant,
                        "shortname": shortname_by_service.get(pg.service_url, ""),
                    }
                )
        except Exception:
            logger.opt(exception=True).warning("Failed to parse manifest for permission display (app %s)", app_id)

    edit_app = await _resolve_edit_app(
        app_row["repo_url"], app_row["repo_path"], db, config, zone_for_request(request)
    )

    return Template(
        template_name="app_detail.html",
        context={
            "app": app_row,
            "links": links,
            "databases": databases,
            "port_mappings": port_mappings,
            "services_provided": services_provided,
            "logs": logs,
            "next_url": next,
            "granted_permissions": granted_perms,
            "ungranted_permissions": ungranted_perms,
            "edit_app": edit_app,
        },
    )


async def _resolve_edit_app(
    repo_url: str | None,
    repo_path: str,
    db: sqlite3.Connection,
    config: Config,
    zone: Domain,
) -> dict[str, str] | None:
    """Describe an "Edit this app" affordance for the template.

    Returns one of:
      - ``{"mode": "service", "action": ..., "repo": ..., "ref": ...}`` — POST to
        a provider of the open-workspace service (see
        github.com/imbue-openhost/claude-code-container/services/open-workspace).
      - ``{"mode": "repo", "href": ...}`` — fallback link to the repo URL.
      - ``None`` — no actionable URL available.
    """
    if not repo_url:
        return None
    if repo_url.startswith("file://"):
        # Builtin apps live in the openhost checkout itself (file:// to
        # apps/<name>). A browser can't do anything useful with that path and
        # the open-workspace provider can't clone it, so substitute the
        # enclosing openhost repo — the user lands in the openhost checkout at
        # the same commit and can navigate to apps/<name> from there.
        try:
            openhost_remote = await get_remote_url(config.openhost_repo_path)
        except Exception:
            logger.opt(exception=True).warning("Failed to read openhost remote URL for builtin app")
            openhost_remote = None
        if not openhost_remote:
            return None
        repo_url = openhost_remote
        repo_path = str(config.openhost_repo_path)
    try:
        base_url, ref_from_url = parse_repo_url(repo_url)
    except UnsupportedRepoUrlError:
        # Legacy SSH upstream (set_app_remote rejects these now): fall back to a
        # plain link to the raw URL rather than failing the detail page render.
        return {"mode": "repo", "href": repo_url}
    repo_link_fallback = {"mode": "repo", "href": base_url}
    ref = ref_from_url
    if not ref:
        try:
            ref = await get_head_sha(Path(repo_path))
        except Exception:
            logger.opt(exception=True).warning("Failed to read HEAD sha for %s", repo_path)

    try:
        provider_app_id, _, _, endpoint = resolve_provider(EDIT_APP_SERVICE_URL, EDIT_APP_VERSION_SPEC, db)
    except ServiceNotAvailable:
        return repo_link_fallback

    if not ref:
        return repo_link_fallback

    provider_row = db.execute("SELECT name FROM apps WHERE app_id = ?", (provider_app_id,)).fetchone()
    if not provider_row:
        logger.warning("resolve_provider returned unknown app_id %s", provider_app_id)
        return repo_link_fallback

    # Pass repo+ref in the query string too: the openhost router 302's
    # unauthenticated POSTs to /login, and the post-login redirect comes back
    # as a GET (only 307/308 preserve method), dropping the form body. Query
    # params survive the bounce, and the provider falls back to them.
    qs = urlencode({"repo": base_url, "ref": ref})
    # Build the provider URL on the domain the operator is currently browsing, so the
    # POST stays same-domain (and any login bounce stays on that domain) rather than
    # jumping to the canonical one.
    action = f"{zone.scheme}://{provider_row['name']}.{zone.name}{endpoint}?{qs}"
    return {"mode": "service", "action": action, "repo": base_url, "ref": ref}


@get("/add_app", guards=[require_owner_auth])
async def add_app(
    db: NamedDependency[sqlite3.Connection], repo: FromQuery[str] = "", next: FromQuery[str] = ""
) -> Template:
    catalog_installed = db.execute("SELECT 1 FROM apps WHERE name = ?", (CATALOG_APP_NAME,)).fetchone() is not None
    return Template(
        template_name="add_app.html",
        context={
            "catalog_installed": catalog_installed,
            "catalog_app_name": CATALOG_APP_NAME,
            "catalog_repo_url": CATALOG_REPO_URL,
            "initial_repo": repo,
            "next_url": next,
        },
    )


# excluded due to user-facing html page rather than api
pages_apps_routes = Router(
    path="/",
    route_handlers=[dashboard, app_detail, add_app],
    include_in_schema=False,
)
