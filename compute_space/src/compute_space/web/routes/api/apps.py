import asyncio
import contextlib
import os
import re
import shutil
import sqlite3
from threading import Thread
from typing import Annotated
from typing import Any

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import WebSocket
from litestar import get
from litestar import post
from litestar import websocket
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.exceptions import InternalServerException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import NotFoundException
from litestar.exceptions import ServiceUnavailableException
from litestar.exceptions import ValidationException
from litestar.exceptions import WebSocketDisconnect
from litestar.params import FromPath
from litestar.params import FromQuery
from litestar.params import Parameter
from litestar.response import Redirect

from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import AppConflictError
from compute_space.core.apps import AppError
from compute_space.core.apps import AppInputError
from compute_space.core.apps import AppNotFoundError
from compute_space.core.apps import AppUnavailableError
from compute_space.core.apps import ReloadOutcome
from compute_space.core.apps import app_log_path
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import is_removing
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import pin_refless_to_landed_branch
from compute_space.core.apps import read_app_git_info
from compute_space.core.apps import reload_app as core_reload_app
from compute_space.core.apps import remove_app_background
from compute_space.core.apps import rename_app_storage_dirs
from compute_space.core.apps import resolve_app
from compute_space.core.apps import start_app_process
from compute_space.core.apps import validate_manifest
from compute_space.core.apps import wipe_data_restart as core_wipe_data_restart
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import stop_app_process
from compute_space.core.containers import stop_container
from compute_space.core.diagnostics import AppDiagnostics
from compute_space.core.diagnostics import app_diagnostics_filename
from compute_space.core.diagnostics import collect_app_diagnostics
from compute_space.core.domains import primary_domain
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.log_stream import stream_app_logs
from compute_space.core.logging import logger
from compute_space.core.manifest import PermissionGrant
from compute_space.core.manifest import all_manifest_permissions_v2
from compute_space.core.manifest import parse_manifest
from compute_space.core.ports import check_port_available
from compute_space.core.updates import wait_for_shutdown
from compute_space.db.connection import get_db
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import verify_owner_ws
from compute_space.web.exceptions import ConflictException

# ─── attrs request / response models ──────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


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
    git_branch: str | None = None
    git_sha: str | None = None
    git_dirty: bool | None = None
    container_id: str | None = None
    repo_url: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class ReloadAppRequest:
    update: bool = False
    # When True, the owner has reviewed the settings the (updated) manifest
    # changes — including any newly declared permissions — and approves the
    # update. Without it, a reload whose pulled manifest differs from the
    # running one is refused (see UpdateReviewRequiredResponse); new permissions
    # are then granted as part of the approved reload, mirroring install time.
    approve_new_permissions: bool = False


@attr.s(auto_attribs=True, frozen=True)
class UpdateReviewRequiredResponse:
    """Returned by ``/reload_app`` when an update's pulled manifest differs from
    the running one and the caller hasn't approved it. The reload is NOT
    performed; the app keeps running its current version until the owner
    re-submits with ``approve_new_permissions``. ``settings_changed`` is the
    grouped old→new diff; ``permissions_required`` the newly declared grants."""

    ok: bool
    review_required: bool
    settings_changed: list[dict[str, object]]
    permissions_required: list[dict[str, object]]
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


_APP_ERROR_HTTP: dict[type[AppError], type[HTTPException]] = {
    AppInputError: ValidationException,
    AppNotFoundError: NotFoundException,
    AppConflictError: ConflictException,
    AppUnavailableError: ServiceUnavailableException,
}


def _to_http_error(exc: AppError) -> HTTPException:
    """Translate a core AppError into the litestar HTTPException the route surfaces."""
    return _APP_ERROR_HTTP[type(exc)](detail=str(exc))


# ─── routes ────────────────────────────────────────────────────────────────


@post(
    "/api/clone_and_get_app_info",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotAuthorizedException],
)
async def clone_and_get_app_info(
    data: CloneRequest,
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[CloneInfoResponse]:
    """Clone a repo and return its manifest info + temp clone dir."""
    repo_url = data.repo_url.strip()
    if not repo_url:
        raise ValidationException(detail="No repository URL provided")

    add_app_url = f"//{primary_domain(db).name}/add_app?repo={repo_url}"
    manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to=add_app_url)

    if authorize_url:
        # The client follows `authorize_url` to complete the GitHub OAuth flow.
        raise NotAuthorizedException(detail="GitHub authorization required", extra={"authorize_url": authorize_url})

    if error:
        raise ValidationException(detail=error)

    if manifest is None:
        raise InternalServerException(
            detail="Internal server error", extra={"output": "manifest unexpectedly None after successful clone"}
        )
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
    port: Annotated[int, Parameter(ge=1, le=65535)], db: NamedDependency[sqlite3.Connection]
) -> Response[CheckPortResponse]:
    """Check if a host port is available. Returns {port, available, used_by}."""
    available, used_by = check_port_available(port, db)
    return Response(
        content=CheckPortResponse(port=port, available=available, used_by=used_by),
        status_code=200,
        media_type=MediaType.JSON,
    )


