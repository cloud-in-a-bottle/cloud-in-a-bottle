from __future__ import annotations

import asyncio
import sqlite3
from enum import StrEnum
from typing import Any

import attr
import bcrypt
from litestar import Request
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.params import FromQuery
from litestar.response import Redirect

from compute_space.config import Config
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.connect import ConnectError
from compute_space.core.connect import build_connect_url
from compute_space.core.connect import exchange_code_for_credential
from compute_space.core.domains import primary_domain
from compute_space.core.identity_store import get_connect_base_url
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.logging import logger
from compute_space.core.seamless_update import clear_update_token
from compute_space.core.seamless_update import new_update_token
from compute_space.core.seamless_update import persist_update_token
from compute_space.core.system_agent import SystemAgentError
from compute_space.core.system_agent import system_agent_apply
from compute_space.core.system_agent import system_agent_fetch
from compute_space.core.system_agent import system_agent_get_remote
from compute_space.core.system_agent import system_agent_set_remote
from compute_space.core.system_agent import system_agent_status
from compute_space.core.update_progress import read_progress
from compute_space.core.updates import trigger_restart
from compute_space.core.util import not_blank
from compute_space.web.auth.auth import require_owner_auth
from openhost_system_agent.protocol import RemoteInfo

# --- request / response types -----------------------------------------------


class UpdateState(StrEnum):
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    UP_TO_DATE = "UP_TO_DATE"
    ERROR = "ERROR"


_GIT_STATE_TO_UPDATE_STATE = {
    "UP_TO_DATE": UpdateState.UP_TO_DATE,
    "BEHIND_REMOTE": UpdateState.UPDATE_AVAILABLE,
    "DIRTY": UpdateState.UPDATE_AVAILABLE,
}

# Explanatory text shown to the owner for git states that need a heads-up
# beyond the generic "Updates available." message.
_GIT_STATE_NOTICE: dict[str, str] = {}


@attr.s(auto_attribs=True, frozen=True)
class SetRemoteRequest:
    url: str = attr.ib(validator=not_blank)


@attr.s(auto_attribs=True, frozen=True)
class SetOwnerUsernameRequest:
    username: str = attr.ib(validator=not_blank)


@attr.s(auto_attribs=True, frozen=True)
class CheckUpdatesResponse:
    state: str
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class ApplyUpdateResponse:
    # Token the browser carries to the detached updater during the downtime
    # window so it recognizes the owner's tab and streams live progress logs.
    token: str


@attr.s(auto_attribs=True, frozen=True)
class OwnerUsernameResponse:
    username: str | None


# --- routes -----------------------------------------------------------------


@get("/api/settings/get-remote", guards=[require_owner_auth])
async def get_remote() -> RemoteInfo:
    try:
        return await system_agent_get_remote()
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e


@post("/api/settings/set-remote", status_code=200, guards=[require_owner_auth])
async def set_remote(data: SetRemoteRequest) -> RemoteInfo:
    try:
        return await system_agent_set_remote(data.url.strip())
    except SystemAgentError as e:
        raise HTTPException(detail=str(e), status_code=500) from e


@get("/api/settings/update", guards=[require_owner_auth])
async def check_for_updates() -> CheckUpdatesResponse:
    try:
        fetch_result = await system_agent_fetch()
    except SystemAgentError as e:
        return CheckUpdatesResponse(state=UpdateState.ERROR, error=str(e))

    try:
        migration_status = await system_agent_status()
    except SystemAgentError as e:
        return CheckUpdatesResponse(state=UpdateState.ERROR, error=str(e))

    if not migration_status.ok and migration_status.reason == "behind":
        # A "behind" host just needs pending system migrations applied, which the
        # Update button does (it runs the same `openhost_system_agent update apply`
        # the CLI status message suggests). Surfacing that CLI-oriented message here
        # would wrongly tell the owner to SSH in, so we treat this like any other
        # available update and let the button handle it.
        return CheckUpdatesResponse(state=UpdateState.UPDATE_AVAILABLE)

    if not migration_status.ok:
        return CheckUpdatesResponse(state=UpdateState.ERROR, error=migration_status.message)

    state = _GIT_STATE_TO_UPDATE_STATE.get(fetch_result.state)
    if state is None:
        return CheckUpdatesResponse(state=UpdateState.ERROR, error=f"Unknown git state: {fetch_result.state}")

    return CheckUpdatesResponse(state=state, error=_GIT_STATE_NOTICE.get(fetch_result.state))


