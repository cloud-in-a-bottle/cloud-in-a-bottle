"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from typing import Annotated

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException
from litestar.exceptions import InternalServerException
from litestar.exceptions import ValidationException
from litestar.params import Body

from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendState
from compute_space.core.domains import INTERRUPTED_APP_REMOVAL_MESSAGE
from compute_space.core.logging import logger
from compute_space.core.settings_store import ARCHIVE_MIGRATION_IN_PROGRESS_KEY
from compute_space.core.settings_store import ARCHIVE_MIGRATION_RECOVERY_REQUIRED_VALUE
from compute_space.core.settings_store import PRIMARY_DOMAIN_RESTART_APP_IDS_KEY
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.exceptions import ConflictException


@attr.s(auto_attribs=True, frozen=True)
class MetaDumpsSummary:
    count: int
    latest_at: str | None
    latest_key: str | None


@attr.s(auto_attribs=True, frozen=True)
class BackendStateResponse:
    """``BackendState`` for the dashboard — secret redacted, plus mount-derived paths."""

    backend: str
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    s3_prefix: str | None
    s3_access_key_id: str | None
    juicefs_volume_name: str
    configured_at: str | None
    state_message: str | None
    archive_dir: str | None
    meta_db_path: str
    meta_dumps: MetaDumpsSummary | None
    # On backend='local': the apps that currently have data in the local
    # archive dir.  Surfaced so the dashboard can tell the operator exactly
    # whose data an S3 upgrade will migrate.  Empty/omitted for other backends.
    local_archive_apps: list[str] = attr.Factory(list)


@attr.s(auto_attribs=True, frozen=True)
class TestConnectionOk:
    ok: bool  # always True


_archive_migration_tasks: set[asyncio.Task[None]] = set()


def _claim_archive_migration(db: sqlite3.Connection, operation_id: str) -> tuple[str | None, bool]:
    """Claim archive migration unless app recreation or another migration is active."""
    db.execute("BEGIN IMMEDIATE")
    try:
        pending_restarts = db.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (PRIMARY_DOMAIN_RESTART_APP_IDS_KEY,),
        ).fetchone()
        if pending_restarts is not None:
            db.execute("ROLLBACK")
            return "primary_domain_restarts", False
        existing = db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (ARCHIVE_MIGRATION_IN_PROGRESS_KEY,),
        ).fetchone()
        recovery_claim = existing is not None and existing[0] == ARCHIVE_MIGRATION_RECOVERY_REQUIRED_VALUE
        if existing is not None:
            if not recovery_claim:
                db.execute("ROLLBACK")
                return "archive_migration", False
        if not recovery_claim:
            busy_rows = db.execute(
                "SELECT status, error_message, manifest_raw FROM apps "
                "WHERE status IN ('building', 'starting', 'removing') "
                "OR (status = 'error' AND error_message = ?)",
                (INTERRUPTED_APP_REMOVAL_MESSAGE,),
            ).fetchall()
            if any(
                row["error_message"] == INTERRUPTED_APP_REMOVAL_MESSAGE
                or archive_backend.manifest_uses_archive(row["manifest_raw"] or "")
                for row in busy_rows
            ):
                db.execute("ROLLBACK")
                return "apps_busy", False
        if recovery_claim:
            claimed = db.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    operation_id,
                    ARCHIVE_MIGRATION_IN_PROGRESS_KEY,
                    ARCHIVE_MIGRATION_RECOVERY_REQUIRED_VALUE,
                ),
            )
        else:
            claimed = db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (ARCHIVE_MIGRATION_IN_PROGRESS_KEY, operation_id),
            )
        if claimed.rowcount != 1:
            db.execute("ROLLBACK")
            return "archive_migration", False
        db.execute("COMMIT")
        return None, recovery_claim
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _release_archive_migration(
    db: sqlite3.Connection,
    operation_id: str,
    *,
    recovery_required: bool = False,
) -> None:
    if recovery_required:
        db.execute(
            "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
            (
                ARCHIVE_MIGRATION_RECOVERY_REQUIRED_VALUE,
                ARCHIVE_MIGRATION_IN_PROGRESS_KEY,
                operation_id,
            ),
        )
    else:
        db.execute(
            "DELETE FROM settings WHERE key = ? AND value = ?",
            (ARCHIVE_MIGRATION_IN_PROGRESS_KEY, operation_id),
        )
    db.commit()