@post(
    "/api/add_app",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotAuthorizedException, ServiceUnavailableException],
)
async def api_add_app(
    data: AddAppRequest,
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[AddAppResponse]:
    """Install an app. Optionally takes a clone_dir from a prior clone_and_get_app_info call."""
    repo_url = data.repo_url.strip()
    app_name: str | None = (data.app_name.strip() or None) if data.app_name else None
    clone_dir: str | None = (data.clone_dir.strip() or None) if data.clone_dir else None

    if not repo_url:
        raise ValidationException(detail="No repository URL provided")

    # Clone if no existing clone_dir provided
    manifest = None
    if not clone_dir or not os.path.isdir(clone_dir):
        manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to="/")
        if authorize_url:
            raise NotAuthorizedException(
                detail="GitHub authorization required", extra={"authorize_url": authorize_url}
            )
        if error:
            raise ValidationException(detail=error)

    if clone_dir is None:
        raise InternalServerException(
            detail="Internal server error", extra={"output": "clone_dir unexpectedly None after successful clone"}
        )
    if manifest is None:
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            raise ValidationException(detail=str(e)) from e

    if app_name is None:
        app_name = manifest.name

    validation_error = validate_manifest(manifest, db, app_name=app_name)
    if validation_error:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise ValidationException(detail=validation_error)

    # The archive tier is ALWAYS available: it is a JuiceFS mount on every
    # zone (a local file-backed volume by default, S3 after an operator
    # upgrade), brought up at boot by attach_on_startup.  So app_archive no
    # longer blocks installation for lack of S3.  We only refuse when that
    # JuiceFS mount is transiently unhealthy (retryable 503), regardless of
    # whether it's the local or S3 backend.
    if manifest.app_archive:
        if not archive_backend.is_archive_dir_healthy(config, db):
            shutil.rmtree(clone_dir, ignore_errors=True)
            raise ServiceUnavailableException(
                detail=(
                    "Archive backend is not healthy; refusing to deploy "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                )
            )

    final_dir = move_clone_to_app_temp_dir(clone_dir, app_name, config)

    # Pin a refless upstream to the concrete branch the fresh clone landed on
    # (origin's default), so the stored URL and detail page show which branch
    # the app tracks from the start — matching what a later Update & Reload records.
    pinned = await pin_refless_to_landed_branch(repo_url, final_dir)
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
        raise ValidationException(detail=str(e)) from e

    return Response(
        content=AddAppResponse(ok=True, app_id=app_id, app_name=app_name, status="building"),
        status_code=200,
        media_type=MediaType.JSON,
    )


@get("/api/apps", guards=[require_owner_auth])
async def api_apps(db: NamedDependency[sqlite3.Connection]) -> list[AppSummary]:
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


@get("/api/app_status/{app_id:str}", guards=[require_owner_auth], raises=[ValidationException, NotFoundException])
async def app_status(app_id: FromPath[str], db: NamedDependency[sqlite3.Connection]) -> Response[AppStatusResponse]:
    if not is_valid_app_id(app_id):
        raise ValidationException(detail="Invalid app_id")
    app_row = db.execute(
        "SELECT status, error_message, repo_path, repo_url, container_id FROM apps WHERE app_id = ?", (app_id,)
    ).fetchone()
    if not app_row:
        raise NotFoundException(detail="App not found")
    error_msg = app_row["error_message"]
    error_kind = None
    # error_message may carry either the current BUILD_CACHE_CORRUPT_MARKER
    # or the legacy ``[CACHE_CORRUPT]`` marker; both trigger the same
    # 'drop cache and rebuild' remediation in the UI.
    if error_msg and (BUILD_CACHE_CORRUPT_MARKER in error_msg or "[CACHE_CORRUPT]" in error_msg):
        error_kind = "build_cache_corrupt"
        error_msg = "Container build cache is corrupted."
    git_branch, git_sha, git_dirty = await read_app_git_info(app_row["repo_path"])
    return Response(
        content=AppStatusResponse(
            status=app_row["status"],
            error=error_msg,
            error_kind=error_kind,
            git_branch=git_branch,
            git_sha=git_sha,
            git_dirty=git_dirty,
            container_id=app_row["container_id"],
            repo_url=app_row["repo_url"],
        ),
        status_code=200,
        media_type=MediaType.JSON,
    )


