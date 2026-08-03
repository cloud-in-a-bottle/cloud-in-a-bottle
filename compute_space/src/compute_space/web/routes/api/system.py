import hashlib
import os
import secrets
import sqlite3
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import patch
from litestar import post

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.core.auth.auth import new_token_id
from compute_space.core.auth.scopes import SCOPE_CATALOG
from compute_space.core.auth.scopes import SYSTEM_ADMIN
from compute_space.core.auth.scopes import SYSTEM_READ
from compute_space.core.auth.scopes import TOKENS_MANAGE
from compute_space.core.auth.scopes import dump_scopes
from compute_space.core.auth.scopes import parse_scopes
from compute_space.core.auth.scopes import validate_requested_scopes
from compute_space.core.auth.security_audit import ListeningPort
from compute_space.core.auth.security_audit import external_ports
from compute_space.core.auth.security_audit import is_sshd_active
from compute_space.core.auth.security_audit import list_listening_ports
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.diagnostics import PlatformDiagnostics
from compute_space.core.diagnostics import collect_platform_diagnostics
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import is_dirty
from compute_space.core.logging import get_log_path
from compute_space.core.storage import is_guard_paused
from compute_space.core.storage import set_guard_paused
from compute_space.core.storage import storage_status
from compute_space.core.updates import is_shutdown_pending
from compute_space.web.auth.auth import require_scope

DEFAULT_TOKEN_EXPIRY_HOURS: float = 8.0


# ─── attrs models ──────────────────────────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class ApiToken:
    # token_id is the opaque public identity ("tok_…") used for delete/patch,
    # not the internal autoincrement row id (which is never exposed).
    token_id: str
    name: str
    expires_at: str | None
    created_at: str
    expired: bool
    scopes: list[str]


@attr.s(auto_attribs=True, frozen=True)
class ScopeInfo:
    """One entry of the scope catalog, surfaced by ``GET /api/token_scopes`` so
    the CLI and web UI render scope choices from a single server-side source."""

    name: str
    description: str
    owner_equivalent: bool


@attr.s(auto_attribs=True, frozen=True)
class CreateTokenRequest:
    """Body for ``POST /api/tokens``.  ``expiry_hours`` is a string so the
    sentinel ``"never"`` (= no expiry) and a numeric value share one field
    without forcing the JS client to send different keys.

    ``scopes`` restricts what the token may do and is REQUIRED — there is no
    implicit owner-on-omission (that would be a privilege hole).  A caller that
    wants full access must ask for it explicitly by passing ``["owner"]``."""

    name: str
    scopes: list[str]
    expiry_hours: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class UpdateTokenRequest:
    """Body for ``PATCH /api/tokens/{id}``: replace a token's scopes in place."""

    scopes: list[str]


@attr.s(auto_attribs=True, frozen=True)
class CreatedToken:
    token: str
    token_id: str
    name: str
    expires_at: str | None
    scopes: list[str]


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


@attr.s(auto_attribs=True, frozen=True)
class HealthOk:
    status: str  # "ok"


@attr.s(auto_attribs=True, frozen=True)
class HealthRestarting:
    status: str  # "restarting"


@attr.s(auto_attribs=True, frozen=True)
class ListeningPortsResponse:
    ports: list[ListeningPort]
    # True when the port list could not be enumerated at all (e.g. ``ss`` failed),
    # as opposed to no ports surviving the external-interface filter.
    enumeration_failed: bool


@attr.s(auto_attribs=True, frozen=True)
class ToggleStorageGuardRequest:
    paused: bool


@attr.s(auto_attribs=True, frozen=True)
class StorageGuardResponse:
    guard_paused: bool


@attr.s(auto_attribs=True, frozen=True)
class SshStatusResponse:
    ssh_enabled: bool


@attr.s(auto_attribs=True, frozen=True)
class DropCacheOk:
    ok: bool  # always True
    output: str


