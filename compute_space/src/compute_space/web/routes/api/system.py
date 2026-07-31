import hashlib
import os
import secrets
import sqlite3
import subprocess
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import attr
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.exceptions import NotAuthorizedException

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.core.auth.security_audit import ListeningPort
from compute_space.core.auth.security_audit import external_ports
from compute_space.core.auth.security_audit import is_sshd_active
from compute_space.core.auth.security_audit import list_listening_ports
from compute_space.core.containers import drop_docker_build_cache
from compute_space.core.diagnostics import PlatformDiagnostics
from compute_space.core.diagnostics import collect_platform_diagnostics
from compute_space.core.domains import primary_domain
from compute_space.core.email.enablement import email_enabled
from compute_space.core.email.relay_credential import RelayCredentialError
from compute_space.core.email.relay_credential import RelayCredentialProvider
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import is_dirty
from compute_space.core.logging import get_log_path
from compute_space.core.logging import logger
from compute_space.core.storage import is_guard_paused
from compute_space.core.storage import set_guard_paused
from compute_space.core.storage import storage_status
from compute_space.core.updates import is_shutdown_pending
from compute_space.web.auth.auth import require_app_auth
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import verify_app_auth

DEFAULT_TOKEN_EXPIRY_HOURS: float = 8.0


# Process-wide relay-credential provider (caches the frontend-fetched credential
# with a short TTL). Keyed on the config values that determine the credential
# rather than on id(config): provide_config() hands each request a freshly-built
# Config object, so an id()-based key would never hit — defeating the TTL cache
# (refetching from the frontend on every request) and leaking a provider per
# request. The identity/zone the credential also depends on are sourced live from
# the DB and keyed inside the provider itself (RelayCredentialProvider.get).
_relay_providers: dict[tuple[object, ...], RelayCredentialProvider] = {}
_relay_providers_lock = threading.Lock()


def _relay_provider_key(config: Config) -> tuple[object, ...]:
    """A stable key over the config fields the relay credential depends on."""
    return (
        config.email_proxy_base_url,
        config.email_custom_domain_normalized,
    )


def get_relay_credential_provider(config: Config) -> RelayCredentialProvider:
    key = _relay_provider_key(config)
    with _relay_providers_lock:
        provider = _relay_providers.get(key)
        if provider is None:
            provider = RelayCredentialProvider(config=config)
            _relay_providers[key] = provider
        return provider


# ─── attrs models ──────────────────────────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class ApiToken:
    id: int
    name: str
    expires_at: str | None
    created_at: str
    expired: bool


@attr.s(auto_attribs=True, frozen=True)
class CreateTokenRequest:
    """Body for ``POST /api/tokens``.  ``expiry_hours`` is a string so the
    sentinel ``"never"`` (= no expiry) and a numeric value share one field
    without forcing the JS client to send different keys."""

    name: str
    expiry_hours: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class CreatedToken:
    token: str
    name: str
    expires_at: str | None


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
class EmailRelayConfigResponse:
    """SMTP smarthost relay config the mailbox app uses to send outbound mail.

    Returned only to the mailbox app (scoped by app name); the relay password is a
    per-instance secret and is deliberately NOT injected into every app's env.
    ``configured`` is False when email/relay isn't set up on this instance.
    """

    configured: bool
    smtp_relay_host: str | None
    smtp_relay_port: int | None
    smtp_relay_user: str | None
    smtp_relay_password: str | None
    zone_domain: str | None
    custom_domain: str | None


@attr.s(auto_attribs=True, frozen=True)
class CustomEmailDomainResponse:
    """Owner-facing view of the custom mail domain and the single NS record to add.

    ``configured`` is False when no custom mail domain is set on this instance, in
    which case the record fields are None.
    """

    configured: bool
    domain: str | None
    record_name: str | None
    record_type: str | None
    record_value: str | None
    display_line: str | None


@attr.s(auto_attribs=True, frozen=True)
class VersionInfo:
    """Git info for the running openhost checkout. ``branch`` is None when HEAD is detached.
    ``sha`` is empty when the install isn't a git checkout (e.g. tarball deploys)."""

    branch: str | None
    sha: str
    short_sha: str
    dirty: bool


# ─── API Tokens ────────────────────────────────────────────────────────────


@get("/api/tokens", guards=[require_owner_auth])
async def api_tokens_list(db: sqlite3.Connection) -> list[ApiToken]:
    rows = db.execute("SELECT id, name, expires_at, created_at FROM api_tokens ORDER BY created_at DESC").fetchall()
    now = datetime.now(UTC)
    tokens: list[ApiToken] = []
    for r in rows:
        has_expiry = bool(r["expires_at"])
        expired = has_expiry and datetime.fromisoformat(r["expires_at"]) < now
        tokens.append(
            ApiToken(
                id=r["id"],
                name=r["name"],
                expires_at=r["expires_at"] or None,
                created_at=r["created_at"],
                expired=expired,
            )
        )
    return tokens


@post("/api/tokens", status_code=200, guards=[require_owner_auth])
async def api_tokens_create(
    data: CreateTokenRequest, db: sqlite3.Connection
) -> Response[CreatedToken] | Response[ErrorResponse]:
    name = data.name.strip() or "Untitled"
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

    db.execute(
        "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
        (name, token_hash, expires_at.isoformat() if expires_at else ""),
    )
    db.commit()

    return Response(
        content=CreatedToken(
            token=raw_token,
            name=name,
            expires_at=expires_at.isoformat() if expires_at else None,
        ),
        status_code=200,
        media_type=MediaType.JSON,
    )