@get(
    "/api/app_diagnostics/{app_id:str}",
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException],
)
async def app_diagnostics(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
    download: FromQuery[bool] = False,
) -> Response[AppDiagnostics]:
    """Return a per-app diagnostics bundle: app version + manifest git checkout,
    container status, and a slice of host/system info so the report is
    self-contained.

    ``?download=1`` adds a Content-Disposition header so browsers save the JSON
    to a timestamped file instead of rendering it inline.
    """
    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    diagnostics = await collect_app_diagnostics(app_row, config, db)
    headers = None
    if download:
        headers = {"Content-Disposition": f'attachment; filename="{app_diagnostics_filename(app_row["name"])}"'}
    return Response(content=diagnostics, status_code=200, media_type=MediaType.JSON, headers=headers)


@get(
    "/app_logs/{app_id:str}",
    guards=[require_owner_auth],
    media_type=MediaType.TEXT,
    raises=[ValidationException, NotFoundException],
)
async def app_logs(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    logs = get_docker_logs(app_row["name"], config.temporary_data_dir, app_row["container_id"])
    return Response(content=logs, status_code=200, media_type=MediaType.TEXT)


@websocket("/app_logs_stream/{app_id:str}")
async def app_logs_stream(
    socket: WebSocket[Any, Any, Any],
    app_id: FromPath[str],
    config: NamedDependency[Config],
) -> None:
    """Stream an app's logs to the detail page over a WebSocket.

    Sends the build-log tail, then follows the live container log (``podman logs
    --follow``) for the life of the connection, so the browser appends new lines
    instead of re-fetching the whole (possibly huge) log on a timer.
    """
    if not await verify_owner_ws(socket):
        return

    # Unlike a normal route, this handler stays open for the life of the stream, so we
    # must not leave a DB connection open across it — open one just to resolve the app
    # to a build-log path, then close it before we accept.
    with contextlib.closing(get_db()) as conn:
        try:
            app_row = resolve_app(app_id, conn)
        except AppError:
            # A WS handler can't return an HTTP error, so report the miss by
            # accepting and closing with an application close code the client reads.
            await socket.accept()
            await socket.close(code=4404, reason="App not found")
            return
    build_log_path = app_log_path(app_row["name"], config)

    await socket.accept()

    async def pump() -> None:
        try:
            async for chunk in stream_app_logs(app_id, build_log_path):
                await socket.send_text(chunk)
        except WebSocketDisconnect:
            return  # client vanished mid-send; watch_close tears the rest down
        except Exception:
            # An unexpected failure in the follow (e.g. tail hitting an unreadable
            # build log) would otherwise be swallowed by the teardown gather below,
            # closing the socket with no trace. Log it, then let pump finish so the
            # connection closes and the client reconnects.
            logger.exception("Streaming logs for app {} failed", app_id)
            return
        await asyncio.Event().wait()  # hold open; let the client decide when to refresh after a restart

    async def watch_close() -> None:
        # This is a send-only stream, so any inbound frame — or, more usually, a
        # disconnect — means the client is gone. Awaiting a receive lets us notice
        # promptly and reap the podman follow instead of leaking it until shutdown.
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await socket.receive()

    tasks = [asyncio.create_task(t) for t in (pump(), watch_close(), wait_for_shutdown())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        # Let the cancellations propagate into stream_app_logs' finally blocks so
        # the podman follow subprocess is terminated before we return.
        await asyncio.gather(*tasks, return_exceptions=True)


@post(
    "/stop_app/{app_id:str}",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ConflictException],
)
async def stop_app(app_id: FromPath[str], db: NamedDependency[sqlite3.Connection]) -> Response[OkResponse]:
    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    if is_removing(app_row):
        raise ConflictException(detail="App is being removed")

    stop_app_process(app_row["name"], app_row["container_id"])
    stop_container(f"openhost-{app_row['name']}")
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()
    return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)


