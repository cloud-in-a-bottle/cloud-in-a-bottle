import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable

import attr
import httpx

import compute_space.core.storage as storage
from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.app_id import is_valid_app_name
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.auth import DEFAULT_OWNER_USERNAME
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.auth.permissions_v2 import PermissionRecord
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import build_image
from compute_space.core.containers import is_container_running
from compute_space.core.containers import remove_image
from compute_space.core.containers import run_container
from compute_space.core.containers import stop_app_process
from compute_space.core.data import deprovision_data
from compute_space.core.data import deprovision_temp_data
from compute_space.core.data import provision_data
from compute_space.core.data import rmtree_with_sudo_fallback
from compute_space.core.git_ops import UnsupportedRepoUrlError
from compute_space.core.git_ops import is_github_repo_url
from compute_space.core.git_ops import is_ssh_url
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger
from compute_space.core.manifest import AppLink
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping
from compute_space.core.manifest import parse_manifest
from compute_space.core.oauth import OAuthAuthorizationRequired
from compute_space.core.oauth import get_oauth_token
from compute_space.core.ports import allocate_port
from compute_space.core.ports import resolve_port_mappings
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.services_v2 import register_v2_service_providers
from compute_space.db import get_db


@attr.s(auto_attribs=True, frozen=True)
class PermissionGrant:
    """A single permission grant: which service and what payload."""

    service_url: str
    grant: Grant


RESERVED_PATHS = {
    "/",
    "/dashboard",
    "/login",
    "/logout",
    "/add_app",
    "/remove_app",
    "/stop_app",
    "/reload_app",
    "/api",
    "/health",
    "/app",
    "/setup",
    "/.well-known",
    "/terminal",
    "/toggle-ssh",
    "/identity",
    "/settings",
    "/system",
    "/docs",
}


def _serialize_links(links: list[AppLink]) -> str:
    """Serialize manifest [[links]] to the JSON stored in ``apps.links``."""
    return json.dumps([{"name": link.name, "path": link.path} for link in links])


def _manifest_column_values(manifest: AppManifest) -> dict[str, object]:
    """The ``apps`` columns that are derived from the manifest, mapped to their
    serialized values.

    Single source of truth shared by install (:func:`insert_and_deploy`) and
    reload (:func:`reload_app_background`) so the two paths can't drift. Reload
    previously only re-wrote a subset (public_paths/links/manifest_raw/name),
    leaving resource limits and other manifest-derived columns stale in the DB
    after an update even though the running container did pick up the new
    values — which surfaced as "the new cpu isn't applied" on the dashboard.

    Excludes columns that are NOT manifest-derived (app_id, local_port,
    repo_path, repo_url, status, installed_by, container_id): those are set at
    install / port-allocation time and must survive a reload unchanged.
    """
    return {
        "manifest_name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "runtime_type": manifest.runtime_type,
        "health_check": manifest.health_check,
        "container_port": manifest.container_port,
        "memory_mb": manifest.memory_mb,
        "cpu_cores": manifest.cpu_cores,
        "gpu": int(manifest.gpu),
        "public_paths": json.dumps(manifest.public_paths),
        "links": _serialize_links(manifest.links),
        "manifest_raw": manifest.raw_toml,
    }


def deserialize_links(raw: str | None) -> list[AppLink]:
    """Parse the JSON stored in ``apps.links`` back into ``AppLink`` objects.

    Tolerates malformed/missing values (returns ``[]``) so a bad row can't
    take down the dashboard; such a value would indicate corruption since
    the column is only ever written by :func:`_serialize_links`.
    """
    try:
        entries = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse apps.links JSON; treating as empty: %r", raw)
        return []
    links: list[AppLink] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("path"), str):
            links.append(AppLink(name=entry["name"], path=entry["path"]))
    return links


@attr.s(auto_attribs=True, frozen=True)
class App:
    """In-memory mirror of one row of the ``apps`` table."""

    id: int
    app_id: str
    name: str
    manifest_name: str
    version: str
    description: str | None
    runtime_type: str
    repo_path: str
    repo_url: str | None
    health_check: str | None
    local_port: int
    container_port: int | None
    container_id: str | None
    status: str
    error_message: str | None
    memory_mb: int
    cpu_cores: float
    gpu: int
    public_paths: list[str]
    links: list[AppLink]
    manifest_raw: str | None
    installed_by: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "App":
        fields = {f.name: row[f.name] for f in attr.fields(cls)}
        fields["public_paths"] = json.loads(fields["public_paths"] or "[]")
        fields["links"] = deserialize_links(fields["links"])
        return cls(**fields)


def find_app_by_name(name: str) -> App | None:
    row = get_db().execute("SELECT * FROM apps WHERE name = ?", (name,)).fetchone()
    return App.from_row(row) if row else None


