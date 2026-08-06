"""In-process dispatch for the OpenHost *platform* service.

Generalizes the installer pattern: apps consume ``PLATFORM_SERVICE_URL`` via a
normal ``[[services.v2.consumes]]`` block and call
``/api/services/v2/call/<shortname>/...``; the router runs these handlers
in-process (sharing its DB + ``apps.*`` state) instead of proxying to a provider
app, and gates each on ``permissions_v2`` grants.

Endpoints (rooted under the consumer's platform shortname):

    POST /deploy                     {repo_url, app_name?}      cap: deploy
    GET  /apps                                                  cap: manage_apps
    GET  /apps/<app_id>/status                                  cap: manage_apps (scoped)
    GET  /apps/<app_id>/logs                                    cap: manage_apps (scoped)
    POST /apps/<app_id>/stop                                    cap: manage_apps (scoped)
    POST /apps/<app_id>/start                                   cap: manage_apps (scoped)
    POST /apps/<app_id>/remove       {keep_data?}               cap: manage_apps (scoped)
    GET  /system                                                cap: system_read
    POST /delegate  {app_id, service, grant}                     cap: delegate_permissions

All permission checks fail closed and are enforced here by the router (the
platform has no provider app to defer to).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from threading import Thread
from typing import Any

from litestar import MediaType
from litestar import Request
from litestar import Response
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.core.apps import remove_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.security_audit import external_ports
from compute_space.core.auth.security_audit import list_listening_ports
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import stop_app_process
from compute_space.core.containers import stop_container
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import is_dirty
from compute_space.core.installer import InstallError
from compute_space.core.installer import install_from_repo_url
from compute_space.core.logging import get_log_path
from compute_space.core.logging import logger
from compute_space.core.platform_service import CAP_DELEGATE_PERMISSIONS
from compute_space.core.platform_service import CAP_DEPLOY
from compute_space.core.platform_service import CAP_MANAGE_APPS
from compute_space.core.platform_service import CAP_SYSTEM_READ
from compute_space.core.platform_service import GRANT_KEY_CAPABILITY
from compute_space.core.platform_service import GRANT_KEY_REPO_URL_PREFIX
from compute_space.core.platform_service import GRANT_KEY_TARGET
from compute_space.core.platform_service import PLATFORM_SERVICE_URL
from compute_space.core.platform_service import PLATFORM_SERVICE_VERSION
from compute_space.core.platform_service import TARGET_OWN
from compute_space.core.platform_service import check_delegation_allowed
from compute_space.core.platform_service import check_deploy_allowed
from compute_space.core.platform_service import has_capability
from compute_space.core.platform_service import resolve_manage_scope
from compute_space.core.services_v2 import build_grant_approval_url
from compute_space.core.storage import storage_status


def _json_error(error: str, message: str, status: int) -> Response[dict[str, Any]]:
    return Response(content={"error": error, "message": message}, status_code=status, media_type=MediaType.JSON)


def _json_ok(body: dict[str, Any]) -> Response[dict[str, Any]]:
    return Response(content=body, status_code=200, media_type=MediaType.JSON)


def _permission_denied(
    message: str,
    *,
    consumer_app_id: str | None = None,
    proposed_grant: Any = None,
    db: sqlite3.Connection | None = None,
) -> Response[dict[str, Any]]:
    """403 with a machine-readable ``permission_required`` shape (parallels the
    installer's denial body so clients can handle both uniformly).

    When a concrete ``proposed_grant`` (plus ``consumer_app_id`` and ``db``) is
    supplied, decorate the body with a ``required_grant`` carrying a one-click
    owner-approval ``grant_url`` for the platform service — so a denied app can
    prompt the owner to approve exactly the grant it needs.
    """
    body: dict[str, Any] = {"error": "permission_required", "message": message}
    if proposed_grant is not None and consumer_app_id is not None and db is not None:
        body["required_grant"] = {
            "grant": proposed_grant,
            "scope": "global",
            "grant_url": build_grant_approval_url(consumer_app_id, PLATFORM_SERVICE_URL, proposed_grant, db),
        }
    return Response(content=body, status_code=403, media_type=MediaType.JSON)


def _platform_grants(consumer_app_id: str) -> list[Any]:
    """The caller's grant payloads for the platform service."""
    return [gp.grant for gp in get_granted_permissions_v2(consumer_app_id, PLATFORM_SERVICE_URL)]


# Proposed grants offered to the owner on a denial, so a denied app gets a
# one-click approval link for the least-privilege grant that would unblock it.
_GRANT_MANAGE_OWN = {GRANT_KEY_CAPABILITY: CAP_MANAGE_APPS, GRANT_KEY_TARGET: TARGET_OWN}
_GRANT_SYSTEM_READ = {GRANT_KEY_CAPABILITY: CAP_SYSTEM_READ}
_GRANT_DELEGATE = {GRANT_KEY_CAPABILITY: CAP_DELEGATE_PERMISSIONS}


def _version_ok(version_spec: str) -> Response[dict[str, Any]] | None:
    try:
        spec = SpecifierSet(version_spec)
    except InvalidSpecifier:
        return _json_error("bad_request", f"Invalid version specifier: {version_spec}", 400)
    if Version(PLATFORM_SERVICE_VERSION) not in spec:
        return _json_error(
            "service_not_available",
            f"platform version {PLATFORM_SERVICE_VERSION} does not match {version_spec}",
            503,
        )
    return None


async def handle_platform_request(
    consumer_app_id: str,
    version_spec: str,
    rest: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> Response[Any]:
    """Route a platform-service call to the right handler after a version check."""
    if (version_err := _version_ok(version_spec)) is not None:
        return version_err

    method = str(request.method)
    parts = [p for p in rest.strip("/").split("/") if p != ""]

    if method == "POST" and parts == ["deploy"]:
        return await _handle_deploy(consumer_app_id, request, db, config)

    if method == "GET" and parts == ["apps"]:
        return _handle_list_apps(consumer_app_id, db)

    if method == "GET" and parts == ["system"]:
        return await _handle_system(consumer_app_id, db, config)

    if method == "POST" and parts == ["delegate"]:
        return await _handle_delegate(consumer_app_id, request, db)

    # /apps/<app_id>/<action>
    if len(parts) == 3 and parts[0] == "apps":
        _, app_id, action = parts
        return await _handle_app_action(consumer_app_id, app_id, action, method, request, db, config)

    return _json_error("bad_request", f"Unknown platform endpoint: {method} /{rest.lstrip('/')}", 404)


# ── deploy ───────────────────────────────────────────────────────────────────


async def _handle_deploy(
    consumer_app_id: str, request: Request[Any, Any, Any], db: sqlite3.Connection, config: Config
) -> Response[Any]:
    try:
        body = await request.json()
    except Exception:
        return _json_error("bad_request", "request body must be a JSON object", 400)
    if not isinstance(body, dict):
        return _json_error("bad_request", "request body must be a JSON object", 400)
    repo_url = (body.get("repo_url") or "").strip()
    if not repo_url:
        return _json_error("bad_request", "repo_url is required", 400)
    app_name = (body.get("app_name") or "").strip() or None

    grants = _platform_grants(consumer_app_id)
    if (reason := check_deploy_allowed(repo_url, grants)) is not None:
        # Propose a deploy grant covering exactly the requested repo_url so the
        # owner can approve it in one click.
        proposed = {GRANT_KEY_CAPABILITY: CAP_DEPLOY, GRANT_KEY_REPO_URL_PREFIX: repo_url}
        return _permission_denied(reason, consumer_app_id=consumer_app_id, proposed_grant=proposed, db=db)

    try:
        # Stamp installed_by with the caller so "manage apps I deployed" and
        # non-escalating delegation can key on provenance.
        result = await install_from_repo_url(repo_url, config, db, app_name=app_name, installed_by=consumer_app_id)
    except InstallError as exc:
        return _json_error("deploy_failed", exc.message, exc.status_code)

    # Return the new app's id so the caller can immediately manage/delegate to it.
    row = db.execute("SELECT app_id FROM apps WHERE name = ?", (result.app_name,)).fetchone()
    new_app_id = row["app_id"] if row else None
    return _json_ok({"ok": True, "app_name": result.app_name, "app_id": new_app_id, "status": result.status})


# ── manage: list / status / logs / stop / start / remove ─────────────────────


def _handle_list_apps(consumer_app_id: str, db: sqlite3.Connection) -> Response[Any]:
    scope = resolve_manage_scope(_platform_grants(consumer_app_id))
    if not (scope.all_apps or scope.own_apps or scope.app_ids):
        return _permission_denied(
            "no manage_apps grant present", consumer_app_id=consumer_app_id, proposed_grant=_GRANT_MANAGE_OWN, db=db
        )
    rows = db.execute("SELECT app_id, name, status, error_message, installed_by FROM apps ORDER BY name").fetchall()
    apps = [
        {"app_id": r["app_id"], "name": r["name"], "status": r["status"], "error": r["error_message"]}
        for r in rows
        if scope.allows(app_id=r["app_id"], installed_by=r["installed_by"], caller_app_id=consumer_app_id)
    ]
    return _json_ok({"apps": apps})


def _load_manageable_app(
    consumer_app_id: str, app_id: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response[dict[str, Any]] | None]:
    """Load an app row and authorize the caller's manage scope against it.

    Returns (row, None) if allowed; (None, error_response) otherwise.

    Existence-leak rule: a missing app and an out-of-reach app return the SAME
    403 for any caller that cannot already see the whole instance.  Only an
    ``all``-scope caller — which can enumerate every app anyway — gets a 404 for
    a genuinely missing id.  In particular an ``own``-only (or specific-app_id)
    caller can never distinguish "doesn't exist" from "exists but not mine", so
    it can't probe arbitrary app_ids.
    """
    scope = resolve_manage_scope(_platform_grants(consumer_app_id))
    if not (scope.all_apps or scope.own_apps or scope.app_ids):
        return None, _permission_denied(
            "no manage_apps grant present", consumer_app_id=consumer_app_id, proposed_grant=_GRANT_MANAGE_OWN, db=db
        )
    row = db.execute(
        "SELECT app_id, name, status, error_message, container_id, installed_by FROM apps WHERE app_id = ?",
        (app_id,),
    ).fetchone()
    if row is None or not scope.allows(
        app_id=row["app_id"], installed_by=row["installed_by"], caller_app_id=consumer_app_id
    ):
        # An ``all`` caller already sees every app, so a missing id is a plain
        # 404 for it.  Everyone else gets a uniform 403 whether the app is
        # missing or simply out of reach, so existence can't be probed.
        if row is None and scope.all_apps:
            return None, _json_error("not_found", f"app {app_id!r} not found", 404)
        return None, _permission_denied(f"{consumer_app_id} may not manage {app_id!r}")
    return row, None


async def _handle_app_action(
    consumer_app_id: str,
    app_id: str,
    action: str,
    method: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> Response[Any]:
    row, denied = _load_manageable_app(consumer_app_id, app_id, db)
    if denied is not None:
        return denied
    assert row is not None

    if method == "GET" and action == "status":
        return _json_ok({"app_id": app_id, "status": row["status"], "error": row["error_message"]})

    if method == "GET" and action == "logs":
        logs = get_docker_logs(row["name"], config.temporary_data_dir, row["container_id"])
        return Response(content=logs, status_code=200, media_type="text/plain; charset=utf-8")

    if method == "POST" and action == "stop":
        stop_app_process(row)
        stop_container(f"openhost-{row['name']}")
        db.execute("UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?", (app_id,))
        db.commit()
        return _json_ok({"ok": True, "app_id": app_id, "status": "stopped"})

    if method == "POST" and action == "start":
        try:
            start_app_process(app_id, db, config)
        except (RuntimeError, ValueError) as exc:
            return _json_error("start_failed", str(exc), 400)
        return _json_ok({"ok": True, "app_id": app_id, "status": "starting"})

    if method == "POST" and action == "remove":
        keep_data = False
        try:
            body = await request.json()
            if isinstance(body, dict):
                keep_data = bool(body.get("keep_data", False))
        except Exception:
            keep_data = False
        # Atomic claim mirrors /remove_app: only the first request flips to
        # 'removing' and spawns the worker.
        cursor = db.execute(
            "UPDATE apps SET status = 'removing', error_message = NULL WHERE app_id = ? AND status != 'removing'",
            (app_id,),
        )
        db.commit()
        if cursor.rowcount == 0:
            return _json_ok({"ok": True, "app_id": app_id, "already_removing": True})
        try:
            Thread(target=remove_app_background, args=(app_id, keep_data, config), daemon=True).start()
        except Exception as exc:
            logger.exception("platform-service: could not spawn remove worker for %s", app_id)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                (f"Could not start removal worker: {exc}", app_id),
            )
            db.commit()
            return _json_error("remove_failed", "could not start removal worker; try again", 503)
        return _json_ok({"ok": True, "app_id": app_id, "status": "removing"})

    return _json_error("bad_request", f"Unknown app action: {method} {action}", 404)


# ── system_read ──────────────────────────────────────────────────────────────


# Cap the platform-logs tail we hand back so a system_read caller can't pull the
# whole (up to 10 MB) rotated log in one request.
_LOG_TAIL_BYTES = 64 * 1024


def _read_log_tail() -> str | None:
    """Return the tail of the compute-space log, or None if not configured."""
    log_path = get_log_path()
    if log_path is None:
        return None
    try:
        with open(log_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
        if size > _LOG_TAIL_BYTES:
            text = text[text.find("\n") + 1 :]
        return text
    except OSError:
        return None


async def _version_info() -> dict[str, Any]:
    """Git branch/SHA of the running openhost checkout (empty if not a checkout)."""
    try:
        sha = await get_head_sha(OPENHOST_PROJECT_DIR)
        branch = await get_branch_name(OPENHOST_PROJECT_DIR)
        dirty = await is_dirty(OPENHOST_PROJECT_DIR)
    except Exception:
        return {"branch": None, "sha": "", "short_sha": "", "dirty": False}
    return {"branch": branch, "sha": sha, "short_sha": sha[:8], "dirty": dirty}


async def _handle_system(consumer_app_id: str, db: sqlite3.Connection, config: Config) -> Response[Any]:
    """Read-only system info: version, storage, external listening ports, log tail."""
    if not has_capability(_platform_grants(consumer_app_id), CAP_SYSTEM_READ):
        return _permission_denied(
            "no system_read grant present", consumer_app_id=consumer_app_id, proposed_grant=_GRANT_SYSTEM_READ, db=db
        )

    version = await _version_info()
    # Off-loop: storage snapshot + ss/podman walks are blocking.
    storage = await asyncio.to_thread(storage_status, config)
    all_ports = await asyncio.to_thread(list_listening_ports, db)
    # ListeningPort is a TypedDict, so it's already JSON-serializable.
    ports = list(external_ports(all_ports))
    logs_tail = await asyncio.to_thread(_read_log_tail)

    return _json_ok(
        {
            "version": version,
            "storage": storage,
            "listening_ports": ports,
            "ports_enumeration_failed": not all_ports,
            "logs_tail": logs_tail,
        }
    )


# ── delegate (non-escalating) ────────────────────────────────────────────────


async def _handle_delegate(
    consumer_app_id: str, request: Request[Any, Any, Any], db: sqlite3.Connection
) -> Response[Any]:
    """Grant one of the caller's OWN permissions to an app the caller deployed.

    Body: {app_id, service, grant}.  Enforces all delegation gates: the caller
    must hold ``delegate_permissions``, must already hold a grant with this
    payload for ``service``, and the target app must be one the caller deployed
    (installed_by == caller).  The child receives the caller's grant at the
    caller's own scope/provider (never broader), so this is the non-escalating
    "make copies of my privileges" primitive.  The scope is inherited from the
    caller's grant, not taken from the request.
    """
    try:
        body = await request.json()
    except Exception:
        return _json_error("bad_request", "request body must be a JSON object", 400)
    if not isinstance(body, dict):
        return _json_error("bad_request", "request body must be a JSON object", 400)

    target_app_id = (body.get("app_id") or "").strip()
    service = (body.get("service") or "").strip()
    grant = body.get("grant")
    if not target_app_id or not service or grant is None:
        return _json_error("bad_request", "app_id, service, and grant are required", 400)

    # Check the delegate capability FIRST, before touching the apps table, so a
    # caller without delegate_permissions can't probe app existence via the
    # 404-vs-403 distinction.
    caller_platform_grants = _platform_grants(consumer_app_id)
    if not has_capability(caller_platform_grants, CAP_DELEGATE_PERMISSIONS):
        return _permission_denied(
            "caller lacks the delegate_permissions capability",
            consumer_app_id=consumer_app_id,
            proposed_grant=_GRANT_DELEGATE,
            db=db,
        )

    # The target must be an app THIS caller deployed.  A missing app and an app
    # the caller didn't deploy return the SAME 403 so a caller can't probe which
    # arbitrary app_ids exist (it can only ever act on apps it deployed).
    row = db.execute("SELECT installed_by FROM apps WHERE app_id = ?", (target_app_id,)).fetchone()
    if row is None or row["installed_by"] != consumer_app_id:
        return _permission_denied(f"{consumer_app_id} may not delegate to {target_app_id!r}")

    # Remaining delegation gate: the caller must already hold a grant with this
    # payload for the service.  The decision carries the caller's own matching
    # grant so we copy its EXACT scope/provider to the child — never broader.
    caller_grants_for_service = get_granted_permissions_v2(consumer_app_id, service)
    decision = check_delegation_allowed(
        caller_platform_grants=caller_platform_grants,
        caller_grants_for_target_service=caller_grants_for_service,
        grant_to_delegate=grant,
    )
    if decision.reason is not None or decision.granted is None:
        return _permission_denied(decision.reason or "delegation not allowed")

    held = decision.granted
    grant_permission_v2(
        target_app_id,
        service,
        held.grant,
        scope=held.scope,
        provider_app_id=held.provider_app_id,
    )
    return _json_ok(
        {
            "ok": True,
            "app_id": target_app_id,
            "service": service,
            "grant": held.grant,
            "scope": held.scope,
        }
    )