def _reload_outcome_to_response(
    outcome: ReloadOutcome, continue_oauth: bool
) -> Response[OkResponse] | Response[UpdateReviewRequiredResponse] | Redirect:
    """Map a core ReloadOutcome onto the HTTP response the reload entry points return.
    OAuth-callback re-entries (``continue_oauth``) bounce browsers back to the detail page."""
    if outcome.kind == "oauth_required":
        if outcome.authorize_url is None:
            raise RuntimeError("oauth_required outcome without authorize_url")
        return Redirect(path=outcome.authorize_url)
    if outcome.kind == "review_required":
        if continue_oauth:
            return Redirect(path=f"/app_detail/{outcome.app_name}")
        if outcome.review is None:
            raise RuntimeError("review_required outcome without review")
        return Response(
            content=UpdateReviewRequiredResponse(
                ok=False,
                review_required=True,
                settings_changed=outcome.review.settings_changed,
                permissions_required=outcome.review.permissions_required,
                error="This update changes the app's settings; review and approve them before it can be applied.",
            ),
            status_code=200,
            media_type=MediaType.JSON,
        )
    if outcome.kind == "pull_failed" and continue_oauth:
        return Redirect(path=f"/app_detail/{outcome.app_name}")
    if outcome.kind in ("started", "no_repo", "pull_failed"):
        return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)
    raise RuntimeError(f"Unhandled reload outcome kind: {outcome.kind!r}")


@post(
    "/reload_app/{app_id:str}",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ConflictException, ServiceUnavailableException],
)
async def reload_app(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
    data: ReloadAppRequest = ReloadAppRequest(),  # noqa: B008 — Litestar resolves this at dependency-injection time
) -> Response[OkResponse] | Response[UpdateReviewRequiredResponse] | Redirect:
    """User-initiated reload, optionally pulling latest code via ``update``."""
    try:
        outcome = await core_reload_app(
            app_id,
            update=data.update,
            continue_oauth=False,
            approve_new_permissions=data.approve_new_permissions,
            db=db,
            config=config,
        )
    except AppError as exc:
        raise _to_http_error(exc) from exc
    return _reload_outcome_to_response(outcome, continue_oauth=False)


@get(
    "/reload_app/{app_id:str}",
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ConflictException, ServiceUnavailableException],
)
async def reload_app_after_oauth(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
    continue_oauth_update: FromQuery[bool] = False,
) -> Response[OkResponse] | Response[UpdateReviewRequiredResponse] | Redirect:
    """OAuth callback re-entry: the secrets app redirected the user back here
    after they granted GitHub access.  Resumes the update with ``continue_oauth=True``
    so we don't truncate the log file again or re-prompt for OAuth."""
    if not continue_oauth_update:
        # GET without the flag isn't meaningful; nudge the operator back to /app_detail.
        row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        if not row:
            return Redirect(path="/dashboard")
        return Redirect(path=f"/app_detail/{row['name']}")
    try:
        outcome = await core_reload_app(
            app_id,
            update=True,
            continue_oauth=True,
            approve_new_permissions=False,
            db=db,
            config=config,
        )
    except AppError as exc:
        raise _to_http_error(exc) from exc
    return _reload_outcome_to_response(outcome, continue_oauth=True)


@post(
    "/wipe_data_restart/{app_id:str}",
    status_code=202,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ConflictException, ServiceUnavailableException],
)
async def wipe_data_restart(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[OkResponse]:
    """Wipe all app data, including archive data, then rebuild the app in place."""
    try:
        core_wipe_data_restart(app_id, db, config)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    return Response(content=OkResponse(ok=True), status_code=202, media_type=MediaType.JSON)


@post(
    "/remove_app/{app_id:str}",
    status_code=202,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ServiceUnavailableException],
)
async def remove_app(
    app_id: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
    data: RemoveAppRequest = RemoveAppRequest(),  # noqa: B008 — body is optional; default = remove with keep_data=False
) -> Response[OkResponse] | Response[RemoveAppAlreadyRemoving]:
    """Flip the row to ``status='removing'`` and run teardown in a thread.

    Returns 202 immediately. The dashboard's /api/apps poll picks up
    the new status and renders 'Removing…' until the row is deleted,
    so reloading the page or opening a second tab still shows the
    in-flight state.
    """
    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc

    keep_data = data.keep_data

    # Refuse destructive remove on an unhealthy archive: rmtree of an
    # empty mountpoint would leave S3 bytes orphaned while the DB row
    # disappears. Must run before the atomic-claim UPDATE.
    if not keep_data and not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
            raise ServiceUnavailableException(
                detail=(
                    "Archive backend is not healthy; refusing to remove an archive-using app's data because the "
                    "S3-side bytes wouldn't actually be deleted.  Either restore the archive mount and retry, or use "
                    "keep_data=1 to remove the app while leaving its data in place."
                )
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
        logger.exception("Could not spawn remove worker for {}", app_id)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            (f"Could not start removal worker: {e}", app_id),
        )
        db.commit()
        raise ServiceUnavailableException(detail="Could not start removal worker; try again.") from e

    return Response(content=OkResponse(ok=True), status_code=202, media_type=MediaType.JSON)