def inject_github_token_in_url(url: str, token: str) -> str:
    """Inject a GitHub OAuth token into an HTTP(S) URL for authentication."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host_port = parsed.hostname
        if parsed.port:
            host_port = f"{parsed.hostname}:{parsed.port}"
        return parsed._replace(netloc=f"{token}@{host_port}").geturl()
    return url


def github_token_git_config(token: str | None) -> list[str]:
    """Ephemeral ``git -c`` args that authenticate GitHub HTTPS fetches.

    Rides in GIT_CONFIG_PARAMETERS, which git propagates to child processes —
    including recursive submodule clones/fetches — so private submodules
    authenticate without the token ever being written to a git config file.
    """
    if not token:
        return []
    return ["-c", f"url.https://{token}@github.com/.insteadOf=https://github.com/"]


async def clone_and_read_manifest(
    repo_url: str, github_token: str | None = None
) -> tuple[AppManifest | None, str | None, str | None]:
    """Clone a repo to a temp dir and read its openhost.toml.

    Returns (manifest, clone_dir, error). On success error is None.
    """
    try:
        base_url, ref = parse_repo_url(repo_url)
    except UnsupportedRepoUrlError as e:
        return None, None, str(e)
    clone_url = base_url

    # For file:// URLs, copy the directory if it's not a git repo
    if base_url.startswith("file://"):
        local_path = base_url[len("file://") :]
        if not os.path.isdir(local_path):
            return None, None, f"Local path does not exist: {local_path}"
        is_git = os.path.isdir(os.path.join(local_path, ".git")) or os.path.isfile(os.path.join(local_path, "HEAD"))
        if not is_git:
            tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
            clone_dir = os.path.join(tmp_parent, "repo")
            try:
                shutil.copytree(local_path, clone_dir)
                manifest = parse_manifest(clone_dir)
                return manifest, clone_dir, None
            except ValueError as e:
                rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
                return None, None, str(e)
            except Exception as e:
                rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
                return None, None, f"Copy failed: {e}"

    if github_token:
        clone_url = inject_github_token_in_url(base_url, github_token)

    tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    clone_dir = os.path.join(tmp_parent, "repo")
    try:
        clone_cmd = [
            "git",
            *github_token_git_config(github_token),
            "clone",
            "--recurse-submodules",
            "--shallow-submodules",
        ]
        if ref:
            clone_cmd.extend(["--branch", ref])
        clone_cmd.extend([clone_url, clone_dir])
        result = await asyncio.to_thread(subprocess.run, clone_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
            stderr = result.stderr.strip()
            if github_token:
                stderr = stderr.replace(github_token, "***")
            return None, None, f"Git clone failed: {stderr}"
        # If we cloned with a token, reset the remote URL so the token isn't persisted
        if github_token and clone_url != base_url:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "set-url", "origin", base_url],
                cwd=clone_dir,
                capture_output=True,
                timeout=10,
            )
            # Relative .gitmodules URLs resolved against the token-bearing clone
            # URL, so the recorded submodule URLs may embed the token; re-sync
            # them against the now-clean origin.
            if os.path.exists(os.path.join(clone_dir, ".gitmodules")):
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "submodule", "sync", "--recursive"],
                    cwd=clone_dir,
                    capture_output=True,
                    timeout=30,
                )
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
            return None, None, str(e)
        return manifest, clone_dir, None
    except subprocess.TimeoutExpired:
        rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
        return None, None, "Git clone timed out"
    except Exception as e:
        rmtree_with_sudo_fallback(tmp_parent, raise_on_failure=False)
        return None, None, f"Clone failed: {e}"


def move_clone_to_app_temp_dir(clone_dir: str, app_name: str, config: Config) -> str:
    """Move a clone tempdir into the persistent <temp>/app_temp_data/<name>/repo
    location and return the destination path.  Used by both /api/add_app
    and the default-apps deploy hook so the on-disk layout matches.

    Also rmtree's the now-empty mkdtemp parent of clone_dir to avoid
    leaking ``openhost-clone-*`` shells.
    """
    final_dir = os.path.join(config.temporary_data_dir, "app_temp_data", app_name, "repo")
    if os.path.exists(final_dir):
        rmtree_with_sudo_fallback(final_dir, raise_on_failure=True)
    os.makedirs(os.path.dirname(final_dir), exist_ok=True)
    tmp_parent = os.path.dirname(clone_dir)
    shutil.move(clone_dir, final_dir)
    shutil.rmtree(tmp_parent, ignore_errors=True)
    return final_dir


async def clone_with_github_fallback(
    repo_url: str, return_to: str
) -> tuple[AppManifest | None, str | None, str | None, str | None]:
    """Clone a repo, retrying with a stored GitHub OAuth token if needed.

    Returns (manifest, clone_dir, error, authorize_url).
    """
    manifest, tmp_dir, error = await clone_and_read_manifest(repo_url)

    # An SSH URL never clones over HTTPS-authenticated OAuth, so don't bounce
    # the owner through the GitHub authorize flow only to fail again — the
    # error from clone_and_read_manifest already explains to use HTTPS.
    if error and is_github_repo_url(repo_url) and not is_ssh_url(repo_url):
        clone_error = error
        try:
            token = await get_oauth_token("github", ["repo"], return_to=return_to)
        except ServiceNotAvailable as e:
            return None, None, f"{clone_error} (also failed to fetch GitHub OAuth token: {e.message})", None
        except OAuthAuthorizationRequired as e:
            return None, None, f"{clone_error} (GitHub OAuth not configured: {e.authorize_url})", e.authorize_url
        manifest, tmp_dir, error = await clone_and_read_manifest(repo_url, github_token=token)

    return manifest, tmp_dir, error, None


def validate_manifest(manifest: AppManifest, db: sqlite3.Connection, app_name: str | None = None) -> str | None:
    """Check reserved names and duplicates. Returns error string or None."""
    if app_name is None:
        app_name = manifest.name

    if not is_valid_app_name(app_name):
        return "App name must be lowercase alphanumeric (hyphens allowed, not at start/end)"

    if f"/{app_name}" in RESERVED_PATHS:
        return f"App name '{app_name}' conflicts with a reserved path"

    existing = db.execute("SELECT name FROM apps WHERE name = ?", (app_name,)).fetchone()
    if existing:
        return f"App name already in use by '{existing['name']}'"

    return None


def _mint_unique_app_id(db: sqlite3.Connection) -> str:
    """Mint an app_id that doesn't already exist in the apps table.

    Collisions are vanishingly rare (~70 bits) but the loop is cheap insurance.
    """
    while True:
        candidate = new_app_id()
        if not db.execute("SELECT 1 FROM apps WHERE app_id = ?", (candidate,)).fetchone():
            return candidate


def all_manifest_permissions_v2(manifest: AppManifest) -> list[PermissionGrant]:
    """Build a permissions_v2_grants list that approves every permission declared in the manifest."""
    grants: list[PermissionGrant] = []
    for perm in manifest.consumes_services_v2:
        for grant_payload in perm.grants:
            grants.append(PermissionGrant(service_url=perm.service, grant=grant_payload))
    return grants


def _permission_key(service_url: str, grant_payload: Grant) -> tuple[str, str]:
    """Normalized identity for a (service, grant) pair.

    Uses the same ``json.dumps(..., sort_keys=True)`` serialization that
    :func:`grant_permission_v2` stores, so a declared grant and a stored grant
    compare equal regardless of dict key order.
    """
    return (service_url, json.dumps(grant_payload, sort_keys=True))


def manifest_ungranted_permissions_v2(
    manifest: AppManifest,
    granted: list[PermissionRecord],
) -> list[PermissionGrant]:
    """The permissions a manifest declares that are NOT already granted.

    Single source of truth for the "which manifest permissions still need owner
    approval" diff, shared by the app-detail page (post-install display) and the
    update/reload gate (so an app update can't silently pick up newly declared
    permissions). ``granted`` is the app's current ``permissions_v2`` rows
    (e.g. from :func:`get_all_permissions_v2`).

    Duplicate manifest declarations collapse to a single entry, and each new
    permission is returned only once even if declared multiple times.
    """
    granted_keys = {_permission_key(rec.service_url, rec.grant) for rec in granted}
    ungranted: list[PermissionGrant] = []
    seen: set[tuple[str, str]] = set(granted_keys)
    for perm in manifest.consumes_services_v2:
        for grant_payload in perm.grants:
            key = _permission_key(perm.service, grant_payload)
            if key in seen:
                continue
            seen.add(key)
            ungranted.append(PermissionGrant(service_url=perm.service, grant=grant_payload))
    return ungranted


def insert_and_deploy(
    manifest: AppManifest,
    repo_path: str,
    config: Config,
    db: sqlite3.Connection,
    permissions_v2_grants: list[PermissionGrant] | None = None,
    app_name: str | None = None,
    repo_url: str | None = None,
    port_overrides: dict[str, int] | None = None,
    installed_by: str | None = None,
) -> str:
    """Insert app into DB and start background deploy.

    Returns app_id. Raises RuntimeError if no port available or
    storage limit is exceeded.

    permissions_v2_grants: list of PermissionGrant objects the owner
        approved on the deploy page.  Only these are granted at install
        time.
    port_overrides: optional dict of label -> host_port from CLI/API.
    installed_by: consumer app name when the install came in via the
        installer v2 service.  Stored on the apps row in the same
        INSERT (so there's no race with the background build thread)
        and used by the installer's /status and /logs endpoints to
        scope visibility to the installs each caller initiated.
    """
    if app_name is None:
        app_name = manifest.name

    storage.check_before_deploy(config)
    local_port = allocate_port(config.port_range_start, config.port_range_end)
    app_id = _mint_unique_app_id(db)

    # Apply port overrides from caller (CLI --port flags, etc.)
    mappings = [
        attr.evolve(pm, host_port=port_overrides.get(pm.label, pm.host_port)) if port_overrides else pm
        for pm in manifest.port_mappings
    ]

    # Resolve auto-assigned ports (host_port=0)
    resolved_mappings = resolve_port_mappings(mappings, db, config.port_range_start, config.port_range_end)

    env_vars = provision_data(
        app_id=app_id,
        app_name=app_name,
        manifest=manifest,
        data_dir=config.persistent_data_dir,
        temp_data_dir=config.temporary_data_dir,
        archive_dir=archive_backend.effective_archive_dir(config, db),
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
        zone_domain=config.zone_domain,
        port=config.port,
        owner_username=read_owner_username(db) or DEFAULT_OWNER_USERNAME,
    )

    # Manifest-derived columns come from the shared helper so install and
    # reload write exactly the same set; non-manifest columns (identity, repo
    # location, allocated port, initial status, installer) are set here only.
    manifest_columns = _manifest_column_values(manifest)
    install_columns: dict[str, object] = {
        "app_id": app_id,
        "name": app_name,
        "repo_path": repo_path,
        "repo_url": repo_url,
        "local_port": local_port,
        "status": "building",
        "installed_by": installed_by,
        **manifest_columns,
    }
    _columns = list(install_columns)
    db.execute(
        f"INSERT INTO apps ({', '.join(_columns)}) VALUES ({', '.join('?' for _ in _columns)})",
        tuple(install_columns[c] for c in _columns),
    )

    # Store resolved port mappings
    for pm in resolved_mappings:
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES (?, ?, ?, ?)",
            (app_id, pm.label, pm.container_port, pm.host_port),
        )

    for db_name in manifest.sqlite_dbs:
        db_path = env_vars.get(f"OPENHOST_SQLITE_{db_name.upper()}", "")
        db.execute(
            "INSERT INTO app_databases (app_id, db_name, db_path) VALUES (?, ?, ?)",
            (app_id, db_name, db_path),
        )
    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        app_token_hash = hashlib.sha256(app_token.encode()).hexdigest()
        db.execute(
            "INSERT OR REPLACE INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, app_token_hash),
        )

    register_v2_service_providers(app_id, manifest, db)

    db.commit()

    if permissions_v2_grants:
        for entry in permissions_v2_grants:
            grant_permission_v2(
                consumer_app_id=app_id,
                service_url=entry.service_url,
                grant_payload=entry.grant,
            )

    threading.Thread(
        target=deploy_app_background,
        args=(manifest, repo_path, local_port, env_vars, config),
        kwargs={"app_id": app_id, "app_name": app_name, "port_mappings": resolved_mappings},
        daemon=True,
    ).start()

    return app_id


def deploy_app_background(
    manifest: AppManifest,
    repo_path: str,
    local_port: int,
    env_vars: dict[str, str],
    config: Config,
    app_id: str,
    app_name: str | None = None,
    port_mappings: list[PortMapping] | None = None,
) -> None:
    """Build and start an app in a background thread."""
    if app_name is None:
        app_name = manifest.name

    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        storage.check_before_deploy(config)

        # Retry container builds for transient failures.  Cache-corrupt
        # failures re-raise immediately — retrying can't fix them and
        # the dashboard needs the marker to surface the remediation.
        max_build_attempts = 3
        image_tag = ""
        for attempt in range(1, max_build_attempts + 1):
            try:
                image_tag = build_image(
                    app_name,
                    repo_path,
                    manifest.container_image,
                    temp_data_dir=config.temporary_data_dir,
                )
                break
            except RuntimeError as e:
                if BUILD_CACHE_CORRUPT_MARKER in str(e):
                    raise
                if attempt == max_build_attempts:
                    raise
                logger.warning(
                    "Container build attempt %d/%d for %s failed, retrying in %ds",
                    attempt,
                    max_build_attempts,
                    app_name,
                    attempt * 5,
                )
                time.sleep(attempt * 5)
        db.execute(
            "UPDATE apps SET status = 'starting' WHERE app_id = ?",
            (app_id,),
        )
        db.commit()
        container_id = run_container(
            app_name,
            image_tag,
            manifest,
            local_port,
            env_vars,
            config.persistent_data_dir,
            config.temporary_data_dir,
            archive_backend.effective_archive_dir(config, db),
            port_mappings=port_mappings,
        )
        db.execute(
            "UPDATE apps SET container_id = ? WHERE app_id = ?",
            (container_id, app_id),
        )
        db.commit()

        if wait_for_ready(local_port):
            db.execute(
                "UPDATE apps SET status = 'running' WHERE app_id = ?",
                (app_id,),
            )
        else:
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                ("App started but not responding to HTTP", app_id),
            )
        db.commit()
    except Exception as e:
        logger.exception("Failed to deploy %s", app_name)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            (str(e), app_id),
        )
        db.commit()
    finally:
        db.close()


def wait_for_ready(local_port: int, timeout: int = 60) -> bool:
    """Poll the app's local port until it responds to HTTP. Returns True if ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{local_port}/", timeout=2)
            if r.status_code < 500:
                return True
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            ConnectionResetError,
        ):
            pass
        time.sleep(1)
    return False