# Serializes apply_update: the agent checks out tags and runs migrations on a
# shared repo, so two concurrent applies would race. On success the agent
# restarts compute_space (killing this process), so the lock is only released
# on failure, leaving the host free to retry.
_apply_lock = asyncio.Lock()


async def _run_apply_and_restart() -> None:
    """Run the (blocking) agent apply. On success the agent restarts openhost,
    killing this process; only failures return here, where we clear the token so
    a later visitor doesn't see a stale, unrelated progress log.

    Invariant: this is only ever scheduled by apply_update AFTER it has acquired
    _apply_lock and handed ownership here, so this coroutine always holds the lock
    and must release it (directly, not via a locked() check) on the failure path."""
    try:
        await system_agent_apply()
    except SystemAgentError:
        logger.exception("system agent apply failed")
        await clear_update_token()
    finally:
        # Release the serialization lock so the owner can retry after a failure.
        # (On success this never runs — the process is gone.)
        _apply_lock.release()


@post("/api/settings/update", status_code=200, guards=[require_owner_auth])
async def apply_update() -> ApplyUpdateResponse:
    # Acquire without releasing on the happy path: the background apply task owns
    # the lock for the rest of this process's life and releases it only if the
    # apply fails (see _run_apply_and_restart). A locked() check gives the owner
    # a clear 409 instead of blocking.
    if _apply_lock.locked():
        raise HTTPException(detail="An update is already in progress.", status_code=409)
    await _apply_lock.acquire()

    # Ownership of the lock transfers to the background apply task only once it is
    # successfully scheduled. Until then, ANY error (expected or not) must release
    # it, or a single failed precheck would wedge updates forever.
    handed_off = False
    try:
        try:
            migration_status = await system_agent_status()
        except SystemAgentError as e:
            raise HTTPException(detail=str(e), status_code=500) from e

        if not migration_status.ok and migration_status.reason != "behind":
            raise HTTPException(detail=migration_status.message, status_code=409)

        # Mint + persist the token BEFORE kicking off the apply so the updater
        # (launched by the agent at the end of the walk) can match the owner's tab
        # the instant it comes up. Persisting runs via the root agent so it works
        # regardless of updater-dir ownership. The apply then runs as a background
        # task so this response — carrying the token — reaches the browser before
        # the restart kills this process.
        token = new_update_token()
        await persist_update_token(token)
        asyncio.ensure_future(_run_apply_and_restart())
        handed_off = True
        return ApplyUpdateResponse(token=token)
    finally:
        if not handed_off and _apply_lock.locked():
            _apply_lock.release()


@attr.s(auto_attribs=True, frozen=True)
class UpdateProgressResponse:
    entries: list[dict[str, Any]]
    terminal: bool


@get("/updates", guards=[require_owner_auth])
async def update_progress() -> UpdateProgressResponse:
    # Served by compute_space while it is UP during the (long) apply phase, so the
    # /updating page streams live progress before the brief final restart. The
    # detached updater serves the SAME path+shape during that restart. Owner-authed
    # here (cookie); the updater authenticates via the URL token instead.
    view = read_progress()
    return UpdateProgressResponse(entries=view.entries, terminal=view.terminal)


@post("/api/settings/restart_compute_space", status_code=204, guards=[require_owner_auth])
async def restart_compute_space() -> None:
    trigger_restart()


# --- Connect to Imbue -------------------------------------------------------


@attr.s(auto_attribs=True, frozen=True)
class ConnectStatusResponse:
    # Whether the "Connect to Imbue" button should be shown (an Imbue URL is
    # configured) and whether this instance already holds a credential.
    available: bool
    connected: bool


@attr.s(auto_attribs=True, frozen=True)
class ConnectStartResponse:
    # The Imbue authorization URL the browser should be sent to.
    redirect_url: str


@get("/api/settings/connect-imbue/status", guards=[require_owner_auth])
async def connect_imbue_status(
    config: NamedDependency[Config], db: NamedDependency[sqlite3.Connection]
) -> ConnectStatusResponse:
    return ConnectStatusResponse(
        available=bool(get_connect_base_url(db)),
        connected=get_instance_identity(db, config) is not None,
    )