def _state_to_response(
    state: BackendState,
    archive_dir: str | None,
    meta_db_path: str,
    meta_dumps: MetaDumpsSummary | None,
    local_archive_apps: list[str] | None = None,
) -> BackendStateResponse:
    return BackendStateResponse(
        backend=state.backend,
        s3_bucket=state.s3_bucket,
        s3_region=state.s3_region,
        s3_endpoint=state.s3_endpoint,
        s3_prefix=state.s3_prefix,
        s3_access_key_id=state.s3_access_key_id,
        juicefs_volume_name=state.juicefs_volume_name,
        configured_at=state.configured_at,
        state_message=state.state_message,
        archive_dir=archive_dir,
        meta_db_path=meta_db_path,
        meta_dumps=meta_dumps,
        local_archive_apps=local_archive_apps or [],
    )


# JuiceFS volume-name regex (cmd/format.go validName); s3_prefix doubles
# as the volume name and so has to satisfy it.
_S3_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def _normalise_s3_prefix(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not _S3_PREFIX_RE.match(cleaned):
        raise ValueError(
            "s3_prefix must be 3-63 characters of [a-z0-9-] (lowercase only, "
            "no leading/trailing dash); it doubles as the JuiceFS volume name."
        )
    return cleaned


@attr.s(auto_attribs=True, frozen=True)
class TestConnectionRequest:
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ConfigureArchiveRequest:
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""
    juicefs_volume_name: str = ""
    # When the current backend is 'local' and apps have written archive
    # data, the operator must explicitly acknowledge the local->S3
    # migration.  The migration copies + verifies the data into S3 and is
    # fail-open (local data is kept if anything goes wrong), but the switch
    # to S3 is one-way and the local copy is removed afterwards, so we
    # require an explicit opt-in rather than doing it silently.
    confirm_migrate_local: bool = False
    # When the current backend is already 's3', configuring a new bucket
    # MIGRATES the archive from the old bucket to the new one (juicefs sync +
    # re-point) and, on success, reclaims the old bucket's objects.  It is
    # fail-open (old bucket kept intact if anything goes wrong), but it moves
    # live data between providers, so we require an explicit opt-in.
    confirm_migrate_s3: bool = False


@get("/api/storage/archive_backend", guards=[require_owner_auth])
async def get_archive_backend(
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> BackendStateResponse:
    """Return current archive-backend state (secret redacted) plus archive_dir, meta_db_path, meta_dumps."""
    state = archive_backend.read_state(db)
    # The archive tier is always the JuiceFS mountpoint (local file backend or
    # S3); only the legacy 'disabled' state has no mount.
    if state.backend in ("s3", "local"):
        archive_dir = archive_backend.juicefs_mount_dir(config)
    else:
        archive_dir = None
    meta_db_path = archive_backend.juicefs_meta_db_path(config)

    meta_dumps: MetaDumpsSummary | None = None
    if state.backend == "s3" and state.s3_bucket and state.s3_access_key_id and state.s3_secret_access_key:
        # Off-loop: list_objects_v2 does DNS + TLS + HTTP.
        summary = await asyncio.to_thread(
            archive_backend.list_meta_dumps,
            state.s3_bucket,
            state.s3_region,
            state.s3_endpoint,
            state.s3_access_key_id,
            state.s3_secret_access_key,
            state.juicefs_volume_name,
        )
        if summary is not None:
            meta_dumps = MetaDumpsSummary(
                count=summary.count,
                latest_at=summary.latest_at,
                latest_key=summary.latest_key,
            )
    local_apps = archive_backend.local_archive_apps_with_data(config, db) if state.backend == "local" else []
    return _state_to_response(state, archive_dir, meta_db_path, meta_dumps, local_apps)


@post(
    "/api/storage/archive_backend/test_connection",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, ClientException],
)
async def test_connection(
    data: Annotated[TestConnectionRequest, Body(media_type=MediaType.JSON)],
) -> Response[TestConnectionOk]:
    """Pre-flight S3 reachability/credentials check; doesn't touch the DB or live mount."""
    try:
        _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        raise ValidationException(detail=f"invalid s3_prefix: {exc}") from exc
    error = await asyncio.to_thread(
        archive_backend.test_s3_credentials,
        data.s3_bucket,
        data.s3_region.strip() or None,
        data.s3_endpoint.strip() or None,
        data.s3_access_key_id,
        data.s3_secret_access_key,
    )
    if error:
        raise ClientException(detail=error)
    return Response(content=TestConnectionOk(ok=True), status_code=200, media_type=MediaType.JSON)


@post(
    "/api/storage/archive_backend/configure",
    status_code=200,
    guards=[require_owner_auth],
    raises=[ValidationException, ConflictException, InternalServerException],
)
async def configure_archive_backend(
    data: Annotated[ConfigureArchiveRequest, Body(media_type=MediaType.JSON)],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[BackendStateResponse]:
    """One-shot S3 configure / re-configure.  Allowed from ``'local'`` (the
    default — migrates local archive data into the bucket), the legacy
    ``'disabled'`` state (fresh format), or ``'s3'`` (migrates the archive from
    the current bucket to a new bucket/provider)."""
    try:
        prefix = _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        raise ValidationException(detail=f"invalid s3_prefix: {exc}") from exc

    region = data.s3_region.strip() or None
    endpoint = data.s3_endpoint.strip() or None
    volume_name = data.juicefs_volume_name.strip() or None
    operation_id = uuid.uuid4().hex
    conflict, recovery_claim = _claim_archive_migration(db, operation_id)
    if conflict:
        if conflict == "primary_domain_restarts":
            raise ConflictException(
                detail="wait for apps to restart after the primary domain change",
                extra={"code": "primary_domain_restarts"},
            )
        if conflict == "apps_busy":
            raise ConflictException(
                detail="wait for current app operations to finish before migrating archive storage",
                extra={"code": "apps_busy"},
            )
        raise ConflictException(detail="archive migration is already in progress", extra={"code": conflict})

    try:
        state = archive_backend.read_state(db)

        # Guard the local->S3 migration behind an explicit acknowledgement when
        # there is actually local data to migrate. The state must be read after
        # claiming the migration marker so another migration cannot change the
        # required confirmation type between validation and execution.
        local_apps_with_data = (
            archive_backend.local_archive_apps_with_data(config, db) if state.backend == "local" else []
        )
        if local_apps_with_data and not data.confirm_migrate_local:
            raise ConflictException(
                detail=(
                    "The archive tier currently uses LOCAL disk and these "
                    f"apps have data on it: {', '.join(local_apps_with_data)}.  Configuring "
                    "S3 will migrate that data into the bucket and then "
                    "remove the local copy.  This switch is one-way.  "
                    "Re-submit with confirm_migrate_local=true to proceed."
                ),
                status_code=409,
                extra={"code": "confirm_migrate_local_required"},
            )

        if state.backend == "s3" and not data.confirm_migrate_s3:
            raise ConflictException(
                detail=(
                    "The archive tier is already on S3 (bucket "
                    f"{state.s3_bucket!r}).  Configuring a new bucket will MIGRATE the "
                    "archive to it (copy + verify), re-point the volume, and then reclaim "
                    "the old bucket's objects.  This is fail-open (the old bucket is kept "
                    "intact if anything fails), but it moves live data.  Re-submit with "
                    "confirm_migrate_s3=true to proceed."
                ),
                status_code=409,
                extra={"code": "confirm_migrate_s3_required"},
            )
    except BaseException:
        _release_archive_migration(db, operation_id, recovery_required=recovery_claim)
        raise

    # The format+mount steps can take 10-30s.  Run off-loop so the event
    # loop doesn't block.  ``db`` from ``provide_db()`` is request-thread-bound
    # by sqlite3's check_same_thread, so the worker opens its own
    # connection against the same DB file.
    db_path = config.db_path

    def _run() -> None:
        worker_db = sqlite3.connect(db_path)
        worker_db.row_factory = sqlite3.Row
        migration_succeeded = False
        try:
            # A migration (local->S3 or s3->s3) must restart the JuiceFS mount
            # (juicefs config re-points the volume) and must not let apps write
            # during the sync.  A fresh format from the legacy 'disabled' state
            # has no data + no live mount, so it needs no quiesce.
            migrating = archive_backend.read_state(worker_db).backend in ("local", "s3")
            # Imported lazily to avoid a core<-web import cycle.
            from compute_space.core.apps import start_apps_by_id  # noqa: PLC0415
            from compute_space.core.apps import stop_running_archive_apps  # noqa: PLC0415

            # For a migration the JuiceFS mount must be restarted (juicefs
            # config re-points the volume).  A running app container holding the
            # mount open would make the unmount time out and could keep writing
            # during the sync, so we STOP archive-using apps just before the
            # sync and record which ones, then RESTART them afterwards so they
            # re-open the (now migrated) archive.  The quiesce callback runs
            # inside configure_backend right before the sync.
            quiesced: list[str] = []

            def _quiesce() -> None:
                # Pass ``quiesced`` as ``stopped_out`` so already-stopped apps
                # are recorded even if the quiesce verification later raises
                # (the return value would be lost on that raise); the finally
                # block then restarts them.
                stop_running_archive_apps(worker_db, config, stopped_out=quiesced)

            try:
                archive_backend.configure_backend(
                    config,
                    worker_db,
                    s3_bucket=data.s3_bucket,
                    s3_region=region,
                    s3_endpoint=endpoint,
                    s3_prefix=prefix,
                    s3_access_key_id=data.s3_access_key_id,
                    s3_secret_access_key=data.s3_secret_access_key,
                    juicefs_volume_name=volume_name,
                    quiesce_archive_apps=_quiesce if migrating else None,
                )
                migration_succeeded = True
            finally:
                # Only restart the quiesced apps if the archive mount is
                # actually LIVE (either the new S3 mount after success, or the
                # restored local mount after fail-open).  If the mount is down
                # — e.g. a total failure where even the fail-open remount
                # couldn't bring it back — we must NOT restart them: their
                # containers would bind-mount the bare (unmounted) mountpoint
                # directory and any archive writes would be silently shadowed
                # once JuiceFS remounts over it on the next boot.  Leaving them
                # stopped is safe; attach_on_startup remounts and the operator
                # restarts the apps (state_message tells them to).
                if quiesced and archive_backend.is_mounted(archive_backend.juicefs_mount_dir(config)):
                    start_apps_by_id(quiesced, worker_db, config)
        finally:
            try:
                if recovery_claim and not migration_succeeded:
                    _release_archive_migration(worker_db, operation_id, recovery_required=True)
                elif not recovery_claim:
                    _release_archive_migration(worker_db, operation_id)
            finally:
                worker_db.close()

    migration_task = asyncio.create_task(asyncio.to_thread(_run))
    _archive_migration_tasks.add(migration_task)
    migration_task.add_done_callback(_archive_migration_tasks.discard)
    try:
        await asyncio.shield(migration_task)
    except asyncio.CancelledError:

        def _report_cancelled_migration(done: asyncio.Task[None]) -> None:
            try:
                done.result()
            except Exception:
                logger.exception("Archive migration failed after its request was cancelled")
                cleanup_db = sqlite3.connect(db_path)
                try:
                    _release_archive_migration(
                        cleanup_db,
                        operation_id,
                        recovery_required=recovery_claim,
                    )
                except Exception:
                    logger.exception("Failed to release archive migration claim after request cancellation")
                finally:
                    cleanup_db.close()
            else:
                if recovery_claim:
                    cleanup_db = sqlite3.connect(db_path)
                    try:
                        _release_archive_migration(cleanup_db, operation_id, recovery_required=True)
                    finally:
                        cleanup_db.close()

        migration_task.add_done_callback(_report_cancelled_migration)
        raise
    except RuntimeError as exc:
        _release_archive_migration(db, operation_id, recovery_required=recovery_claim)
        # 409 if it was a TOCTOU race against another configure attempt;
        # 500 for genuine bring-up failures (detail is masked, so it rides in extra).
        if "already configured" in str(exc):
            raise ConflictException(detail=str(exc), extra={"code": "already_configured"}) from exc
        raise InternalServerException(
            detail="Failed to configure archive backend", extra={"output": str(exc)}
        ) from exc
    except Exception:
        _release_archive_migration(db, operation_id, recovery_required=recovery_claim)
        raise

    if recovery_claim:
        app_recovery_succeeded = False
        try:
            from compute_space.core.startup import check_app_status  # noqa: PLC0415

            check_app_status(
                config,
                recover_quiesced_archive_apps=True,
                restart_synchronously=True,
            )
            app_recovery_succeeded = True
        except Exception:
            logger.exception("Archive repair succeeded but deferred app recovery failed")
        finally:
            _release_archive_migration(
                db,
                operation_id,
                recovery_required=not app_recovery_succeeded,
            )
        if not app_recovery_succeeded:
            raise InternalServerException(detail="Archive repaired, but deferred app recovery failed")

    state = archive_backend.read_state(db)
    archive_dir = archive_backend.juicefs_mount_dir(config) if state.backend == "s3" else None
    meta_db_path = archive_backend.juicefs_meta_db_path(config)
    return Response(content=_state_to_response(state, archive_dir, meta_db_path, meta_dumps=None), status_code=200)


api_archive_backend_routes = Router(
    path="/",
    route_handlers=[get_archive_backend, test_connection, configure_archive_backend],
)