def _load_port_mappings_from_db(app_id: str, db: sqlite3.Connection) -> list[PortMapping]:
    """Load resolved port mappings from the database."""
    rows = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ?",
        (app_id,),
    ).fetchall()
    return [PortMapping(label=r["label"], container_port=r["container_port"], host_port=r["host_port"]) for r in rows]


def _sync_port_mappings(
    app_id: str,
    new_mappings: list[PortMapping],
    db: sqlite3.Connection,
    config: Config,
) -> None:
    """Sync port mappings on reload: preserve existing host_port, handle adds/removes."""
    existing = {
        r["label"]: r
        for r in db.execute(
            "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ?",
            (app_id,),
        ).fetchall()
    }

    new_labels = {pm.label for pm in new_mappings}
    old_labels = set(existing.keys())

    # Remove labels no longer in manifest
    for removed in old_labels - new_labels:
        db.execute(
            "DELETE FROM app_port_mappings WHERE app_id = ? AND label = ?",
            (app_id, removed),
        )

    # Build list for resolve: preserve existing host_port, new ones get 0 (auto)
    to_resolve: list[PortMapping] = []
    for pm in new_mappings:
        if pm.label in existing:
            to_resolve.append(attr.evolve(pm, host_port=existing[pm.label]["host_port"]))
        else:
            to_resolve.append(pm)

    resolved = resolve_port_mappings(
        to_resolve, db, config.port_range_start, config.port_range_end, exclude_app_id=app_id
    )

    # Upsert resolved mappings
    for pm in resolved:
        if pm.label in existing:
            db.execute(
                "UPDATE app_port_mappings SET container_port = ?, host_port = ? WHERE app_id = ? AND label = ?",
                (pm.container_port, pm.host_port, app_id, pm.label),
            )
        else:
            db.execute(
                "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES (?, ?, ?, ?)",
                (app_id, pm.label, pm.container_port, pm.host_port),
            )