@post(
    "/rename_app/{app_id:str}",
    status_code=200,
    guards=[require_owner_auth],
    raises=[
        ValidationException,
        NotFoundException,
        ConflictException,
        ServiceUnavailableException,
        InternalServerException,
    ],
)
async def rename_app(
    app_id: FromPath[str],
    data: RenameAppRequest,
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[RenameAppResponse]:
    """Rename an app's label and subdomain. The app_id (cross-table identity) stays the same."""
    new_name = data.name.strip()

    if not new_name:
        raise ValidationException(detail="Name is required")

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", new_name):
        raise ValidationException(detail="Name must be lowercase alphanumeric (hyphens allowed, not at start/end)")

    if f"/{new_name}" in RESERVED_PATHS:
        raise ValidationException(detail=f"Name '{new_name}' conflicts with a reserved path")

    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    if is_removing(app_row):
        raise ConflictException(detail="App is being removed")
    old_name = app_row["name"]

    # Refuse rename on an unhealthy archive ONLY if this app actually
    # uses the archive — otherwise the archive subdir would be silently
    # skipped while other tiers are renamed, orphaning the archive
    # contents under the old name.  Apps that don't use the archive
    # tier are unaffected and can rename freely on any backend state.
    if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
        if not archive_backend.is_archive_dir_healthy(config, db):
            raise ServiceUnavailableException(
                detail=(
                    "Archive backend is not healthy; refusing to rename "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                )
            )

    if new_name == old_name:
        return Response(content=RenameAppResponse(ok=True, name=new_name), status_code=200, media_type=MediaType.JSON)

    conflict = db.execute("SELECT name FROM apps WHERE name = ?", (new_name,)).fetchone()
    if conflict:
        raise ConflictException(detail=f"Name already in use by '{conflict['name']}'")

    prior_status = app_row["status"]
    prior_container_id = app_row["container_id"]
    was_running = prior_status in ("running", "starting", "building")
    stop_app_process(app_row["name"], prior_container_id)
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    # Off-loop because JuiceFS renames can take hundreds of ms.
    rename_error = await asyncio.to_thread(
        rename_app_storage_dirs,
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
            logger.error("Failed to restore status during rename rollback: {}", db_exc)
            rollback_db_error = str(db_exc)
        error_message = f"Failed to rename app data directories: {rename_error}"
        if rollback_db_error is not None:
            error_message += (
                f"; additionally, the DB-status rollback failed: "
                f"{rollback_db_error}.  Check the apps table; the row "
                f"for app_id={app_id!r} may be stuck at status='stopped'."
            )
        # Litestar masks `detail` on 500s, so the actionable message rides in `extra`.
        raise InternalServerException(detail="Failed to rename app data directories", extra={"output": error_message})

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
            logger.warning("Failed to restart {} after rename: {}", new_name, e)
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


@post(
    "/set_app_remote/{app_id:str}",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException, ConflictException],
)
async def set_app_remote(
    app_id: FromPath[str],
    data: SetAppRemoteRequest,
    db: NamedDependency[sqlite3.Connection],
) -> Response[SetAppRemoteResponse]:
    """Edit an app's git upstream (repo URL and/or ``@branch`` ref).

    Persists the new value to ``apps.repo_url``; the next ``Update & Reload``
    (``/reload_app`` with ``update=true``) re-points origin and checks out the
    pinned ref. Only git-backed apps can have an upstream — builtin apps copied
    from the apps/ directory have no ``.git`` to update.
    """
    repo_url = data.repo_url.strip()
    if not repo_url:
        raise ValidationException(detail="Repo URL is required")

    try:
        app_row = resolve_app(app_id, db)
    except AppError as exc:
        raise _to_http_error(exc) from exc
    if is_removing(app_row):
        raise ConflictException(detail="App is being removed")

    if not app_row["repo_path"] or not os.path.isdir(os.path.join(app_row["repo_path"], ".git")):
        raise ValidationException(detail="This app has no git repository, so its upstream cannot be edited.")

    # Normalise via parse_repo_url so a bare hostname gets an https:// scheme
    # and the stored value matches what git_pull will re-point origin to.
    try:
        base_url, ref = parse_repo_url(repo_url)
    except ValueError as e:
        raise ValidationException(detail=str(e)) from e
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
        app_logs_stream,
        stop_app,
        reload_app,
        wipe_data_restart,
        reload_app_after_oauth,
        remove_app,
        rename_app,
        set_app_remote,
    ],
)