@delete("/api/tokens/{token_id:int}", guards=[require_owner_auth])
async def api_tokens_delete(token_id: int, db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
    db.commit()


# ─── Compute Space Logs ────────────────────────────────────────────────────


# The System page polls this every few seconds; serving the whole file (up to
# the 10 MB rotation limit) makes each poll take ~1s on a small VPS.
_LOG_TAIL_BYTES = 256 * 1024


@get("/api/compute_space_logs", guards=[require_owner_auth], media_type=MediaType.TEXT, sync_to_thread=False)
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


@get("/api/listening-ports", guards=[require_owner_auth], sync_to_thread=False)
def listening_ports(db: sqlite3.Connection) -> ListeningPortsResponse:
    """Return TCP ports listening on external-facing or wildcard interfaces, with classification.

    Loopback-only listeners are excluded — they are not reachable from outside the VM.
    """
    all_ports = list_listening_ports(db=db)
    return ListeningPortsResponse(ports=external_ports(all_ports), enumeration_failed=not all_ports)


# sync_to_thread: walking app_data and querying podman take ~1s on a real
# instance; running on the event loop would stall every concurrent request.
@get("/api/storage-status", guards=[require_owner_auth], sync_to_thread=True)
def api_storage_status(config: Config) -> dict[str, object]:
    return storage_status(config)


@post("/api/storage-guard", status_code=200, guards=[require_owner_auth], sync_to_thread=False)
def toggle_storage_guard(data: ToggleStorageGuardRequest) -> StorageGuardResponse:
    """Pause or resume the storage guard."""
    set_guard_paused(data.paused)
    return StorageGuardResponse(guard_paused=is_guard_paused())


# ─── SSH Toggle ────────────────────────────────────────────────────────────


@get("/api/ssh-status", guards=[require_owner_auth], sync_to_thread=False)
def ssh_status() -> SshStatusResponse:
    return SshStatusResponse(ssh_enabled=is_sshd_active())


@post("/toggle-ssh", status_code=200, guards=[require_owner_auth], sync_to_thread=False)
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


@post("/api/drop-docker-cache", status_code=200, guards=[require_owner_auth], sync_to_thread=False)
def drop_docker_cache() -> Response[DropCacheOk] | Response[ErrorResponse]:
    """Drop the container build cache to free disk space."""
    try:
        output = drop_docker_build_cache()
    except RuntimeError as e:
        return Response(content=ErrorResponse(error=str(e)), status_code=500)
    return Response(content=DropCacheOk(ok=True, output=output), status_code=200, media_type=MediaType.JSON)


@get("/api/version", guards=[require_owner_auth])
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


@get("/api/diagnostics", guards=[require_owner_auth])
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


@get("/api/email/relay-config", guards=[require_app_auth], sync_to_thread=True)
def email_relay_config(
    request: Request[Any, Any, Any], db: sqlite3.Connection, config: Config
) -> Response[EmailRelayConfigResponse]:
    """Return the SMTP smarthost relay config for the mailbox app.

    Scoped: the request must be authenticated as an app (OPENHOST_APP_TOKEN), and
    that app must be one of ``config.email_mailbox_app_names``. The relay host/port
    + per-instance credential are fetched at runtime from the frontend (never baked
    into this instance's config), then handed only to the mailbox app.
    """
    app_id = verify_app_auth(request)
    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    app_name = row["name"] if row is not None else None
    if app_name not in config.email_mailbox_app_names:
        raise NotAuthorizedException(detail="app is not authorized to fetch email relay config")

    unconfigured = Response(
        content=EmailRelayConfigResponse(
            configured=False,
            smtp_relay_host=None,
            smtp_relay_port=None,
            smtp_relay_user=None,
            smtp_relay_password=None,
            zone_domain=None,
            custom_domain=None,
        ),
        media_type=MediaType.JSON,
    )
    if not email_enabled(config, db):
        return unconfigured
    try:
        cred = get_relay_credential_provider(config).get(db)
    except RelayCredentialError as e:
        logger.warning(f"relay-config: could not fetch credential from frontend: {e}")
        return unconfigured
    if cred is None:
        return unconfigured
    return Response(
        content=EmailRelayConfigResponse(
            configured=True,
            smtp_relay_host=cred.smtp_relay_host,
            smtp_relay_port=cred.smtp_relay_port,
            smtp_relay_user=cred.smtp_relay_user,
            smtp_relay_password=cred.smtp_relay_password,
            zone_domain=cred.zone_domain,
            custom_domain=cred.custom_domain,
        ),
        media_type=MediaType.JSON,
    )


@get("/api/email/custom-domain", guards=[require_owner_auth], sync_to_thread=False)
def custom_email_domain(db: sqlite3.Connection, config: Config) -> CustomEmailDomainResponse:
    """Return the owner's custom mail domain and the single NS record to delegate it.

    The owner sets this once at their registrar; the instance's nameserver host
    (ns.<zone>) already resolves to the instance IP, so this one record is all
    that is required for the custom domain to work.
    """
    record = config.custom_domain_delegation_record(primary_domain(db).name_no_port)
    if record is None:
        return CustomEmailDomainResponse(
            configured=False,
            domain=None,
            record_name=None,
            record_type=None,
            record_value=None,
            display_line=None,
        )
    return CustomEmailDomainResponse(
        configured=True,
        domain=config.email_custom_domain_normalized,
        record_name=record.name,
        record_type=record.record_type,
        record_value=record.value,
        display_line=record.as_display_line(),
    )


@post("/restart_router", status_code=200, guards=[require_owner_auth], sync_to_thread=False)
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
        api_tokens_list,
        api_tokens_create,
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
        email_relay_config,
        custom_email_domain,
        restart_router,
    ],
)
