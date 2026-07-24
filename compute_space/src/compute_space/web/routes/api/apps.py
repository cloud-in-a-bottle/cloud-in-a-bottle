import asyncio
import os
import re
import shutil
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Annotated
from typing import Any

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.params import Parameter
from litestar.response import Redirect

from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import PermissionGrant
from compute_space.core.apps import all_manifest_permissions_v2
from compute_space.core.apps import app_container_log_path
from compute_space.core.apps import app_log_path
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import git_pull
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import manifest_ungranted_permissions_v2
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import reload_app_background
from compute_space.core.apps import remove_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.apps import validate_manifest
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import archive_old_log
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import log_timestamp
from compute_space.core.containers import stop_app_process
from compute_space.core.containers import stop_container
from compute_space.core.diagnostics import AppDiagnostics
from compute_space.core.diagnostics import collect_app_diagnostics
from compute_space.core.git_ops import UnsupportedRepoUrlError
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import is_dirty
from compute_space.core.git_ops import is_github_repo_url
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.git_ops import reset_hard
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest
from compute_space.core.oauth import OAuthAuthorizationRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.core.ports import check_port_available
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.web.auth.auth import require_owner_auth

# ─── attrs request / response models ──────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


@attr.s(auto_attribs=True, frozen=True)
class AuthRequiredResponse:
    error: str
    authorize_url: str


@attr.s(auto_attribs=True, frozen=True)
class CloneRequest:
    repo_url: str


@attr.s(auto_attribs=True, frozen=True)
class CloneInfoResponse:
    manifest: dict[str, Any]
    clone_dir: str
    app_name: str
    validation_error: str | None = None
    # Storage tiers the app will use, including whether its archive data lands
    # on durable S3 or non-durable local disk.  Surfaced on the install screen
    # alongside permissions.  See archive_backend.storage_summary.
    storage: archive_backend.StorageSummary | None = None


@attr.s(auto_attribs=True, frozen=True)
class CloneAuthorizeResponse:
    authorize_url: str


@attr.s(auto_attribs=True, frozen=True)
class CheckPortResponse:
    port: int
    available: bool
    used_by: dict[str, str] | None


@attr.s(auto_attribs=True, frozen=True)
class AddAppRequest:
    repo_url: str
    app_name: str | None = None
    clone_dir: str | None = None
    # List of {service_url, grant} dicts the owner approved on the deploy page.
    # Only these permissions are granted at install time.
    permissions_v2_grants: list[dict[str, Any]] = attr.Factory(list)
    # Back-compat: CLI sends this boolean. When True and permissions_v2_grants
    # is empty, all manifest permissions are auto-granted.
    grant_permissions_v2: bool = False
    port_overrides: dict[str, int] = attr.Factory(dict)


@attr.s(auto_attribs=True, frozen=True)
class AddAppResponse:
    ok: bool
    app_id: str
    app_name: str
    status: str


@attr.s(auto_attribs=True, frozen=True)
class AppSummary:
    app_id: str
    name: str
    status: str
    error_message: str | None


@attr.s(auto_attribs=True, frozen=True)
class AppStatusResponse:
    status: str
    error: str | None
    error_kind: str | None
    # Git info for the app's checked-out repo. All None when the app has no
    # git repo on disk (e.g. builtin apps copied from the apps/ directory)
    # or when the .git read fails for any reason. ``git_branch`` is None when
    # HEAD is detached even if ``git_sha`` is populated.
    git_branch: str | None = None
    git_sha: str | None = None
    git_dirty: bool | None = None
    container_id: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class ReloadAppRequest:
    update: bool = False
    # When True, the owner has reviewed the permissions the (updated) manifest
    # newly declares and approves granting them as part of this reload. Without
    # it, a reload whose manifest declares new, ungranted permissions is
    # refused (see PermissionsRequiredResponse) — mirroring the explicit
    # owner approval required at install time.
    approve_new_permissions: bool = False


@attr.s(auto_attribs=True, frozen=True)
class PermissionsRequiredResponse:
    """Returned by ``/reload_app`` when the manifest to be deployed declares
    permissions the app does not already hold and the caller has not approved
    them. The reload is NOT performed; the app keeps running its current
    version until the owner re-submits with ``approve_new_permissions``."""

    ok: bool
    permissions_required: list[dict[str, Any]]
    error: str


@attr.s(auto_attribs=True, frozen=True)
class RemoveAppRequest:
    keep_data: bool = False


@attr.s(auto_attribs=True, frozen=True)
class RemoveAppAlreadyRemoving:
    ok: bool  # always True
    already_removing: bool  # always True


@attr.s(auto_attribs=True, frozen=True)
class RenameAppRequest:
    name: str