def stop_running_archive_apps(
    db: sqlite3.Connection,
    config: Config,
    stopped_out: list[str] | None = None,
) -> list[str]:
    """Stop every RUNNING app that uses the archive tier; return their app_ids.

    Used to quiesce the archive mount before a local->S3 migration restarts
    the JuiceFS FUSE mount: no app may write into the local store during the
    sync (that write would be lost when the volume re-points to S3), and
    ``systemctl stop openhost-juicefs`` cannot cleanly unmount while a
    container still has the mount open.  The caller restarts these apps once
    the mount is back (via ``start_apps_by_id``).

    ``stop_app_process`` stops the container but does NOT update the DB status
    and never raises, so we verify quiescence against the real container state
    (``is_container_running``), NOT the ``apps.status`` column.  If any archive
    container is still running after the stop attempt we raise: proceeding
    while a container may still be writing to the archive risks silent data
    loss, so we abort and let the caller fail-open (the local backend stays
    intact).

    ``stopped_out``, when provided, is appended to incrementally as each
    container is stopped, so the caller can restart the already-stopped apps
    even if this function later raises (the return value is lost on ``raise``).
    """
    recorded: list[str] = stopped_out if stopped_out is not None else []
    rows = db.execute("SELECT app_id, name, container_id, status, manifest_raw FROM apps").fetchall()
    still_running: list[str] = []
    for row in rows:
        if row["status"] != "running":
            continue
        if not archive_backend.manifest_uses_archive(row["manifest_raw"] or ""):
            continue
        logger.info("stopping archive-using app %s before archive migration", row["name"])
        try:
            stop_app_process(row)
        except Exception:
            logger.exception("failed to stop app %s before archive migration", row["name"])
        recorded.append(row["app_id"])
        # Verify against the real container state (stop_app_process is
        # best-effort and doesn't touch the DB).
        container_id = row["container_id"]
        if container_id and is_container_running(container_id):
            still_running.append(row["name"])

    if still_running:
        raise RuntimeError(
            f"could not stop archive-using app(s) before migration: {', '.join(still_running)}; "
            "aborting so the sync can't race a live writer (local backend left intact)"
        )
    return recorded