@attr.s(auto_attribs=True, frozen=True)
class VersionInfo:
    """Git info for the running openhost checkout. ``branch`` is None when HEAD is detached.
    ``sha`` is empty when the install isn't a git checkout (e.g. tarball deploys)."""

    branch: str | None
    sha: str
    short_sha: str
    dirty: bool


# ─── API Tokens ────────────────────────────────────────────────────────────


@get("/api/token_scopes", guards=[require_scope(TOKENS_MANAGE)])
async def api_token_scopes(db: sqlite3.Connection) -> list[ScopeInfo]:
    """The scope catalog, so the CLI and web UI render choices from one source."""
    return [
        ScopeInfo(name=s.name, description=s.description, owner_equivalent=s.owner_equivalent) for s in SCOPE_CATALOG
    ]


@get("/api/tokens", guards=[require_scope(TOKENS_MANAGE)])
async def api_tokens_list(db: sqlite3.Connection) -> list[ApiToken]:
    rows = db.execute(
        "SELECT token_id, name, expires_at, created_at, scopes FROM api_tokens ORDER BY created_at DESC"
    ).fetchall()
    now = datetime.now(UTC)
    tokens: list[ApiToken] = []
    for r in rows:
        has_expiry = bool(r["expires_at"])
        expired = has_expiry and datetime.fromisoformat(r["expires_at"]) < now
        tokens.append(
            ApiToken(
                token_id=r["token_id"],
                name=r["name"],
                expires_at=r["expires_at"] or None,
                created_at=r["created_at"],
                expired=expired,
                scopes=sorted(parse_scopes(r["scopes"])),
            )
        )
    return tokens


@post("/api/tokens", status_code=200, guards=[require_scope(TOKENS_MANAGE)])
async def api_tokens_create(
    data: CreateTokenRequest, db: sqlite3.Connection
) -> Response[CreatedToken] | Response[ErrorResponse]:
    name = data.name.strip() or "Untitled"
    # Scopes are required and validated — no implicit owner-on-omission.  A
    # caller wanting full access must pass ["owner"] explicitly.
    requested_scopes = data.scopes
    if error := validate_requested_scopes(requested_scopes):
        return Response(content=ErrorResponse(error=error), status_code=400)

    expiry_hours_raw = data.expiry_hours.strip() if data.expiry_hours else ""
    expires_at: datetime | None
    if not expiry_hours_raw or expiry_hours_raw.lower() == "never":
        expires_at = None
    else:
        try:
            expiry_hours = float(expiry_hours_raw)
        except ValueError:
            expiry_hours = DEFAULT_TOKEN_EXPIRY_HOURS
        if expiry_hours <= 0:
            return Response(content=ErrorResponse(error="Expiry must be positive"), status_code=400)
        expires_at = datetime.now(UTC) + timedelta(hours=expiry_hours)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_id = new_token_id()
    scopes_json = dump_scopes(requested_scopes)

    db.execute(
        "INSERT INTO api_tokens (token_id, name, token_hash, expires_at, scopes) VALUES (?, ?, ?, ?, ?)",
        (token_id, name, token_hash, expires_at.isoformat() if expires_at else "", scopes_json),
    )
    db.commit()

    return Response(
        content=CreatedToken(
            token=raw_token,
            token_id=token_id,
            name=name,
            expires_at=expires_at.isoformat() if expires_at else None,
            scopes=sorted(parse_scopes(scopes_json)),
        ),
        status_code=200,
        media_type=MediaType.JSON,
    )