@post("/api/settings/connect-imbue/start", status_code=200, guards=[require_owner_auth])
async def connect_imbue_start(
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
    request: Request[Any, Any, Any],
) -> ConnectStartResponse:
    frontend = get_connect_base_url(db)
    if not frontend:
        raise HTTPException(detail="Connect to Imbue is not available on this deployment", status_code=503)
    # The one-time code is returned to this instance's own https origin, so derive
    # it from the request (honoring proxy headers).
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    zone = primary_domain(db).name
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or zone
    instance_base = f"{scheme}://{host}"
    redirect_url = build_connect_url(frontend, zone, instance_base)
    return ConnectStartResponse(redirect_url=redirect_url)


@get("/api/settings/connect-imbue/callback", guards=[require_owner_auth], sync_to_thread=True)
def connect_imbue_callback(
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
    code: FromQuery[str] = "",
) -> Redirect:
    """Exchange the one-time code and store the credential.

    Imbue returns the one-time code here (?code=). We exchange it for the credential
    and write it into the DB settings table, which is read live by the services
    that use it — so no restart is needed. Redirects back to Settings.

    Sync + sync_to_thread so the blocking exchange (httpx) doesn't stall the event
    loop for other (proxied app) traffic.
    """
    frontend = get_connect_base_url(db)
    if not frontend:
        raise HTTPException(detail="Connect to Imbue is not available on this deployment", status_code=503)
    if not code.strip():
        return Redirect(path="/settings?connect=error")
    try:
        credential = exchange_code_for_credential(frontend, code.strip())
        set_instance_identity(db, credential)
    except ConnectError as e:
        raise HTTPException(detail=str(e), status_code=502) from e
    return Redirect(path="/settings?connect=ok")


@attr.s(auto_attribs=True, frozen=True)
class ChangePasswordRequest:
    current_password: str
    new_password: str
    confirm_password: str


@attr.s(auto_attribs=True, frozen=True)
class ChangePasswordResponse:
    ok: bool


@post("/api/settings/change_password", status_code=200, guards=[require_owner_auth])
async def change_password(
    data: ChangePasswordRequest, db: NamedDependency[sqlite3.Connection]
) -> ChangePasswordResponse:
    current = data.current_password.strip()
    new_pw = data.new_password.strip()
    confirm = data.confirm_password.strip()

    if not current or not new_pw:
        raise HTTPException(detail="All fields required", status_code=400)
    if new_pw != confirm:
        raise HTTPException(detail="Passwords do not match", status_code=400)
    if len(new_pw) < 8:
        raise HTTPException(detail="Password must be at least 8 characters", status_code=400)

    row = db.execute("SELECT user_id, password_hash FROM users LIMIT 1").fetchone()
    if not row:
        raise HTTPException(detail="No owner found", status_code=404)

    if not bcrypt.checkpw(current.encode(), row["password_hash"].encode()):
        raise HTTPException(detail="Current password is incorrect", status_code=403)

    new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE user_id = ?",
        (new_hash, row["user_id"]),
    )
    db.commit()

    return ChangePasswordResponse(ok=True)


@get("/api/settings/owner_username", guards=[require_owner_auth])
async def get_owner_username(db: NamedDependency[sqlite3.Connection]) -> OwnerUsernameResponse:
    return OwnerUsernameResponse(username=read_owner_username(db))


@post("/api/settings/owner_username", status_code=200, guards=[require_owner_auth])
async def set_owner_username(
    data: SetOwnerUsernameRequest, db: NamedDependency[sqlite3.Connection]
) -> OwnerUsernameResponse:
    error = validate_owner_username(data.username)
    if error is not None:
        raise HTTPException(detail=error, status_code=400)

    try:
        update_owner_username(db, data.username)
        db.commit()
    except ValueError as e:
        raise HTTPException(detail=str(e), status_code=400) from e
    except sqlite3.Error as e:
        raise HTTPException(detail=f"database error: {e}", status_code=500) from e

    return OwnerUsernameResponse(username=data.username)


api_settings_routes = Router(
    path="/",
    route_handlers=[
        get_remote,
        set_remote,
        check_for_updates,
        apply_update,
        update_progress,
        restart_compute_space,
        connect_imbue_status,
        connect_imbue_start,
        connect_imbue_callback,
        change_password,
        get_owner_username,
        set_owner_username,
    ],
)