def start_apps_by_id(app_ids: list[str], db: sqlite3.Connection, config: Config) -> None:
    """Start each app by id (best-effort).  Companion to
    ``stop_running_archive_apps`` for the migration quiesce/resume dance."""
    for app_id in app_ids:
        try:
            start_app_process(app_id, db, config)
        except Exception:
            logger.exception("failed to restart app %s after archive migration remount", app_id)


def start_app_process(app_id: str, db: sqlite3.Connection, config: Config) -> None:
    """Start the process for an app. Updates DB with status and container id."""
    app_row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    storage.check_before_deploy(config)
    app_name = app_row["name"]

    manifest = parse_manifest(app_row["repo_path"])
    env_vars = provision_data(
        app_id=app_id,
        app_name=app_name,
        manifest=manifest,
        data_dir=config.persistent_data_dir,
        temp_data_dir=config.temporary_data_dir,
        archive_dir=archive_backend.effective_archive_dir(config, db),
        my_openhost_redirect_domain=config.my_openhost_redirect_domain,
        zone_domain=config.zone_domain,
        port=config.port,
        owner_username=read_owner_username(db) or DEFAULT_OWNER_USERNAME,
    )

    app_token = env_vars.get("OPENHOST_APP_TOKEN")
    if app_token:
        app_token_hash = hashlib.sha256(app_token.encode()).hexdigest()
        db.execute(
            "INSERT OR REPLACE INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, app_token_hash),
        )

    register_v2_service_providers(app_id, manifest, db)

    # Load resolved port mappings from DB (preserves host_port assignments)
    port_mappings = _load_port_mappings_from_db(app_id, db)

    db.execute(
        "UPDATE apps SET status = 'starting', error_message = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    image_tag = build_image(
        app_name,
        app_row["repo_path"],
        manifest.container_image,
        temp_data_dir=config.temporary_data_dir,
    )
    container_id = run_container(
        app_name,
        image_tag,
        manifest,
        app_row["local_port"],
        env_vars,
        config.persistent_data_dir,
        config.temporary_data_dir,
        archive_backend.effective_archive_dir(config, db),
        port_mappings=port_mappings,
    )
    db.execute(
        "UPDATE apps SET container_id = ? WHERE app_id = ?",
        (container_id, app_id),
    )
    db.commit()

    if wait_for_ready(app_row["local_port"]):
        db.execute(
            "UPDATE apps SET status = 'running' WHERE app_id = ?",
            (app_id,),
        )
    else:
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            ("App started but not responding to HTTP", app_id),
        )
    db.commit()