@attr.s(auto_attribs=True, frozen=True)
class RenameAppResponse:
    ok: bool
    name: str
    app_id: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class SetAppRemoteRequest:
    repo_url: str


@attr.s(auto_attribs=True, frozen=True)
class SetAppRemoteResponse:
    ok: bool
    repo_url: str


# ─── helpers ───────────────────────────────────────────────────────────────


def _is_removing(app_row: sqlite3.Row | None) -> bool:
    """True if the row is being torn down by remove_app_background.

    Mutating routes (stop, reload, rename) refuse to touch a removing
    row with 409. /remove_app itself uses an atomic UPDATE...WHERE
    status != 'removing' instead of this helper to avoid a TOCTOU race
    on concurrent removal requests.
    """
    return app_row is not None and app_row["status"] == "removing"


def _resolve_app_or_error(
    app_id: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response[ErrorResponse] | None]:
    """Validate app_id format and load the app row.

    Returns (row, None) on success, (None, error_response) on bad id or unknown app.
    """
    if not is_valid_app_id(app_id):
        return None, Response(
            content=ErrorResponse(error="Invalid app_id"), status_code=400, media_type=MediaType.JSON
        )
    row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return None, Response(content=ErrorResponse(error="App not found"), status_code=404, media_type=MediaType.JSON)
    return row, None


async def _pin_refless_to_landed_branch(repo_url: str | None, repo_path: str) -> str | None:
    """Return ``repo_url`` with ``@<branch>`` appended for the branch ``repo_path``
    is on, or ``None`` when there's nothing to pin.

    Pins only when ``repo_url`` has no ``@ref`` yet and ``repo_path`` is a git
    checkout sitting on a (non-detached) branch. Used at install and after a
    refless update so the stored upstream names a concrete branch — visible on
    the detail page and deterministic across future pulls — rather than
    re-resolving a moving remote default. A ``file://`` URL pointing at a
    non-git directory is copied verbatim (no ``.git``), so it's skipped here
    rather than letting get_branch_name raise InvalidGitRepositoryError. Reads
    local HEAD only, so no network.
    """
    if not repo_url:
        return None
    try:
        base_url, ref = parse_repo_url(repo_url)
    except UnsupportedRepoUrlError:
        # A stored SSH upstream can't be pinned (and shouldn't exist after the
        # set_app_remote guard); leave it untouched rather than crashing.
        return None
    if ref or not os.path.isdir(os.path.join(repo_path, ".git")):
        return None
    landed = await get_branch_name(Path(repo_path))
    return f"{base_url}@{landed}" if landed else None


# ─── routes ────────────────────────────────────────────────────────────────


@post("/api/clone_and_get_app_info", status_code=200, guards=[require_owner_auth])
async def clone_and_get_app_info(
    data: CloneRequest, db: sqlite3.Connection, config: Config
) -> Response[CloneInfoResponse] | Response[ErrorResponse] | Response[CloneAuthorizeResponse]:
    """Clone a repo and return its manifest info + temp clone dir."""
    repo_url = data.repo_url.strip()
    if not repo_url:
        return Response(content=ErrorResponse(error="No repository URL provided"), status_code=400)

    add_app_url = f"//{config.zone_domain}/add_app?repo={repo_url}"
    manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to=add_app_url)

    if authorize_url:
        return Response(content=CloneAuthorizeResponse(authorize_url=authorize_url), status_code=401)

    if error:
        return Response(content=ErrorResponse(error=error), status_code=400)

    if manifest is None:
        raise RuntimeError("manifest unexpectedly None after successful clone")
    validation_error = validate_manifest(manifest, db)
    info = attr.asdict(manifest)
    info.pop("raw_toml", None)
    assert clone_dir is not None
    storage = archive_backend.storage_summary(manifest.raw_toml, db)
    return Response(
        content=CloneInfoResponse(
            manifest=info,
            clone_dir=clone_dir,
            app_name=manifest.name,
            validation_error=validation_error,
            storage=storage,
        ),
        status_code=200,
        media_type=MediaType.JSON,
    )


@get("/api/check_port", guards=[require_owner_auth])
async def check_port(
    port: Annotated[int, Parameter(ge=1, le=65535)], db: sqlite3.Connection
) -> Response[CheckPortResponse]:
    """Check if a host port is available. Returns {port, available, used_by}."""
    available, used_by = check_port_available(port, db)
    return Response(
        content=CheckPortResponse(port=port, available=available, used_by=used_by),
        status_code=200,
        media_type=MediaType.JSON,
    )