@patch("/api/tokens/{token_id:str}", status_code=200, guards=[require_scope(TOKENS_MANAGE)])
async def api_tokens_update(
    token_id: str, data: UpdateTokenRequest, db: sqlite3.Connection
) -> Response[OkResponse] | Response[ErrorResponse]:
    """Replace a token's scopes in place (the "update later" capability)."""
    if error := validate_requested_scopes(data.scopes):
        return Response(content=ErrorResponse(error=error), status_code=400)
    cursor = db.execute(
        "UPDATE api_tokens SET scopes = ? WHERE token_id = ?",
        (dump_scopes(data.scopes), token_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        return Response(content=ErrorResponse(error="Token not found"), status_code=404)
    return Response(content=OkResponse(ok=True), status_code=200, media_type=MediaType.JSON)


@delete("/api/tokens/{token_id:str}", guards=[require_scope(TOKENS_MANAGE)])
async def api_tokens_delete(token_id: str, db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM api_tokens WHERE token_id = ?", (token_id,))
    db.commit()


# ─── Compute Space Logs ────────────────────────────────────────────────────


# The System page polls this every few seconds; serving the whole file (up to
# the 10 MB rotation limit) makes each poll take ~1s on a small VPS.
_LOG_TAIL_BYTES = 256 * 1024


@get("/api/compute_space_logs", guards=[require_scope(SYSTEM_READ)], media_type=MediaType.TEXT, sync_to_thread=False)
def compute_space_logs() -> Response[str]:
    """Return the tail (last 256 KiB) of the compute space log file."""
    log_path = get_log_path()
    if log_path is None:
        return Response(content="Log file not configured", status_code=503, media_type=MediaType.TEXT)
    with open(log_path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - _LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    if size > _LOG_TAIL_BYTES:
        text = text[text.find("\n") + 1 :]
    return Response(content=text, status_code=200, media_type=MediaType.TEXT)


# ─── Health & Security ─────────────────────────────────────────────────────


@get("/health", sync_to_thread=False)
def health() -> Response[HealthRestarting] | HealthOk:
    if is_shutdown_pending():
        return Response(content=HealthRestarting(status="restarting"), status_code=503)
    return HealthOk(status="ok")


@get("/api/listening-ports", guards=[require_scope(SYSTEM_READ)], sync_to_thread=False)
def listening_ports(db: sqlite3.Connection) -> ListeningPortsResponse:
    """Return TCP ports listening on external-facing or wildcard interfaces, with classification.

    Loopback-only listeners are excluded — they are not reachable from outside the VM.
    """
    all_ports = list_listening_ports(db=db)
    return ListeningPortsResponse(ports=external_ports(all_ports), enumeration_failed=not all_ports)


# sync_to_thread: walking app_data and querying podman take ~1s on a real
# instance; running on the event loop would stall every concurrent request.
@get("/api/storage-status", guards=[require_scope(SYSTEM_READ)], sync_to_thread=True)
def api_storage_status(config: Config) -> dict[str, object]:
    return storage_status(config)


@post("/api/storage-guard", status_code=200, guards=[require_scope(SYSTEM_ADMIN)], sync_to_thread=False)
def toggle_storage_guard(data: ToggleStorageGuardRequest) -> StorageGuardResponse:
    """Pause or resume the storage guard."""
    set_guard_paused(data.paused)
    return StorageGuardResponse(guard_paused=is_guard_paused())


# ─── SSH Toggle ────────────────────────────────────────────────────────────


@get("/api/ssh-status", guards=[require_scope(SYSTEM_READ)], sync_to_thread=False)
def ssh_status() -> SshStatusResponse:
    return SshStatusResponse(ssh_enabled=is_sshd_active())


@post("/toggle-ssh", status_code=200, guards=[require_scope(SYSTEM_ADMIN)], sync_to_thread=False)
def toggle_ssh() -> SshStatusResponse:
    if is_sshd_active():
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", "ssh.service", "sshd.service", "ssh.socket"],
            timeout=10,
        )
        subprocess.run(
            ["sudo", "-n", "systemctl", "mask", "ssh.service", "sshd.service", "ssh.socket"],
            timeout=10,
        )
        return SshStatusResponse(ssh_enabled=False)
    subprocess.run(
        ["sudo", "-n", "systemctl", "unmask", "ssh.service", "sshd.service", "ssh.socket"],
        timeout=10,
    )
    subprocess.run(["sudo", "-n", "systemctl", "start", "ssh.service"], timeout=10)
    return SshStatusResponse(ssh_enabled=is_sshd_active())


# ─── Router restart ────────────────────────────────────────────────────────


@post("/api/drop-docker-cache", status_code=200, guards=[require_scope(SYSTEM_ADMIN)], sync_to_thread=False)
def drop_docker_cache() -> Response[DropCacheOk] | Response[ErrorResponse]:
    """Drop the container build cache to free disk space."""
    try:
        output = drop_docker_build_cache()
    except RuntimeError as e:
        return Response(content=ErrorResponse(error=str(e)), status_code=500)
    return Response(content=DropCacheOk(ok=True, output=output), status_code=200, media_type=MediaType.JSON)


@get("/api/version", guards=[require_scope(SYSTEM_READ)])
async def api_version() -> VersionInfo:
    """Return git branch/SHA of the running openhost checkout.

    If openhost wasn't installed via git, sha/short_sha are empty and dirty is False.
    """
    try:
        sha = await get_head_sha(OPENHOST_PROJECT_DIR)
        branch = await get_branch_name(OPENHOST_PROJECT_DIR)
        dirty = await is_dirty(OPENHOST_PROJECT_DIR)
    except Exception:
        return VersionInfo(branch=None, sha="", short_sha="", dirty=False)
    return VersionInfo(branch=branch, sha=sha, short_sha=sha[:8], dirty=dirty)


# ─── Diagnostics ─────────────────────────────────────────────────────────


def _diagnostics_filename(zone_domain: str) -> str:
    """Build a safe, timestamped filename for a downloaded diagnostics bundle."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # zone_domain may contain a ':<port>' and dots; keep only filename-safe chars.
    safe_zone = "".join(c if c.isalnum() or c in "-." else "_" for c in zone_domain) or "openhost"
    return f"openhost-diagnostics-{safe_zone}-{stamp}.json"


@get("/api/diagnostics", guards=[require_scope(SYSTEM_READ)])
async def api_diagnostics(
    db: sqlite3.Connection, config: Config, download: bool = False
) -> Response[PlatformDiagnostics]:
    """Return a full instance diagnostics bundle for debugging.

    Includes the OpenHost git checkout, host OS/Python/dependency versions,
    container runtime info, disk usage, and a summary of every installed app.

    ``?download=1`` adds a Content-Disposition header so browsers save the JSON
    to a timestamped file instead of rendering it inline.
    """
    diagnostics = await collect_platform_diagnostics(db, config)
    headers = None
    if download:
        headers = {"Content-Disposition": f'attachment; filename="{_diagnostics_filename(diagnostics.zone_domain)}"'}
    return Response(content=diagnostics, status_code=200, media_type=MediaType.JSON, headers=headers)


@post("/restart_router", status_code=200, guards=[require_scope(SYSTEM_ADMIN)], sync_to_thread=False)
def restart_router() -> OkResponse:
    """Restart the router systemd service to pick up code changes."""
    subprocess.Popen(
        [
            "sudo",
            "-n",
            "bash",
            "-c",
            "systemctl kill --signal=SIGKILL openhost; systemctl start openhost",
        ],
        start_new_session=True,
    )
    return OkResponse(ok=True)


system_routes = Router(
    path="/",
    route_handlers=[
        api_token_scopes,
        api_tokens_list,
        api_tokens_create,
        api_tokens_update,
        api_tokens_delete,
        compute_space_logs,
        health,
        listening_ports,
        api_storage_status,
        toggle_storage_guard,
        ssh_status,
        toggle_ssh,
        drop_docker_cache,
        api_version,
        api_diagnostics,
        restart_router,
    ],
)