def app_log_path(app_name: str, config: Config) -> str:
    """Return the build log file path for an app."""
    return os.path.join(config.temporary_data_dir, "app_temp_data", app_name, "docker.log")


def app_container_log_path(app_name: str, config: Config) -> str:
    """Return the container runtime log file path for an app."""
    return os.path.join(config.temporary_data_dir, "app_temp_data", app_name, "container.log")


def _remote_default_branch(repo_path: str, log: Callable[[str], None]) -> tuple[str | None, str | None]:
    """Resolve origin's default branch (e.g. ``main``/``master``).

    Returns ``(branch, error)``. ``git remote set-head --auto`` queries the
    remote, so on auth/network failure we return an error string for the
    caller's token-retry path to act on; ``(None, None)`` means the remote was
    reachable but the default branch couldn't be read (caller falls back to a
    plain pull on the current branch).
    """
    log("$ git remote set-head origin --auto")
    set_head = subprocess.run(
        ["git", "remote", "set-head", "origin", "--auto"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if set_head.returncode != 0:
        return None, set_head.stderr.strip() or set_head.stdout.strip() or "failed to query remote default branch"
    sym = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if sym.returncode != 0:
        return None, None
    name = sym.stdout.strip().removeprefix("origin/")
    return (name or None), None


def git_pull(
    repo_path: str,
    app_name: str,
    github_token: str | None = None,
    log_file: str | None = None,
    repo_url: str | None = None,
) -> tuple[bool, str | None]:
    """Try to git pull. Returns (ok, error_msg)."""

    def _redact(msg: str) -> str:
        return msg.replace(github_token, "***") if github_token else msg

    def _log(msg: str) -> None:
        msg = _redact(msg)
        logger.info("%s: %s", app_name, msg)
        if log_file:
            with open(log_file, "a") as f:
                f.write(msg + "\n")

    # ``ref`` is the branch/tag/commit pinned via the ``url@ref`` suffix of the
    # stored repo_url. When set, we fetch and hard-checkout it so that editing
    # the upstream branch actually switches branches. When unset (the operator
    # cleared the ``@ref``), we fall back to origin's default branch so that
    # clearing it returns to main/master rather than sticking on whatever branch
    # happened to be checked out.
    ref: str | None = None
    if repo_url:
        try:
            base_url, ref = parse_repo_url(repo_url)
        except UnsupportedRepoUrlError as e:
            _log(str(e))
            return False, str(e)
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", base_url],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            _log(f"failed to set remote url: {e}")
            return False, str(e)

    original_url = None
    if github_token:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            original_url = result.stdout.strip()
            authed_url = inject_github_token_in_url(original_url, github_token)
            if authed_url != original_url:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", authed_url],
                    cwd=repo_path,
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            pass

    def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str | None]:
        _log("$ " + " ".join(cmd))
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout)
        if result.stdout.strip():
            _log(result.stdout.strip())
        if result.stderr.strip():
            _log(result.stderr.strip())
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or f"{' '.join(cmd)} failed"
            return False, _redact(err)
        return True, None

    def _restore_origin() -> None:
        if github_token and original_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", original_url],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            )

    def _update_submodules() -> tuple[bool, str | None]:
        if not os.path.exists(os.path.join(repo_path, ".gitmodules")):
            return True, None
        # Sync recorded submodule URLs against the clean origin URL (relative
        # .gitmodules paths resolve against it), so the token never lands in a
        # config file; auth rides only in the ephemeral insteadOf config.
        _restore_origin()
        ok, err = _run(["git", "submodule", "sync", "--recursive"])
        if not ok:
            return False, err
        return _run(
            ["git", *github_token_git_config(github_token), "submodule", "update", "--init", "--recursive"],
            timeout=300,
        )

    try:
        if not ref:
            # No pinned ref: resolve origin's default branch and switch to it.
            ref, default_err = _remote_default_branch(repo_path, _log)
            if default_err:
                # Likely auth/network — surface so the caller can retry with a token.
                return False, default_err
        if not ref:
            # Couldn't determine a default branch; pull the current one.
            ok, err = _run(["git", "pull"])
            if not ok:
                return False, err
            return _update_submodules()

        # Fetch the pinned ref and hard-reset the working tree to it.
        # TODO: this duplicates the branch-vs-tag checkout in
        # git_ops.hard_checkout_ref (the system_agent update path), which also
        # runs ``git clean -fd`` to drop untracked files that can shadow modules
        # removed/renamed between revisions. We omit the clean (parity with the
        # old ``git pull`` behaviour). Decide whether to converge the two paths
        # and/or add ``git clean -fd`` here so stale untracked files don't leak
        # into the Docker build context.
        ok, err = _run(["git", "fetch", "origin", ref])
        if not ok:
            return False, err
        # A branch ref materialises as the remote-tracking ``origin/<ref>``;
        # check it out as a local branch so it tracks and future pulls update
        # cleanly (mirrors the system_agent update flow). A tag or commit SHA
        # has no ``origin/<ref>`` branch, so fall back to a detached checkout
        # of the just-fetched FETCH_HEAD.
        is_branch = (
            subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}"],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
        if is_branch:
            ok, err = _run(["git", "checkout", "-fB", ref, f"origin/{ref}"])
        else:
            ok, err = _run(["git", "checkout", "-f", "FETCH_HEAD"])
        if not ok:
            return False, err
        return _update_submodules()
    except Exception as e:
        _log(f"git update failed: {e}")
        return False, str(e)
    finally:
        if github_token and original_url:
            subprocess.run(
                ["git", "remote", "set-url", "origin", original_url],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            )