@post("/api/add_app", status_code=200, guards=[require_owner_auth])
async def api_add_app(
    data: AddAppRequest, db: sqlite3.Connection, config: Config
) -> Response[AddAppResponse] | Response[ErrorResponse] | Response[AuthRequiredResponse]:
    """Install an app. Optionally takes a clone_dir from a prior clone_and_get_app_info call."""
    repo_url = data.repo_url.strip()
    app_name: str | None = (data.app_name.strip() or None) if data.app_name else None
    clone_dir: str | None = (data.clone_dir.strip() or None) if data.clone_dir else None

    if not repo_url:
        return Response(content=ErrorResponse(error="No repository URL provided"), status_code=400)

    # Clone if no existing clone_dir provided
    manifest = None
    if not clone_dir or not os.path.isdir(clone_dir):
        manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to="/")
        if authorize_url:
            return Response(
                content=AuthRequiredResponse(error="GitHub authorization required", authorize_url=authorize_url),
                status_code=401,
            )
        if error:
            return Response(content=ErrorResponse(error=error), status_code=400)

    if clone_dir is None:
        raise RuntimeError("clone_dir unexpectedly None after successful clone")
    if manifest is None:
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            return Response(content=ErrorResponse(error=str(e)), status_code=400)

    if app_name is None:
        app_name = manifest.name

    validation_error = validate_manifest(manifest, db, app_name=app_name)
    if validation_error:
        shutil.rmtree(clone_dir, ignore_errors=True)
        return Response(content=ErrorResponse(error=validation_error), status_code=400)

    # The archive tier is ALWAYS available: it is a JuiceFS mount on every
    # zone (a local file-backed volume by default, S3 after an operator
    # upgrade), brought up at boot by attach_on_startup.  So app_archive no
    # longer blocks installation for lack of S3.  We only refuse when that
    # JuiceFS mount is transiently unhealthy (retryable 503), regardless of
    # whether it's the local or S3 backend.
    if manifest.app_archive:
        if not archive_backend.is_archive_dir_healthy(config, db):
            shutil.rmtree(clone_dir, ignore_errors=True)
            return Response(
                content=ErrorResponse(
                    error=(
                        "Archive backend is not healthy; refusing to deploy "
                        "an archive-using app until the JuiceFS mount is live "
                        "again (see the dashboard's Archive backend panel)."
                    )
                ),
                status_code=503,
            )

    final_dir = move_clone_to_app_temp_dir(clone_dir, app_name, config)

    # Pin a refless upstream to the concrete branch the fresh clone landed on
    # (origin's default), so the stored URL and detail page show which branch
    # the app tracks from the start — matching what a later Update & Reload records.
    pinned = await _pin_refless_to_landed_branch(repo_url, final_dir)
    if pinned:
        repo_url = pinned

    port_overrides: dict[str, int] | None = dict(data.port_overrides) if data.port_overrides else None

    # Resolve permissions to grant: explicit list from the deploy page,
    # or all manifest permissions if the CLI's --grant-permissions-v2 flag is set.
    grants: list[PermissionGrant] = [
        PermissionGrant(service_url=g["service_url"], grant=g["grant"]) for g in data.permissions_v2_grants
    ]
    if not grants and data.grant_permissions_v2:
        grants = all_manifest_permissions_v2(manifest)

    try:
        app_id = insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            permissions_v2_grants=grants,
            app_name=app_name,
            repo_url=repo_url,
            port_overrides=port_overrides,
        )
    except (RuntimeError, ValueError) as e:
        # ValueError covers uid_map pool exhaustion (see compute_uid_map_base)
        # and other manifest-validation errors raised at insert time; both
        # map to a 400 rather than a 500.
        return Response(content=ErrorResponse(error=str(e)), status_code=400)

    return Response(
        content=AddAppResponse(ok=True, app_id=app_id, app_name=app_name, status="building"),
        status_code=200,
        media_type=MediaType.JSON,
    )


@get("/api/apps", guards=[require_owner_auth])
async def api_apps(db: sqlite3.Connection) -> list[AppSummary]:
    rows = db.execute("SELECT app_id, name, status, error_message FROM apps ORDER BY name").fetchall()
    return [
        AppSummary(
            app_id=row["app_id"],
            name=row["name"],
            status=row["status"],
            error_message=row["error_message"],
        )
        for row in rows
    ]


async def _read_app_git_info(repo_path: str | None) -> tuple[str | None, str | None, bool | None]:
    """Return (branch, sha, dirty) for an app's repo, or (None, None, None) on any error."""
    if not repo_path:
        return None, None, None
    path = Path(repo_path)
    if not (path / ".git").exists():
        return None, None, None
    try:
        branch = await get_branch_name(path)
        sha = await get_head_sha(path)
        dirty = await is_dirty(path)
    except Exception:
        return None, None, None
    return branch, sha, dirty


@get("/api/app_status/{app_id:str}", guards=[require_owner_auth])
async def app_status(app_id: str, db: sqlite3.Connection) -> Response[AppStatusResponse] | Response[ErrorResponse]:
    if not is_valid_app_id(app_id):
        return Response(content=ErrorResponse(error="Invalid app_id"), status_code=400)
    app_row = db.execute(
        "SELECT status, error_message, repo_path, container_id FROM apps WHERE app_id = ?", (app_id,)
    ).fetchone()
    if not app_row:
        return Response(content=ErrorResponse(error="not found"), status_code=404)
    error_msg = app_row["error_message"]
    error_kind = None
    # error_message may carry either the current BUILD_CACHE_CORRUPT_MARKER
    # or the legacy ``[CACHE_CORRUPT]`` marker; both trigger the same
    # 'drop cache and rebuild' remediation in the UI.
    if error_msg and (BUILD_CACHE_CORRUPT_MARKER in error_msg or "[CACHE_CORRUPT]" in error_msg):
        error_kind = "build_cache_corrupt"
        error_msg = "Container build cache is corrupted."
    git_branch, git_sha, git_dirty = await _read_app_git_info(app_row["repo_path"])
    return Response(
        content=AppStatusResponse(
            status=app_row["status"],
            error=error_msg,
            error_kind=error_kind,
            git_branch=git_branch,
            git_sha=git_sha,
            git_dirty=git_dirty,
            container_id=app_row["container_id"],
        ),
        status_code=200,
        media_type=MediaType.JSON,
    )


def _app_diagnostics_filename(app_name: str) -> str:
    """Build a safe, timestamped filename for a downloaded app diagnostics bundle."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_name = "".join(c if c.isalnum() or c in "-." else "_" for c in app_name) or "app"
    return f"openhost-app-diagnostics-{safe_name}-{stamp}.json"


@get("/api/app_diagnostics/{app_id:str}", guards=[require_owner_auth])
async def app_diagnostics(
    app_id: str, db: sqlite3.Connection, config: Config, download: bool = False
) -> Response[AppDiagnostics] | Response[ErrorResponse]:
    """Return a per-app diagnostics bundle: app version + manifest git checkout,
    container status, and a slice of host/system info so the report is
    self-contained.

    ``?download=1`` adds a Content-Disposition header so browsers save the JSON
    to a timestamped file instead of rendering it inline.
    """
    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    diagnostics = await collect_app_diagnostics(app_row, config)
    headers = None
    if download:
        headers = {"Content-Disposition": f'attachment; filename="{_app_diagnostics_filename(app_row["name"])}"'}
    return Response(content=diagnostics, status_code=200, media_type=MediaType.JSON, headers=headers)


@get("/app_logs/{app_id:str}", guards=[require_owner_auth], media_type=MediaType.TEXT)
async def app_logs(app_id: str, db: sqlite3.Connection, config: Config) -> Response[str] | Response[ErrorResponse]:
    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    logs = get_docker_logs(app_row["name"], config.temporary_data_dir, app_row["container_id"])
    return Response(content=logs, status_code=200, media_type=MediaType.TEXT)


@post("/stop_app/{app_id:str}", status_code=200, guards=[require_owner_auth])
async def stop_app(app_id: str, db: sqlite3.Connection) -> Response[OkResponse] | Response[ErrorResponse]:
    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content=ErrorResponse(error="App is being removed"), status_code=409)

    stop_app_process(app_row)
    stop_container(f"openhost-{app_row['name']}")
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()
    return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)


def _gate_new_permissions(
    app_id: str,
    repo_path: str,
    approve_new_permissions: bool,
) -> PermissionsRequiredResponse | None:
    """Enforce explicit owner approval of permissions a reload would newly grant.

    Reads the manifest that is about to be deployed (from ``repo_path`` on disk,
    which already reflects any git pull) and diffs its declared permissions
    against what the app already holds. If the manifest declares nothing new,
    returns ``None`` (proceed). If it declares new permissions:

    - ``approve_new_permissions=True``: grant them and return ``None`` (proceed),
      mirroring the owner-approved grants at install time.
    - otherwise: return a :class:`PermissionsRequiredResponse` so the caller can
      refuse the reload until the owner approves.

    A manifest that can't be parsed is treated as "nothing new" here; the reload
    path will surface the parse error on its own.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return None
    try:
        manifest = parse_manifest(repo_path)
    except ValueError:
        return None

    ungranted = manifest_ungranted_permissions_v2(manifest, get_all_permissions_v2(consumer_app_id=app_id))
    if not ungranted:
        return None

    if approve_new_permissions:
        for pg in ungranted:
            grant_permission_v2(consumer_app_id=app_id, service_url=pg.service_url, grant_payload=pg.grant)
        return None

    shortname_by_service = {c.service: c.shortname for c in manifest.consumes_services_v2}
    return PermissionsRequiredResponse(
        ok=False,
        permissions_required=[
            {
                "service_url": pg.service_url,
                "grant": pg.grant,
                "shortname": shortname_by_service.get(pg.service_url, ""),
            }
            for pg in ungranted
        ],
        error=("This update declares new service permissions that must be approved before it can be applied."),
    )