def reload_app_background(app_id: str, repo_path: str, config: Config) -> None:
    """Reload an app in a background thread: re-read manifest, rebuild, start."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        app_row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        if app_row is None:
            logger.error("reload_app_background: no app with id %s", app_id)
            return
        app_name = app_row["name"]

        if repo_path and os.path.isdir(os.path.join(repo_path, ".git")):
            pass  # git pull already done before background thread
        else:
            source_dir = os.path.join(config.apps_dir, app_name)
            if not os.path.isdir(source_dir):
                source_dir = None  # type: ignore[assignment]
                for entry in os.listdir(config.apps_dir):
                    candidate = os.path.join(config.apps_dir, entry)
                    if os.path.isfile(os.path.join(candidate, "openhost.toml")):
                        try:
                            m = parse_manifest(candidate)
                            if m.name == app_name:
                                source_dir = candidate
                                break
                        except ValueError:
                            pass
            if source_dir and os.path.isdir(source_dir):
                if os.path.exists(repo_path):
                    # Git clones can leave read-only objects that plain
                    # shutil.rmtree refuses; fall back to sudo rm -rf.
                    rmtree_with_sudo_fallback(repo_path, raise_on_failure=True)
                shutil.copytree(source_dir, repo_path)
                logger.info("Re-copied %s from %s", app_name, source_dir)

        manifest = None
        if repo_path and os.path.isdir(repo_path):
            try:
                manifest = parse_manifest(repo_path)
                # Re-sync ALL manifest-derived columns (resource limits,
                # health_check, container_port, version, etc.), not just a
                # subset, so the DB reflects what the reloaded container is
                # actually run with. Without this, a manifest change like a new
                # cpu_cores took effect on the container but the DB (and thus the
                # dashboard/diagnostics) kept showing the install-time value.
                manifest_columns = _manifest_column_values(manifest)
                _set_cols = list(manifest_columns)
                db.execute(
                    f"UPDATE apps SET {', '.join(f'{c} = ?' for c in _set_cols)} WHERE app_id = ?",
                    (*(manifest_columns[c] for c in _set_cols), app_id),
                )

                # Diff port mappings: preserve existing host_port for unchanged labels
                _sync_port_mappings(app_id, manifest.port_mappings, db, config)

                # Re-register v2 service providers from the new manifest.
                register_v2_service_providers(app_id, manifest, db)

                db.commit()
            except ValueError:
                logger.warning("Failed to re-read manifest for %s during reload", app_id)
            # Permissions are not touched here. New permissions declared in an
            # updated manifest are gated before this background reload even
            # starts: _reload_app_impl refuses to rebuild until the owner has
            # explicitly approved them (mirroring the install-time approval),
            # so by the time we get here the required grants already exist.

        start_app_process(app_id, db, config)
    except Exception as e:
        logger.exception("Failed to reload %s", app_id)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            (str(e), app_id),
        )
        db.commit()
    finally:
        db.close()


def remove_app_background(app_id: str, keep_data: bool, config: Config) -> None:
    """Tear an app down in a background thread.

    Caller flips the row to ``status='removing'`` before spawning this.
    Stop/remove/deprovision errors are logged and swallowed so the
    final DELETE still runs and the row never gets stuck in 'removing'.
    If anything escapes the inner handlers, the outer except flips the
    row to 'error' so the operator can retry.
    """
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # FK enforcement is per-connection and OFF by default; without it
    # the final DELETE wouldn't cascade and child rows in app_tokens,
    # permissions, etc. would be orphaned.
    db.execute("PRAGMA foreign_keys = ON")
    try:
        app_row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        if app_row is None:
            return
        app_name = app_row["name"]

        try:
            stop_app_process(app_row)
        except Exception:
            logger.exception("stop_app_process failed during remove of %s", app_name)
        try:
            remove_image(app_name)
        except Exception:
            logger.exception("remove_image failed during remove of %s", app_name)
        try:
            if keep_data:
                deprovision_temp_data(app_name, config.temporary_data_dir)
            else:
                deprovision_data(
                    app_name,
                    config.persistent_data_dir,
                    config.temporary_data_dir,
                    archive_backend.effective_archive_dir(config, db),
                )
        except Exception:
            logger.exception("Failed to deprovision data for %s", app_name)

        db.execute("DELETE FROM apps WHERE app_id = ?", (app_id,))
        db.commit()
        logger.info("Removed app %s (keep_data=%s)", app_name, keep_data)
    except Exception:
        logger.exception("remove_app_background failed for %s", app_id)
        try:
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                ("Removal failed; retry from the dashboard. See server logs for details.", app_id),
            )
            db.commit()
        except sqlite3.Error:
            logger.exception("Could not record removal failure for %s", app_id)
    finally:
        db.close()


def get_app_from_hostname(host: str) -> App | None:
    """Extract+validate app name from a Host header value (or litestar's request.url.netloc; ie example.com[:port])
    by assuming that app_name is a subdir of zone_domain (as is convention).

    returns None if an app cannot be matched from the header.

    if zone_dir==host.imbue.com:
        ha-tunnel.zplizzi.host.imbue.com -> "ha-tunnel"
        zplizzi.host.imbue.com -> None
        localhost:8080 -> None
    """
    config = get_config()
    zone_domain_no_port = config.zone_domain_no_port
    host_no_port = host.split(":", 1)[0]
    if host_no_port == zone_domain_no_port:
        return None
    if host_no_port.endswith("." + zone_domain_no_port):
        app_name = host_no_port[: -(len(zone_domain_no_port) + 1)]
        if "." not in app_name:
            if app := find_app_by_name(app_name):
                return app
    return None


def is_public_path(app: App, request_path: str) -> bool:
    # TODO: we should consider if this is the appropriate matching logic
    return any(request_path == pp or request_path.startswith(pp.rstrip("/") + "/") for pp in app.public_paths)