async def _reload_app_impl(
    app_id: str,
    update: bool,
    continue_oauth: bool,
    approve_new_permissions: bool,
    db: sqlite3.Connection,
    config: Config,
) -> Response[OkResponse] | Response[ErrorResponse] | Response[PermissionsRequiredResponse] | Redirect:
    """Shared body for the POST (user-initiated reload) and GET (OAuth callback)
    entry points to ``/reload_app/{app_id}``."""
    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content=ErrorResponse(error="App is being removed"), status_code=409)
    app_name = app_row["name"]

    # The archive tier is a JuiceFS mount for both the local and S3 backends;
    # refuse to reload an app that hard-requires it while that mount is down.
    if not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_requires_archive(app_row["manifest_raw"] or ""):
            return Response(
                content=ErrorResponse(
                    error=(
                        "Archive backend is not healthy; refusing to "
                        "reload an app that requires app_archive until "
                        "the operator-configured archive mount is live "
                        "again (see the dashboard's Archive backend panel)."
                    )
                ),
                status_code=503,
            )

    log_file = app_log_path(app_name, config)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not continue_oauth:
        ts = log_timestamp(log_file)
        archive_old_log(log_file, ts=ts)
        archive_old_log(app_container_log_path(app_name, config), ts=ts)

    with open(log_file, "a") as lf:
        if not continue_oauth:
            lf.write(f"reloading app (update={update})\n")
        else:
            lf.write("continuing app reload after oauth\n")

        # SHA the app is currently deployed at, captured before any pull so a
        # refused update can roll the working tree back to it (keeping the
        # on-disk repo in sync with the running version — see the gate below).
        pre_pull_sha: str | None = None

        if update or continue_oauth:
            if not app_row["repo_path"] or not os.path.isdir(os.path.join(app_row["repo_path"], ".git")):
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (
                        "No git repository found to update. If this is a builtin app, git-based updates are not possible.",
                        app_id,
                    ),
                )
                db.commit()
                return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)

            try:
                pre_pull_sha = await get_head_sha(Path(app_row["repo_path"]))
            except Exception as e:  # noqa: BLE001 — best-effort; rollback just won't run
                lf.write(f"Could not read pre-pull HEAD sha: {e}\n")

            repo_url = app_row["repo_url"] or ""
            pull_ok = False
            pull_err = None

            if not continue_oauth:
                lf.write("Attempting git pull without github oauth\n")
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row["repo_path"],
                    app_name,
                    log_file=log_file,
                    repo_url=repo_url,
                )

            if not pull_ok and is_github_repo_url(repo_url):
                lf.write("Attempting git pull with github oauth\n")
                lf.flush()
                return_to = f"//{config.zone_domain}/reload_app/{app_id}?continue_oauth_update=1"
                try:
                    token = await get_oauth_token("github", ["repo"], return_to=return_to)
                except ServiceNotAvailable as e:
                    lf.write(f"Secrets service unavailable: {e.message}\n")
                    db.execute(
                        "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                        (e.message, app_id),
                    )
                    db.commit()
                    if continue_oauth:
                        return Redirect(path=f"/app_detail/{app_name}")
                    return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)
                except OAuthAuthorizationRequired as e:
                    lf.write("No token available; redirecting to oauth flow\n")
                    return Redirect(path=e.authorize_url)
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row["repo_path"],
                    app_name,
                    github_token=token,
                    log_file=log_file,
                    repo_url=repo_url,
                )

            if not pull_ok:
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (f"Git pull failed: {pull_err}", app_id),
                )
                db.commit()
                if continue_oauth:
                    return Redirect(path=f"/app_detail/{app_name}")
                return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)

            # The pull succeeded. If the upstream had no pinned ``@ref`` (the
            # operator cleared it, or it was never set), git_pull resolved and
            # checked out origin's default branch. Record that concrete branch
            # back into repo_url so the app stays on it deterministically and
            # the detail page shows which branch it tracks — rather than
            # silently re-resolving (and following) a moving remote default on
            # every future update.
            pinned = await _pin_refless_to_landed_branch(repo_url, app_row["repo_path"])
            if pinned:
                db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (pinned, app_id))
                db.commit()
                lf.write(f"Pinned upstream to {pinned}\n")

    # Gate: when an update pulls a new manifest that declares permissions the
    # app doesn't already hold, refuse the reload until the owner approves them
    # (the install flow requires the same explicit approval). Runs before the
    # running container is touched, so a refused update leaves the app untouched.
    #
    # Only applies when code is actually being pulled (update / oauth re-entry).
    # A plain reload deploys the manifest already on disk — the one the app is
    # currently running — so it can't introduce new permissions, and gating it
    # would wrongly re-prompt for permissions the owner deliberately declined at
    # install and chose to keep running without.
    if update or continue_oauth:
        perm_gate = await asyncio.to_thread(
            _gate_new_permissions, app_id, app_row["repo_path"], approve_new_permissions
        )
        if perm_gate is not None:
            # Roll the working tree back to the version the app is running, so the
            # pulled-but-refused code (which declares the unapproved permissions)
            # does not linger on disk where a later plain reload — which is not
            # gated, on the assumption the on-disk manifest matches the running
            # one — would silently deploy it.
            if pre_pull_sha:
                try:
                    await reset_hard(Path(app_row["repo_path"]), pre_pull_sha)
                except Exception as e:  # noqa: BLE001
                    with open(log_file, "a") as lf:
                        lf.write(f"WARNING: failed to roll back refused update to {pre_pull_sha}: {e}\n")
            with open(log_file, "a") as lf:
                lf.write("Update requires approval of new service permissions; not reloading.\n")
            if continue_oauth:
                return Redirect(path=f"/app_detail/{app_name}")
            return Response(content=perm_gate, status_code=200, media_type=MediaType.JSON)

    await asyncio.to_thread(stop_app_process, app_row)
    db.execute(
        "UPDATE apps SET status = 'building', container_id = NULL, error_message = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    Thread(
        target=reload_app_background,
        args=(app_id, app_row["repo_path"], config),
        daemon=True,
    ).start()

    return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)


@post("/reload_app/{app_id:str}", status_code=200, guards=[require_owner_auth])
async def reload_app(
    app_id: str,
    db: sqlite3.Connection,
    config: Config,
    data: ReloadAppRequest = ReloadAppRequest(),  # noqa: B008 — Litestar resolves this at dependency-injection time
) -> Response[OkResponse] | Response[ErrorResponse] | Response[PermissionsRequiredResponse] | Redirect:
    """User-initiated reload, optionally pulling latest code via ``update``."""
    return await _reload_app_impl(
        app_id,
        update=data.update,
        continue_oauth=False,
        approve_new_permissions=data.approve_new_permissions,
        db=db,
        config=config,
    )


@get("/reload_app/{app_id:str}", guards=[require_owner_auth])
async def reload_app_after_oauth(
    app_id: str,
    db: sqlite3.Connection,
    config: Config,
    continue_oauth_update: Annotated[bool, Parameter(query="continue_oauth_update", required=False)] = False,
) -> Response[OkResponse] | Response[ErrorResponse] | Response[PermissionsRequiredResponse] | Redirect:
    """OAuth callback re-entry: the secrets app redirected the user back here
    after they granted GitHub access.  Resumes the update with ``continue_oauth=True``
    so we don't truncate the log file again or re-prompt for OAuth."""
    if not continue_oauth_update:
        # GET without the flag isn't meaningful; nudge the operator back to /app_detail.
        row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        if not row:
            return Redirect(path="/dashboard")
        return Redirect(path=f"/app_detail/{row['name']}")
    return await _reload_app_impl(
        app_id,
        update=True,
        continue_oauth=True,
        approve_new_permissions=False,
        db=db,
        config=config,
    )


@post("/remove_app/{app_id:str}", status_code=202, guards=[require_owner_auth])
async def remove_app(
    app_id: str,
    db: sqlite3.Connection,
    config: Config,
    data: RemoveAppRequest = RemoveAppRequest(),  # noqa: B008 — body is optional; default = remove with keep_data=False
) -> Response[OkResponse] | Response[RemoveAppAlreadyRemoving] | Response[ErrorResponse]:
    """Flip the row to ``status='removing'`` and run teardown in a thread.

    Returns 202 immediately. The dashboard's /api/apps poll picks up
    the new status and renders 'Removing…' until the row is deleted,
    so reloading the page or opening a second tab still shows the
    in-flight state.
    """
    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None

    keep_data = data.keep_data

    # Refuse destructive remove on an unhealthy archive: rmtree of an
    # empty mountpoint would leave S3 bytes orphaned while the DB row
    # disappears. Must run before the atomic-claim UPDATE.
    if not keep_data and not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
            return Response(
                content=ErrorResponse(
                    error=(
                        "Archive backend is not healthy; refusing to "
                        "remove an archive-using app's data because the "
                        "S3-side bytes wouldn't actually be deleted.  "
                        "Either restore the archive mount and retry, or "
                        "use keep_data=1 to remove the app while leaving "
                        "its data in place."
                    )
                ),
                status_code=503,
            )

    # Atomic claim: ``WHERE status != 'removing'`` makes concurrent
    # POSTs safe — only the first one gets rowcount=1 and spawns a
    # worker; later ones short-circuit to the already_removing branch.
    cursor = db.execute(
        "UPDATE apps SET status = 'removing', error_message = NULL WHERE app_id = ? AND status != 'removing'",
        (app_id,),
    )
    db.commit()

    if cursor.rowcount == 0:
        return Response(
            content=RemoveAppAlreadyRemoving(ok=True, already_removing=True),
            status_code=202,
            media_type=MediaType.JSON,
        )

    try:
        Thread(
            target=remove_app_background,
            args=(app_id, keep_data, config),
            daemon=True,
        ).start()
    except Exception as e:
        # Thread spawn failed (resource exhaustion). Roll the row back
        # to 'error' so a retry can re-claim it via the atomic UPDATE.
        logger.exception("Could not spawn remove worker for %s", app_id)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            (f"Could not start removal worker: {e}", app_id),
        )
        db.commit()
        return Response(content=ErrorResponse(error="Could not start removal worker; try again."), status_code=503)

    return Response(content=OkResponse(ok=True), status_code=202, media_type=MediaType.JSON)


def _rename_app_storage_dirs(config: Config, old_name: str, new_name: str, archive_dir: str) -> str | None:
    """Rename per-app subdirs across the three storage tiers, with rollback on partial failure.

    ``archive_dir`` is the archive parent for the current backend — always
    the JuiceFS mountpoint now (absent only on a legacy 'disabled' zone) —
    pass ``archive_backend.effective_archive_dir``.  Getting this wrong
    orphans an app's archive data under its old name.

    Returns ``None`` on success or an error message on failure. Sync helper
    so the blocking renames (which can be slow on JuiceFS) stay off the
    event loop.
    """
    rename_parents = [
        os.path.join(config.persistent_data_dir, "app_data"),
        os.path.join(config.temporary_data_dir, "app_temp_data"),
    ]
    for parent in rename_parents:
        if not os.path.isdir(parent):
            return f"Storage parent {parent!r} is not a directory; refusing to rename so per-app data isn't orphaned."
    # The archive parent is the JuiceFS mountpoint, present whenever the mount
    # is live (both local and S3 backends).  Skip it cleanly only on a legacy
    # 'disabled' zone where the mountpoint never gets created.
    if os.path.isdir(archive_dir):
        rename_parents.append(archive_dir)
    renamed: list[tuple[str, str]] = []
    try:
        for parent in rename_parents:
            old_dir = os.path.join(parent, old_name)
            new_dir = os.path.join(parent, new_name)
            if os.path.exists(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
                renamed.append((old_dir, new_dir))
    except OSError as exc:
        for old_dir, new_dir in reversed(renamed):
            try:
                os.rename(new_dir, old_dir)
            except OSError as rollback_exc:
                logger.error(
                    "Rollback of partial rename %s -> %s failed: %s",
                    new_dir,
                    old_dir,
                    rollback_exc,
                )
        return str(exc)
    return None


@post("/rename_app/{app_id:str}", status_code=200, guards=[require_owner_auth])
async def rename_app(
    app_id: str,
    data: RenameAppRequest,
    db: sqlite3.Connection,
    config: Config,
) -> Response[RenameAppResponse] | Response[ErrorResponse]:
    """Rename an app's label and subdomain. The app_id (cross-table identity) stays the same."""
    new_name = data.name.strip()

    if not new_name:
        return Response(content=ErrorResponse(error="Name is required"), status_code=400)

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", new_name):
        return Response(
            content=ErrorResponse(error="Name must be lowercase alphanumeric (hyphens allowed, not at start/end)"),
            status_code=400,
        )

    if f"/{new_name}" in RESERVED_PATHS:
        return Response(
            content=ErrorResponse(error=f"Name '{new_name}' conflicts with a reserved path"), status_code=400
        )

    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content=ErrorResponse(error="App is being removed"), status_code=409)
    old_name = app_row["name"]

    # Refuse rename on an unhealthy archive ONLY if this app actually
    # uses the archive — otherwise the archive subdir would be silently
    # skipped while other tiers are renamed, orphaning the archive
    # contents under the old name.  Apps that don't use the archive
    # tier are unaffected and can rename freely on any backend state.
    if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
        if not archive_backend.is_archive_dir_healthy(config, db):
            return Response(
                content=ErrorResponse(
                    error=(
                        "Archive backend is not healthy; refusing to rename "
                        "an archive-using app until the JuiceFS mount is live "
                        "again (see the dashboard's Archive backend panel)."
                    )
                ),
                status_code=503,
            )

    if new_name == old_name:
        return Response(content=RenameAppResponse(ok=True, name=new_name), status_code=200, media_type=MediaType.JSON)

    conflict = db.execute("SELECT name FROM apps WHERE name = ?", (new_name,)).fetchone()
    if conflict:
        return Response(content=ErrorResponse(error=f"Name already in use by '{conflict['name']}'"), status_code=409)

    prior_status = app_row["status"]
    prior_container_id = app_row["container_id"]
    was_running = prior_status in ("running", "starting", "building")
    stop_app_process(app_row)
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    # Off-loop because JuiceFS renames can take hundreds of ms.
    rename_error = await asyncio.to_thread(
        _rename_app_storage_dirs,
        config,
        old_name,
        new_name,
        archive_backend.effective_archive_dir(config, db),
    )
    if rename_error is not None:
        rollback_db_error: str | None = None
        try:
            db.execute(
                "UPDATE apps SET status = ?, container_id = ? WHERE app_id = ?",
                (prior_status, prior_container_id, app_id),
            )
            db.commit()
        except sqlite3.Error as db_exc:
            logger.error("Failed to restore status during rename rollback: %s", db_exc)
            rollback_db_error = str(db_exc)
        error_message = f"Failed to rename app data directories: {rename_error}"
        if rollback_db_error is not None:
            error_message += (
                f"; additionally, the DB-status rollback failed: "
                f"{rollback_db_error}.  Check the apps table; the row "
                f"for app_id={app_id!r} may be stuck at status='stopped'."
            )
        return Response(content=ErrorResponse(error=error_message), status_code=500)

    # Identity (app_id) is unchanged, so no FK rewrites in app_tokens,
    # app_databases.app_id, app_port_mappings, service_providers_v2,
    # permissions_v2, etc. — they all point at app_id which is stable.
    # Only the name label and any name-keyed paths need rewriting.
    db.execute(
        "UPDATE apps SET name = ?, repo_path = REPLACE(repo_path, ?, ?) WHERE app_id = ?",
        (new_name, f"/{old_name}/", f"/{new_name}/", app_id),
    )
    db.execute(
        "UPDATE app_databases SET db_path = REPLACE(db_path, ?, ?) WHERE app_id = ?",
        (f"/{old_name}/", f"/{new_name}/", app_id),
    )
    db.commit()

    if was_running:
        # Persist failures instead of letting them 500 out: the rename
        # has already succeeded, and the dashboard needs a visible error
        # on the app rather than a generic server error.
        try:
            start_app_process(app_id, db, config)
        except (RuntimeError, ValueError) as e:
            logger.warning("Failed to restart %s after rename: %s", new_name, e)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                (str(e), app_id),
            )
            db.commit()

    return Response(
        content=RenameAppResponse(ok=True, name=new_name, app_id=app_id),
        status_code=200,
        media_type=MediaType.JSON,
    )


@post("/set_app_remote/{app_id:str}", status_code=200, guards=[require_owner_auth])
async def set_app_remote(
    app_id: str,
    data: SetAppRemoteRequest,
    db: sqlite3.Connection,
) -> Response[SetAppRemoteResponse] | Response[ErrorResponse]:
    """Edit an app's git upstream (repo URL and/or ``@branch`` ref).

    Persists the new value to ``apps.repo_url``; the next ``Update & Reload``
    (``/reload_app`` with ``update=true``) re-points origin and checks out the
    pinned ref. Only git-backed apps can have an upstream — builtin apps copied
    from the apps/ directory have no ``.git`` to update.
    """
    repo_url = data.repo_url.strip()
    if not repo_url:
        return Response(content=ErrorResponse(error="Repo URL is required"), status_code=400)

    app_row, err = _resolve_app_or_error(app_id, db)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content=ErrorResponse(error="App is being removed"), status_code=409)

    if not app_row["repo_path"] or not os.path.isdir(os.path.join(app_row["repo_path"], ".git")):
        return Response(
            content=ErrorResponse(error="This app has no git repository, so its upstream cannot be edited."),
            status_code=400,
        )

    # Normalise via parse_repo_url so a bare hostname gets an https:// scheme
    # and the stored value matches what git_pull will re-point origin to.
    try:
        base_url, ref = parse_repo_url(repo_url)
    except UnsupportedRepoUrlError as e:
        return Response(content=ErrorResponse(error=str(e)), status_code=400)
    normalized = f"{base_url}@{ref}" if ref else base_url

    db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (normalized, app_id))
    db.commit()

    return Response(
        content=SetAppRemoteResponse(ok=True, repo_url=normalized),
        status_code=200,
        media_type=MediaType.JSON,
    )


api_apps_routes = Router(
    path="/",
    route_handlers=[
        clone_and_get_app_info,
        check_port,
        api_add_app,
        api_apps,
        app_status,
        app_diagnostics,
        app_logs,
        stop_app,
        reload_app,
        reload_app_after_oauth,
        remove_app,
        rename_app,
        set_app_remote,
    ],
)
