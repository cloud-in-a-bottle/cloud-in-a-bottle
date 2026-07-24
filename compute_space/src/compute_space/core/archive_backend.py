"""Operator-controlled archive backend management.  See
``docs/src/data.md`` for the operator-facing model.

Design: the archive tier is ALWAYS a JuiceFS volume mounted at
``config.app_archive_dir``.  Only the JuiceFS *object storage* differs by
backend:

* ``'local'`` (the default) — JuiceFS ``--storage file``: objects live in a
  directory on the instance's local disk (``local_object_store_dir``, under
  ``persistent_data`` so it is backed up).  No S3, no extra daemon, no extra
  listening port — JuiceFS's ``file`` storage is a first-class backend.
* ``'s3'`` — JuiceFS ``--storage s3``: objects live in an operator-supplied
  S3 (or S3-compatible) bucket.

Because the tier is always the same JuiceFS mount, app containers always
bind-mount ``config.app_archive_dir`` regardless of backend; nothing in the
app lifecycle needs to know which object storage is in use.

Migrating between backends is JuiceFS-native and provider-agnostic: copy the
underlying objects with ``juicefs sync`` from the old object store to the new
one, then re-point the *same* volume at the new store with ``juicefs config
--storage/--bucket``.  The metadata database is untouched, so every file,
directory, mode, and ownership is preserved exactly.  ``local`` -> ``s3``
(first S3 configuration, migrating the local data in) and ``s3`` -> ``s3``
(rotating to a new bucket or a different provider, e.g. AWS -> MinIO) are both
exposed in the UI; the same mechanism would generalise to ``s3`` -> ``local``
if we ever want it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import time
import tomllib
from collections.abc import Callable
from typing import Any

import attr
import boto3
import botocore.exceptions  # noqa: F401  -- imported for ``except`` matching downstream

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.core.pinned_binary import install_pinned_binary

# Name of the systemd unit that manages the JuiceFS FUSE mount.
# Installed by ansible (disabled); enabled by compute_space when the
# archive backend is brought online (which now happens for BOTH the local
# and s3 backends, since local is also a JuiceFS mount).
JUICEFS_SERVICE = "openhost-juicefs"

# JuiceFS binary: pinned version + per-arch download URLs/checksums live in
# ``pinned_binary.py``.
_JUICEFS = get_pinned_binary("juicefs")

# Legacy JuiceFS volume name.  The volume name doubles as the per-store object
# prefix (every chunk lands under ``<store>/<volume>/...``).  Historically every
# zone used this constant, which is UNSAFE when several zones migrate into one
# shared S3 bucket: they all key objects under ``openhost/`` and clobber each
# other's ``juicefs_uuid``/metadata/chunks.  Fresh zones now derive a unique
# per-zone volume name (see ``default_volume_name_for_zone``); this constant is
# kept only as the last-resort fallback and for reading legacy rows.
DEFAULT_VOLUME_NAME = "openhost"

# JuiceFS volume names must match cmd/format.go validName: [a-z0-9-], 3..63 chars,
# not starting/ending with '-'.
_VOLUME_NAME_MAX = 63


def default_volume_name_for_zone(config: Config) -> str:
    """A unique, JuiceFS-valid volume name for this zone.

    Derived from the zone domain so that when a zone migrates its local archive
    into a shared S3 bucket, its objects land under a per-zone prefix
    (``<bucket>/<volume>/...``) that does not collide with any other zone's.
    A short hash of the full zone domain is appended so two zones whose
    sanitised names would otherwise coincide (e.g. very long domains truncated
    to the same head) stay distinct.
    """
    zone = (config.zone_domain or "").split(":", 1)[0].lower()
    # Map anything outside [a-z0-9-] to '-', collapse runs, trim edge dashes.
    slug = re.sub(r"[^a-z0-9-]+", "-", zone).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    digest = hashlib.sha256(zone.encode()).hexdigest()[:8] if zone else "00000000"
    # Reserve room for the "-<8-hex>" suffix and the "oh-" prefix.
    head = slug[: _VOLUME_NAME_MAX - len(digest) - 4].strip("-")
    name = f"oh-{head}-{digest}" if head else f"oh-{digest}"
    # Final safety clamp to validName.
    name = name[:_VOLUME_NAME_MAX].strip("-")
    return name or DEFAULT_VOLUME_NAME


def _juicefs_state_dir(config: Config) -> str:
    """Critical state that must survive reboots (meta.db); back this up."""
    return os.path.join(config.openhost_data_path, "juicefs", "state")


def _juicefs_runtime_dir(config: Config) -> str:
    """Regenerable state (binary, etc.); safe to wipe."""
    return os.path.join(config.openhost_data_path, "juicefs", "runtime")


def _juicefs_install_dir(config: Config) -> str:
    return os.path.join(_juicefs_runtime_dir(config), "bin")


def _juicefs_binary(config: Config) -> str:
    return os.path.join(_juicefs_install_dir(config), f"juicefs-{_JUICEFS.version}")


def _juicefs_meta_db(config: Config) -> str:
    return os.path.join(_juicefs_state_dir(config), "meta.db")


def juicefs_mount_dir(config: Config) -> str:
    """The host-side JuiceFS FUSE mount; bind-mounted into containers.

    This is the archive tier for EVERY backend — apps always see the same
    mount; only the object storage behind JuiceFS changes.
    """
    return config.app_archive_dir


def local_object_store_dir(config: Config) -> str:
    """Directory backing the JuiceFS ``file`` object store on the ``local``
    backend.

    This holds JuiceFS's raw chunk objects (NOT a POSIX view of app files),
    so nothing should ever read it directly.  It lives under
    ``persistent_data`` so it survives rebuilds and is captured by restic
    backups — local archive data has no other durable copy.
    """
    return config.local_archive_object_store_dir


def effective_archive_dir(config: Config, db: sqlite3.Connection) -> str:  # noqa: ARG001
    """The host path bind-mounted into app containers as the archive tier.

    Always the JuiceFS mountpoint now, regardless of backend — kept as a
    function (rather than inlining ``juicefs_mount_dir``) so the many app
    lifecycle call sites keep a single, intention-revealing seam and so the
    signature is stable if a future backend ever needs a different path.
    """
    return juicefs_mount_dir(config)


@attr.s(auto_attribs=True, frozen=True)
class StorageSummary:
    """The storage tiers an app will use, for the install screen.

    Mirrors the way permissions are surfaced before install: the operator
    sees, up front, which storage tiers the app touches and — crucially —
    whether its archive data will land on durable S3 or on non-durable
    LOCAL disk (so they can decide to configure S3 first if they care).
    """

    app_data: bool  # local, backed-up permanent data
    app_temp_data: bool  # local scratch, not backed up
    uses_archive: bool  # app_archive OR access_all_archive / access_all_data
    requires_archive: bool  # hard app_archive requirement
    archive_backend: str  # "local" | "s3" | "disabled"
    archive_is_durable: bool  # True only when backend == "s3"


def storage_summary(manifest_raw: str, db: sqlite3.Connection) -> StorageSummary:
    """Build the :class:`StorageSummary` for an app's manifest + current backend."""
    data = _data_section(manifest_raw)
    requires = bool(data.get("app_archive"))
    uses = bool(data.get("app_archive") or data.get("access_all_archive") or data.get("access_all_data"))
    backend = read_state(db).backend
    durable = backend == "s3"
    return StorageSummary(
        app_data=bool(data.get("app_data", True)) or bool(data.get("sqlite")) or bool(data.get("access_all_app_data")),
        app_temp_data=bool(data.get("app_temp_data")) or bool(data.get("access_all_app_data")),
        uses_archive=uses,
        requires_archive=requires,
        archive_backend=backend,
        archive_is_durable=durable,
    )


def local_archive_apps_with_data(config: Config, db: sqlite3.Connection) -> list[str]:
    """Return the app names that have content in the (local-backed) archive.

    Powers the operator-facing "these apps' archive data will be migrated"
    summary shown before a local -> S3 upgrade.  Because the archive is a
    live JuiceFS mount, we look at the per-app subdirectories of the
    mountpoint itself (the POSIX view), not at the raw object store.

    Returns ``[]`` when the backend is not ``local`` or the mount isn't up.
    """
    if read_state(db).backend != "local":
        return []
    root = juicefs_mount_dir(config)
    if not is_mounted(root) or not os.path.isdir(root):
        return []
    apps: list[str] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for app_name in entries:
        # Skip JuiceFS's own control entries at the mount root (.trash,
        # .config, .stats, .accesslog) — they are not apps.  App names are
        # never dot-prefixed, so excluding dotfiles is safe and future-proof.
        if app_name.startswith("."):
            continue
        app_dir = os.path.join(root, app_name)
        if not os.path.isdir(app_dir):
            continue
        try:
            if any(True for _ in os.scandir(app_dir)):
                apps.append(app_name)
        except OSError:
            continue
    return apps


def juicefs_meta_db_path(config: Config) -> str:
    return _juicefs_meta_db(config)


def juicefs_state_dir(config: Config) -> str:
    return _juicefs_state_dir(config)


def is_juicefs_installed(config: Config) -> bool:
    return os.path.isfile(_juicefs_binary(config)) and os.access(_juicefs_binary(config), os.X_OK)


def install_juicefs(config: Config) -> None:
    """Download + verify + extract the JuiceFS binary.  Idempotent."""
    install_pinned_binary(_JUICEFS, _juicefs_binary(config))


def _format_meta_dsn(config: Config) -> str:
    return f"sqlite3://{_juicefs_meta_db(config)}"


def _bucket_url(
    s3_bucket: str,
    s3_region: str,
    s3_endpoint: str | None,
) -> str:
    """JuiceFS bucket URL for the S3 backend.  Do NOT append a path
    component: JuiceFS's S3 backend parses the first path segment as the
    bucket name (pkg/object/s3.go), so any extra path here would break the
    DNS lookup.  Per-zone isolation is handled via the volume name prefix
    instead.
    """
    if s3_endpoint:
        return f"{s3_endpoint.rstrip('/')}/{s3_bucket}"
    return f"https://{s3_bucket}.s3.{s3_region or 'us-east-1'}.amazonaws.com"


def format_local_volume(config: Config, juicefs_volume_name: str) -> None:
    """Run ``juicefs format --storage file`` for the default local backend.

    Idempotent: ``juicefs format`` on an existing volume is a no-op re:
    data.  The object store directory is created if missing.  This is what
    makes the archive tier available on a fresh zone with zero operator
    configuration.
    """
    if not is_juicefs_installed(config):
        install_juicefs(config)
    # JuiceFS's sqlite3 meta backend opens the file but won't mkdir its parent.
    os.makedirs(_juicefs_state_dir(config), exist_ok=True)
    store_dir = local_object_store_dir(config)
    os.makedirs(store_dir, exist_ok=True)
    cmd = [
        _juicefs_binary(config),
        # --no-agent: skip JuiceFS's pprof HTTP agent (binds 6060..6099) so the
        # security audit doesn't flag a transient unexpected listener.
        "--no-agent",
        "format",
        "--storage",
        "file",
        # JuiceFS's file backend treats the bucket as a directory path; it
        # must end with a slash.  Objects land under ``<store>/<volume>/``.
        "--bucket",
        _file_bucket(store_dir),
        _format_meta_dsn(config),
        juicefs_volume_name,
    ]
    logger.info("Running juicefs format (local file backend) at %s", store_dir)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"juicefs format (file) failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _file_bucket(store_dir: str) -> str:
    """JuiceFS ``file`` backend bucket path: an absolute dir ending in '/'."""
    return store_dir.rstrip("/") + "/"


def format_s3_volume(
    config: Config,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str,
) -> None:
    """Run ``juicefs format --storage s3`` against the S3 bucket.  Idempotent.

    ``juicefs_volume_name`` doubles as the per-zone object prefix (every
    chunk lands under ``<bucket>/<volume>/...``), so two zones can share
    one bucket safely.
    """
    os.makedirs(_juicefs_state_dir(config), exist_ok=True)
    bucket_url = _bucket_url(s3_bucket, s3_region or "us-east-1", s3_endpoint)
    cmd = [
        _juicefs_binary(config),
        "--no-agent",
        "format",
        "--storage",
        "s3",
        "--bucket",
        bucket_url,
        _format_meta_dsn(config),
        juicefs_volume_name,
    ]
    # Pass S3 creds via env, not argv, so they don't leak into ``ps``.
    env = os.environ.copy()
    env["ACCESS_KEY"] = s3_access_key_id
    env["SECRET_KEY"] = s3_secret_access_key
    logger.info("Running juicefs format against %s", bucket_url)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"juicefs format failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )


def _juicefs_env_file(config: Config) -> str:
    """Path to the systemd EnvironmentFile for the JuiceFS service.

    Contains JUICEFS_BINARY, JUICEFS_META_DSN, JUICEFS_MOUNT_DIR, and —
    only on the s3 backend — the S3 credentials (ACCESS_KEY / SECRET_KEY).
    Written by ``_write_env_file`` at configure/attach time; read by the
    ``openhost-juicefs.service`` systemd unit.  The mount command itself is
    backend-agnostic: JuiceFS reads the storage type + bucket from the meta
    DB (set by ``format``/``config``); creds are only needed for S3.
    """
    return os.path.join(config.openhost_data_path, "juicefs", "juicefs.env")


def _write_env_file(
    config: Config,
    s3_access_key_id: str | None,
    s3_secret_access_key: str | None,
) -> None:
    """Write (or overwrite) the systemd EnvironmentFile for JuiceFS.

    The file is mode 0600 so only the ``host`` user can read the S3
    credentials.  Parent directories are created if missing.  On the local
    (file) backend, ``s3_access_key_id``/``s3_secret_access_key`` are None
    and no ACCESS_KEY/SECRET_KEY lines are written.
    """
    env_path = _juicefs_env_file(config)
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    content = (
        f"JUICEFS_BINARY={_juicefs_binary(config)}\n"
        f"JUICEFS_META_DSN={_format_meta_dsn(config)}\n"
        f"JUICEFS_MOUNT_DIR={juicefs_mount_dir(config)}\n"
    )
    if s3_access_key_id is not None and s3_secret_access_key is not None:
        content += f"ACCESS_KEY={s3_access_key_id}\nSECRET_KEY={s3_secret_access_key}\n"
    # Atomic-ish write: write to a temp file then rename so a crash
    # mid-write doesn't leave a truncated env file.
    tmp_path = env_path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.rename(tmp_path, env_path)
    logger.info("Wrote JuiceFS env file at %s", env_path)


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a ``systemctl`` command and return the result.

    Raises ``RuntimeError`` on non-zero exit.
    """
    cmd = ["sudo", "systemctl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def is_mounted(mount_point: str) -> bool:
    """Return True iff ``mount_point`` is a live mount.

    Uses /proc/self/mountinfo because ``os.path.ismount`` breaks on some
    FS/userns combinations.
    """
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 5 and parts[4] == mount_point:
                    return True
    except OSError:
        return False
    return False


def mount(
    config: Config,
    s3_access_key_id: str | None = None,
    s3_secret_access_key: str | None = None,
) -> None:
    """Start the JuiceFS mount via systemd.  Idempotent.

    Backend-agnostic: writes the EnvironmentFile (binary path, meta DSN,
    mount dir, and — only for s3 — S3 creds), then enables and starts the
    ``openhost-juicefs`` systemd service.  systemd's ``Restart=always``
    handles automatic recovery if the FUSE process is OOM-killed or crashes.

    The storage type + bucket come from the meta DB (set by ``format`` /
    ``config``), so the same mount command works for both the local (file)
    and s3 backends; only s3 needs credentials in the environment.
    """
    mount_point = juicefs_mount_dir(config)
    os.makedirs(mount_point, exist_ok=True)

    if is_mounted(mount_point):
        logger.info("juicefs already mounted at %s", mount_point)
        return

    _write_env_file(config, s3_access_key_id, s3_secret_access_key)

    logger.info("Starting %s systemd service", JUICEFS_SERVICE)
    # daemon-reload in case the unit file was just installed or updated.
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", JUICEFS_SERVICE)

    # Wait for the mount to appear.  systemd starts the process
    # asynchronously; the FUSE handshake + initial object-store connection
    # can take 15-30s on high-latency S3 links (e.g. Hetzner -> us-west-2).
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_mounted(mount_point):
            logger.info("juicefs mount ready at %s (via systemd)", mount_point)
            return
        time.sleep(0.5)

    # Check if the service failed to start.
    try:
        status = subprocess.run(
            ["systemctl", "is-active", JUICEFS_SERVICE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        svc_state = status.stdout.strip()
    except Exception:
        svc_state = "unknown"

    raise RuntimeError(
        f"juicefs mount did not become ready within 30s at {mount_point} "
        f"(service state: {svc_state}); check journalctl -u {JUICEFS_SERVICE}"
    )


def umount(config: Config) -> None:
    """Stop the JuiceFS systemd service and unmount.

    Surfaces a failed-stop rather than swallowing it.  Idempotent.
    """
    mount_point = juicefs_mount_dir(config)

    if not is_mounted(mount_point):
        # Service might be stopped already; ensure it's disabled so it
        # doesn't auto-start on next boot.
        try:
            _systemctl("disable", "--now", JUICEFS_SERVICE)
        except RuntimeError:
            pass  # already stopped/disabled
        return

    logger.info("Stopping %s systemd service", JUICEFS_SERVICE)
    try:
        # Give the stop generous headroom: JuiceFS's own umount flushes
        # buffered data and its signal handler only force-exits after ~30s, so
        # a 30s cap here races that and spuriously reports a stop failure even
        # though the unmount is about to complete.  The unit's ExecStop uses
        # ``juicefs umount -f`` (lazy) so a busy mount still detaches promptly.
        _systemctl("stop", JUICEFS_SERVICE, timeout=120)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to stop {JUICEFS_SERVICE}; ensure all containers "
            f"using the archive tier are stopped before switching "
            f"backends.  Original: {exc}"
        ) from exc

    _systemctl("disable", JUICEFS_SERVICE)
    logger.info("juicefs unmounted from %s (via systemd)", mount_point)


def _remount(config: Config, s3_access_key_id: str | None, s3_secret_access_key: str | None) -> None:
    """Stop + start the JuiceFS mount so it picks up a changed object store.

    Used after ``juicefs config`` re-points the volume at a new backend:
    the running FUSE process cached the old storage config, so it must be
    restarted to talk to the new store.  Rewrites the env file (adding /
    dropping S3 creds as appropriate) before restarting.
    """
    umount(config)
    # ``umount`` disables the unit; ``mount`` re-enables + starts it with a
    # fresh env file reflecting the new backend's credentials.
    mount(config, s3_access_key_id, s3_secret_access_key)


@attr.s(auto_attribs=True, frozen=True)
class BackendState:
    """Operator-visible archive backend state."""

    backend: str  # "local" (default) | "s3" | "disabled" (legacy pre-v12)
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    s3_prefix: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    juicefs_volume_name: str
    configured_at: str | None
    state_message: str | None


def read_state(db: sqlite3.Connection) -> BackendState:
    row = db.execute(
        "SELECT backend, s3_bucket, s3_region, s3_endpoint, "
        "s3_access_key_id, s3_secret_access_key, juicefs_volume_name, "
        "configured_at, state_message, s3_prefix FROM archive_backend WHERE id = 1"
    ).fetchone()
    if row is None:
        # Defensive fallback for a partial DB; migrations seed this row.
        # The default archive backend is 'local' (always-available archive
        # on a local file-backed JuiceFS volume); operators upgrade to 's3'
        # explicitly.
        return BackendState(
            backend="local",
            s3_bucket=None,
            s3_region=None,
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id=None,
            s3_secret_access_key=None,
            juicefs_volume_name=DEFAULT_VOLUME_NAME,
            configured_at=None,
            state_message=None,
        )
    return BackendState(
        backend=row[0],
        s3_bucket=row[1],
        s3_region=row[2],
        s3_endpoint=row[3],
        s3_access_key_id=row[4],
        s3_secret_access_key=row[5],
        juicefs_volume_name=row[6] or DEFAULT_VOLUME_NAME,
        configured_at=row[7],
        state_message=row[8],
        s3_prefix=row[9],
    )


def _data_section(manifest_raw: str) -> dict[str, Any]:
    """Read just the ``[data]`` table out of a stored manifest.  Tolerant of
    parse errors (returns ``{}``) because callers gate behaviour on this and
    a corrupt row should fail closed."""
    if not manifest_raw:
        return {}
    try:
        return tomllib.loads(manifest_raw).get("data", {}) or {}
    except tomllib.TOMLDecodeError:
        return {}


def manifest_requires_archive(manifest_raw: str) -> bool:
    """``app_archive = true`` means the app cannot run without the archive tier."""
    return bool(_data_section(manifest_raw).get("app_archive"))


def manifest_uses_archive(manifest_raw: str) -> bool:
    """Return True if the app receives the archive mount.

    ``app_archive`` is a hard requirement; ``access_all_archive`` and the
    convenience alias ``access_all_data`` are permissive but still cause
    the app to receive the archive bind-mount when the tier is live, so
    destructive removal still needs the archive healthy to delete its bytes.
    """
    data = _data_section(manifest_raw)
    return bool(data.get("app_archive") or data.get("access_all_archive") or data.get("access_all_data"))


def is_archive_dir_healthy(config: Config, db: sqlite3.Connection) -> bool:
    """True iff the archive tier is usable on the host for the current backend.

    The archive is a JuiceFS mount for both ``'local'`` and ``'s3'``, so in
    both cases it is healthy iff the mount is live.  A transiently-down
    mount (FUSE crash mid-restart, S3 unreachable at boot) blocks operations
    that would otherwise silently orphan or skip archive data.

    ``'disabled'`` is the legacy pre-v12 state (no archive data to protect);
    it passes so legacy zones aren't blocked.
    """
    state = read_state(db)
    if state.backend in ("local", "s3"):
        return is_mounted(juicefs_mount_dir(config))
    return True


def _set_state_message(db: sqlite3.Connection, message: str | None) -> None:
    db.execute("UPDATE archive_backend SET state_message = ? WHERE id = 1", (message,))
    db.commit()


def _set_juicefs_volume_name(db: sqlite3.Connection, volume_name: str) -> None:
    """Persist the JuiceFS volume name (set once, at first-boot local format)."""
    db.execute("UPDATE archive_backend SET juicefs_volume_name = ? WHERE id = 1", (volume_name,))
    db.commit()


def attach_on_startup(config: Config, db: sqlite3.Connection) -> None:
    """Bring the archive backend online at boot.  Failures don't crash boot;
    they're surfaced via state_message so the dashboard stays reachable.

    With the systemd service (``openhost-juicefs.service``), the mount is
    normally started by systemd before this process even boots (the unit
    has ``Before=openhost.service``).  This function handles the case where
    the service hasn't started yet (first boot after provisioning, or if the
    env file is stale/missing) by (re)formatting the local volume when
    needed, writing a fresh env file, and ensuring the service is started.

    For the ``local`` backend it also performs first-boot initialisation:
    if the volume has never been formatted, format it with the file backend
    so the archive tier is available with zero operator configuration.
    """
    state = read_state(db)
    if state.backend == "local":
        try:
            if not is_juicefs_installed(config):
                install_juicefs(config)
            # On first boot, pick a UNIQUE per-zone volume name before formatting
            # (rather than the shared legacy "openhost"), so that if this zone
            # later migrates its local archive into a shared S3 bucket its
            # objects live under a per-zone prefix and can't collide with another
            # zone's.  Only do this if the volume hasn't been formatted yet AND
            # the row still carries the legacy default; a volume that already
            # exists keeps its name (its objects are already keyed under it).
            volume_name = state.juicefs_volume_name
            if not _local_volume_formatted(config) and volume_name == DEFAULT_VOLUME_NAME:
                volume_name = default_volume_name_for_zone(config)
                _set_juicefs_volume_name(db, volume_name)
            _ensure_local_volume_formatted(config, volume_name)
            mount(config)
            _set_state_message(db, None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to bring up local archive backend on startup")
            _set_state_message(db, f"Failed to bring up local archive backend: {exc}")
        return
    if state.backend != "s3":
        return
    try:
        if not is_juicefs_installed(config):
            install_juicefs(config)
        if state.s3_access_key_id is None or state.s3_secret_access_key is None:
            raise RuntimeError(
                "S3 credentials are missing from the archive_backend row.  Re-configure "
                "the archive backend from the dashboard."
            )
        mount(config, state.s3_access_key_id, state.s3_secret_access_key)
        _set_state_message(db, None)
    except Exception as exc:
        logger.exception("Failed to attach archive backend on startup")
        _set_state_message(db, f"Failed to attach archive backend: {exc}")


def _local_volume_formatted(config: Config) -> bool:
    """True iff a JuiceFS volume has actually been FORMATTED in the local meta DB.

    IMPORTANT: this must NOT merely check that the meta.db *file* exists.  When
    JuiceFS opens a ``sqlite3://`` meta address it creates the sqlite file
    immediately — including on a ``mount`` attempt that then fatally exits with
    "database is not formatted" — so a bare file-existence check yields false
    positives.  A false positive is pathological: ``_ensure_local_volume_formatted``
    would skip ``format``, and the ``openhost-juicefs`` unit (Restart=always)
    then fatal-loops forever on the unformatted DB, leaving the archive tier
    permanently down.  This was observed on an upgraded instance whose JuiceFS
    unit created an empty meta.db (via failing mounts) before attach_on_startup
    could format it.

    Instead we look for JuiceFS's own format marker: the ``jfs_setting`` row
    named ``format`` that ``juicefs format`` writes.  Any error (missing file,
    missing table, empty row) is treated as NOT formatted, so we fail safe
    toward (re)formatting rather than toward the fatal-loop.
    """
    meta_db = _juicefs_meta_db(config)
    if not os.path.isfile(meta_db):
        return False
    try:
        conn = sqlite3.connect(f"file:{meta_db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM jfs_setting WHERE name = 'format'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # No jfs_setting table (file created but never formatted) or the DB is
        # locked/corrupt: treat as not formatted so we (re)format it.
        return False
    return bool(row and row[0])


def _ensure_local_volume_formatted(config: Config, juicefs_volume_name: str) -> None:
    """Format the local file-backed volume if it hasn't been yet.  Idempotent.

    Self-healing: because :func:`_local_volume_formatted` checks the real format
    marker (not just the file), a meta.db that exists but was never successfully
    formatted (e.g. created by the restart-looping mount unit before the first
    format) is detected here and formatted.  ``juicefs format`` is idempotent on
    an already-formatted volume, so re-running it is safe.
    """
    if _local_volume_formatted(config):
        return
    format_local_volume(config, juicefs_volume_name or DEFAULT_VOLUME_NAME)


def _s3_client(
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> Any:
    kwargs: dict[str, object] = {
        "aws_access_key_id": s3_access_key_id,
        "aws_secret_access_key": s3_secret_access_key,
    }
    if s3_endpoint:
        kwargs["endpoint_url"] = s3_endpoint
    if s3_region:
        kwargs["region_name"] = s3_region
    return boto3.client("s3", **kwargs)


@attr.s(auto_attribs=True, frozen=True)
class MetaDumpSummary:
    count: int
    latest_at: str | None
    latest_key: str | None


def list_meta_dumps(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str,
) -> MetaDumpSummary | None:
    """Summarise JuiceFS meta-dump objects.  None on error.  Caps at 1000 dumps.

    JuiceFS prefixes every object it writes with the volume name, so dumps land
    at ``<volume>/meta/`` — not under ``s3_prefix`` (which only ever *feeds* the
    volume name, and is null whenever the operator didn't set one).
    """
    volume = (juicefs_volume_name or "").strip("/")
    list_prefix = f"{volume}/meta/" if volume else "meta/"
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        resp = client.list_objects_v2(Bucket=s3_bucket, Prefix=list_prefix, MaxKeys=1000)
    except Exception:
        logger.exception("list_meta_dumps: list_objects_v2 failed")
        return None

    contents = resp.get("Contents") or []
    dumps = [
        obj
        for obj in contents
        if obj.get("Key", "").rsplit("/", 1)[-1].startswith("dump-") and obj.get("Key", "").endswith(".json.gz")
    ]
    if not dumps:
        return MetaDumpSummary(count=0, latest_at=None, latest_key=None)

    latest = max(dumps, key=lambda obj: obj.get("LastModified") or 0)
    last_modified = latest.get("LastModified")
    latest_at: str | None = None
    if last_modified is not None:
        try:
            latest_at = last_modified.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            latest_at = str(last_modified)
    return MetaDumpSummary(
        count=len(dumps),
        latest_at=latest_at,
        latest_key=latest.get("Key"),
    )


def test_s3_credentials(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> str | None:
    """Probe with ``head_bucket``.  None on success, else error string."""
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        client.head_bucket(Bucket=s3_bucket)
    except Exception as exc:
        return f"S3 reachability test failed: {exc}"
    return None


class BackendConfigureError(Exception):
    """Raised by ``configure_backend`` when configuration fails."""


def _endpoint_is_insecure_http(s3_endpoint: str | None) -> bool:
    """True iff a custom endpoint uses plain HTTP (e.g. a same-host MinIO).

    ``juicefs sync`` defaults to HTTPS for S3 URLs and offers no way to encode
    the scheme in the URL, so an HTTP endpoint needs the ``--no-https`` flag or
    it fails with 'server gave HTTP response to HTTPS client'.  (``juicefs
    config``/``mount`` don't need this — their bucket URL carries the scheme.)
    """
    if not s3_endpoint:
        return False
    return s3_endpoint.strip().lower().startswith("http://")


def _sync_objects(
    config: Config,
    *,
    src: str,
    dst: str,
    s3_access_key_id: str | None,
    s3_secret_access_key: str | None,
    insecure: bool = False,
) -> None:
    """Copy every underlying object from ``src`` to ``dst`` with ``juicefs sync``.

    ``src``/``dst`` are JuiceFS object-store URLs (``/abs/path/`` for the file
    backend, ``s3://<bucket>.<endpoint>/<prefix>/`` for S3), each already
    including the volume-name prefix so object keys line up.

    Credentials for the S3 side are passed via ``AWS_ACCESS_KEY_ID`` /
    ``AWS_SECRET_ACCESS_KEY`` in the environment.  ``juicefs sync`` only reads
    credentials from the URL (``AK:SK@bucket.endpoint``) or the standard AWS
    SDK credential chain — NOT from JuiceFS's own ``ACCESS_KEY``/``SECRET_KEY``
    vars — so we use the AWS_* env form to keep the secret out of ``ps``.

    Used for the ``local`` -> ``s3`` migration, where only ONE side is S3.
    For ``s3`` -> ``s3`` (where the two ends can have different credentials
    and endpoints) use :func:`_sync_objects_s3_to_s3`, which encodes each
    side's creds in its own URL.

    Raises on any sync failure.  Never deletes the source.
    """
    cmd = [
        _juicefs_binary(config),
        "--no-agent",
        "sync",
        # --check-all re-reads and compares every synced object so a short /
        # corrupt copy is caught before we re-point the volume.
        "--check-all",
    ]
    if insecure:
        # Plain-HTTP endpoint (e.g. a same-host MinIO): juicefs sync would
        # otherwise force HTTPS and fail the TLS handshake.
        cmd.append("--no-https")
    cmd += [src, dst]
    env = os.environ.copy()
    if s3_access_key_id is not None and s3_secret_access_key is not None:
        env["AWS_ACCESS_KEY_ID"] = s3_access_key_id
        env["AWS_SECRET_ACCESS_KEY"] = s3_secret_access_key
    logger.info("juicefs sync %s -> %s", src, dst)
    # Generous but bounded timeout: large archives on slow S3 links take a
    # while, but a wedged sync must not hang the configure request forever.
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=6 * 60 * 60)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}"
        raise RuntimeError(f"juicefs sync failed: {detail}")


def _s3_url_with_creds(url: str, access_key_id: str, secret_access_key: str) -> str:
    """Return an ``s3://`` URL with ``AK:SK@`` credentials spliced into the host.

    ``juicefs sync`` reads per-endpoint credentials from the URL itself
    (``s3://ACCESS:SECRET@bucket.endpoint/prefix/``).  A single pair of
    ``AWS_*`` env vars cannot describe TWO different S3 endpoints, so for an
    ``s3`` -> ``s3`` migration between providers with distinct credentials we
    must encode each side's creds in its own URL.  The keys are percent-encoded
    so a secret containing ``/``, ``@``, ``:`` or ``+`` (common in AWS/MinIO
    secret keys) can't corrupt the URL structure.
    """
    from urllib.parse import quote  # noqa: PLC0415

    scheme, rest = url.split("://", 1)
    ak = quote(access_key_id, safe="")
    sk = quote(secret_access_key, safe="")
    return f"{scheme}://{ak}:{sk}@{rest}"


def _sync_objects_s3_to_s3(
    config: Config,
    *,
    src: str,
    dst: str,
    src_access_key_id: str,
    src_secret_access_key: str,
    dst_access_key_id: str,
    dst_secret_access_key: str,
    insecure: bool = False,
) -> None:
    """``juicefs sync`` an S3 object store to another S3 object store.

    Unlike :func:`_sync_objects` (one S3 side, creds via ``AWS_*`` env), both
    ends here are S3 and may use different providers/credentials, so each
    side's credentials are encoded in its own URL via
    :func:`_s3_url_with_creds`.  This puts the keys on ``juicefs sync``'s argv
    (visible in ``ps`` for the lifetime of this one-shot command) — an
    acceptable trade on a single-tenant, root-only host, matching the same
    trade already made by ``juicefs config`` for ``--secret-key``.

    ``--check-all`` verifies every synced object; raises on failure and never
    deletes the source.
    """
    cmd = [
        _juicefs_binary(config),
        "--no-agent",
        "sync",
        "--check-all",
    ]
    if insecure:
        cmd.append("--no-https")
    cmd += [
        _s3_url_with_creds(src, src_access_key_id, src_secret_access_key),
        _s3_url_with_creds(dst, dst_access_key_id, dst_secret_access_key),
    ]
    # Log without the credential-bearing URLs.
    logger.info("juicefs sync (s3->s3) %s -> %s", src, dst)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 60 * 60)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}"
        # Never surface the credential-bearing argv in the error.
        raise RuntimeError(f"juicefs sync (s3->s3) failed: {detail}")


def _reconfigure_volume_storage(
    config: Config,
    *,
    storage: str,
    bucket: str,
    s3_access_key_id: str | None,
    s3_secret_access_key: str | None,
) -> None:
    """Re-point the existing volume at a new object store with ``juicefs config``.

    Only changes the DATA STORAGE fields; the metadata (every file, dir,
    mode, uid/gid) is untouched, so the switch is transparent to apps once
    the mount is restarted.
    """
    cmd = [
        _juicefs_binary(config),
        "--no-agent",
        "config",
        _format_meta_dsn(config),
        "--storage",
        storage,
        "--bucket",
        bucket,
        "--yes",
        "--force",
    ]
    env = os.environ.copy()
    if s3_access_key_id is not None:
        cmd += ["--access-key", s3_access_key_id]
    if s3_secret_access_key is not None:
        # The secret must be passed as a literal to ``juicefs config``: the
        # ``env:VAR`` indirection that ``mount``/``sync`` accept is NOT
        # resolved by ``config`` (it would store the literal string
        # "env:SECRET_KEY" and every subsequent upload would fail with
        # SignatureDoesNotMatch).  This puts the secret on argv briefly
        # (visible in ``ps`` for the lifetime of this one-shot command); it's
        # an acceptable trade on a single-tenant, root-only host, and the
        # secret is already stored (encrypted) in the meta DB afterwards.
        cmd += ["--secret-key", s3_secret_access_key]
    logger.info("juicefs config: re-point volume storage -> %s (%s)", storage, bucket)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit {result.returncode}"
        raise RuntimeError(f"juicefs config (re-point storage) failed: {detail}")


def _local_sync_source(config: Config, volume: str) -> str:
    """The ``juicefs sync`` SOURCE for the local file object store.

    Points at ``<store>/<volume>/`` (trailing slash) so object keys under
    the volume prefix map 1:1 onto the destination.
    """
    return os.path.join(_file_bucket(local_object_store_dir(config)), volume) + "/"


def _s3_sync_dest(
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    volume: str,
) -> str:
    """The ``juicefs sync`` DESTINATION URL for the S3 object store.

    ``juicefs sync`` parses S3 URLs as
    ``s3://[AK:SK@]BUCKET.ENDPOINT[/PREFIX]`` — i.e. the bucket is the FIRST
    dot-segment of the host, followed by the endpoint host.  We omit inline
    creds (they'd leak into ``ps``) and supply them via AWS_* env instead.
    The ``<volume>/`` prefix mirrors the source so keys line up.
    """
    if s3_endpoint:
        # Custom endpoint (MinIO, R2, etc.): s3://<bucket>.<endpoint-host>/<volume>/
        host = s3_endpoint.split("://", 1)[-1].rstrip("/")
        return f"s3://{s3_bucket}.{host}/{volume}/"
    region = s3_region or "us-east-1"
    return f"s3://{s3_bucket}.s3.{region}.amazonaws.com/{volume}/"


# The ``juicefs sync`` SOURCE for an S3 object store is built identically to
# the destination (same URL grammar); alias for call-site clarity.
_s3_sync_source = _s3_sync_dest


def _migrate_s3_to_s3(
    config: Config,
    *,
    volume: str,
    src_bucket: str,
    src_region: str | None,
    src_endpoint: str | None,
    src_access_key_id: str,
    src_secret_access_key: str,
    dst_bucket: str,
    dst_region: str | None,
    dst_endpoint: str | None,
    dst_access_key_id: str,
    dst_secret_access_key: str,
) -> None:
    """Migrate the archive from one S3 object store to another, JuiceFS-native.

    Mirrors :func:`_migrate_local_to_s3` but with an S3 source: the metadata
    DB (every file, dir, mode, uid/gid) is local and never re-formatted, so
    only the underlying objects move and everything is preserved.

    1. ``juicefs sync`` copies every object under ``<volume>/`` from the OLD
       bucket to the NEW bucket, ``--check-all`` verifying each.  Both ends'
       credentials are carried in their own URLs so the two providers can
       differ.
    2. ``juicefs config --storage s3 --bucket ...`` re-points the same volume
       at the NEW bucket + credentials.

    FAIL-OPEN CONTRACT: either fully succeeds or raises, and never deletes the
    source objects.  The re-point (step 2) is the only thing that changes what
    the volume reads from, so a failure in step 1 leaves the volume still
    pointing at the intact OLD bucket.  The caller only flips the DB row and
    reclaims the old objects AFTER this returns successfully AND the mount has
    been restarted against the new bucket.
    """
    src = _s3_sync_source(src_bucket, src_region, src_endpoint, volume)
    dst = _s3_sync_dest(dst_bucket, dst_region, dst_endpoint, volume)
    _sync_objects_s3_to_s3(
        config,
        src=src,
        dst=dst,
        src_access_key_id=src_access_key_id,
        src_secret_access_key=src_secret_access_key,
        dst_access_key_id=dst_access_key_id,
        dst_secret_access_key=dst_secret_access_key,
        # --no-https is a single global flag in juicefs sync; enable it if
        # EITHER endpoint is plain HTTP (e.g. a same-host MinIO target).  An
        # HTTPS AWS source still works under --no-https because the source URL
        # carries its own https scheme via the amazonaws.com host resolution.
        insecure=_endpoint_is_insecure_http(src_endpoint) or _endpoint_is_insecure_http(dst_endpoint),
    )
    _reconfigure_volume_storage(
        config,
        storage="s3",
        bucket=_bucket_url(dst_bucket, dst_region or "us-east-1", dst_endpoint),
        s3_access_key_id=dst_access_key_id,
        s3_secret_access_key=dst_secret_access_key,
    )


def _migrate_local_to_s3(
    config: Config,
    *,
    volume: str,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
) -> None:
    """Migrate the local file-backed archive into S3, JuiceFS-native.

    Steps (the volume + meta DB are never re-formatted, so all file
    metadata is preserved):

    1. ``juicefs sync`` copies every underlying object from the local file
       store to the S3 bucket (both under the ``<volume>/`` prefix), with
       ``--check-all`` verifying each object.
    2. ``juicefs config --storage s3 --bucket ...`` re-points the same
       volume at the S3 store.

    FAIL-OPEN CONTRACT: this must either fully succeed or raise, and it
    never deletes the local objects.  Because the re-point (step 2) is the
    only thing that changes what the volume reads from, a failure in step 1
    leaves the volume still pointing at the intact local store.  The caller
    only flips the DB row and reclaims the local objects AFTER this returns
    successfully AND the mount has been restarted against S3.
    """
    dst = _s3_sync_dest(s3_bucket, s3_region, s3_endpoint, volume)
    src = _local_sync_source(config, volume)
    _sync_objects(
        config,
        src=src,
        dst=dst,
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
        insecure=_endpoint_is_insecure_http(s3_endpoint),
    )
    _reconfigure_volume_storage(
        config,
        storage="s3",
        bucket=_bucket_url(s3_bucket, s3_region or "us-east-1", s3_endpoint),
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
    )


def configure_backend(
    config: Config,
    db: sqlite3.Connection,
    *,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_prefix: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    juicefs_volume_name: str | None = None,
    quiesce_archive_apps: Callable[[], None] | None = None,
) -> None:
    """Configure / re-configure the S3 archive backend, in one shot.

    Allowed starting states:

    * ``'local'`` (the default) — archive data lives in the local file store
      and is MIGRATED into S3 via ``juicefs sync`` + ``juicefs config``.
    * ``'disabled'`` (legacy pre-v12 zone with no volume) — formatted fresh
      against S3.
    * ``'s3'`` — the archive is ALREADY on S3 and is MIGRATED to a DIFFERENT
      S3 bucket/provider (e.g. AWS -> MinIO, or bucket rotation), via
      ``juicefs sync`` (old bucket -> new bucket, under the same volume
      prefix) + ``juicefs config`` re-point.  The source credentials come
      from the current DB state.

    The volume name (object prefix) is fixed once a volume exists and is
    always preserved across a migration, so metadata (every file, dir, mode,
    uid/gid — held in the LOCAL meta DB, never re-formatted) is untouched.

    ``quiesce_archive_apps`` (when provided) is called just before the sync,
    to STOP every running archive-using app.  This is required: no app may
    write into the source store once the sync starts (that write would be
    lost), and ``systemctl stop openhost-juicefs`` cannot unmount the FUSE
    filesystem while an app container holds it open (the unmount times out).
    The caller is responsible for RE-STARTING those apps afterwards (the web
    route records the quiesced app ids and calls ``start_apps_by_id`` in a
    ``finally``), which re-opens the now-migrated archive.

    FAIL-OPEN: if any step before the DB flip fails, the volume is left
    pointing at the intact SOURCE store (best-effort remounted) and the DB
    backend/credentials are unchanged, so the operator's files remain
    available.  The source objects are deleted only after the switch has been
    committed and the mount restarted against the new store.
    """
    state = read_state(db)
    if state.backend not in ("local", "disabled", "s3"):
        raise BackendConfigureError(f"cannot configure S3 from backend={state.backend!r}")

    migrating_from_local = state.backend == "local"
    migrating_from_s3 = state.backend == "s3"
    if migrating_from_s3:
        # Source creds must exist for an s3->s3 migration; without them we
        # cannot read the old bucket to copy it forward.
        if not state.s3_bucket or state.s3_access_key_id is None or state.s3_secret_access_key is None:
            raise BackendConfigureError(
                "current S3 archive backend is missing its bucket/credentials in the "
                "database; cannot migrate to a new bucket.  Re-provision or contact support."
            )

    # Pick the volume name (== object prefix) for this configuration.
    #
    # s3 -> s3: the volume name MUST be preserved exactly.  The migration syncs
    # objects from ``<old-bucket>/<volume>/`` to ``<new-bucket>/<volume>/`` and
    # the JuiceFS meta DB already keys chunks under it; renaming it (even a
    # legacy shared ``openhost``) would point the sync SOURCE at an empty prefix
    # and copy nothing.  A legacy ``openhost`` prefix in a shared bucket is not
    # ideal, but rotating buckets is not the place to fix it — keep it stable.
    #
    # local -> s3: keep the already-formatted local volume name, UNLESS it is
    # still the legacy shared default ``openhost`` — in that case an operator
    # prefix/name (or a unique per-zone default) is used so the migrated objects
    # are isolated in a shared bucket.  This rename is safe here because the
    # local file store is re-keyed under the new name as part of the sync and no
    # S3 objects exist under either name yet.  Fresh zones already format under a
    # unique per-zone name, so this legacy branch only fires for old zones.
    #
    # disabled -> s3: no volume yet; pick a fresh (prefix/unique) name.
    if migrating_from_s3:
        volume = state.juicefs_volume_name or DEFAULT_VOLUME_NAME
    elif migrating_from_local:
        existing = state.juicefs_volume_name or DEFAULT_VOLUME_NAME
        if existing == DEFAULT_VOLUME_NAME:
            volume = juicefs_volume_name or s3_prefix or default_volume_name_for_zone(config)
        else:
            volume = existing
    else:
        volume = juicefs_volume_name or s3_prefix or default_volume_name_for_zone(config)

    try:
        if not is_juicefs_installed(config):
            install_juicefs(config)

        if migrating_from_local:
            # Make sure the local volume is up so its object store is complete
            # and consistent before we sync it.
            _ensure_local_volume_formatted(config, volume)
            mount(config)
            # Stop archive-using apps FIRST, for two reasons:
            #  1. Consistency: no app may write into the local store after we
            #     start syncing, or that write would be lost (it wouldn't be in
            #     S3 and we're about to re-point the volume there).
            #  2. Unmount: a container holding the FUSE mount open makes the
            #     later ``systemctl stop`` (unmount) time out.
            # The caller restarts them after we return.
            if quiesce_archive_apps is not None:
                quiesce_archive_apps()
            # Copy objects into S3 and re-point the volume's storage.  On any
            # failure here the volume still reads from the local store.
            _migrate_local_to_s3(
                config,
                volume=volume,
                s3_bucket=s3_bucket,
                s3_region=s3_region,
                s3_endpoint=s3_endpoint,
                s3_access_key_id=s3_access_key_id,
                s3_secret_access_key=s3_secret_access_key,
            )
            # Restart the mount so the FUSE process talks to S3 now.
            _remount(config, s3_access_key_id, s3_secret_access_key)
        elif migrating_from_s3:
            # The current S3 mount must be live so the source bucket is
            # complete + consistent before we sync it.
            mount(config, state.s3_access_key_id, state.s3_secret_access_key)
            # Stop archive-using apps for the same two reasons as the local
            # case: consistency (no writes race the sync) and unmount (a held
            # FUSE mount blocks the later restart).
            if quiesce_archive_apps is not None:
                quiesce_archive_apps()
            # Copy objects OLD bucket -> NEW bucket and re-point the volume.
            # On any failure the volume still reads from the intact old bucket.
            assert state.s3_bucket is not None
            assert state.s3_access_key_id is not None
            assert state.s3_secret_access_key is not None
            _migrate_s3_to_s3(
                config,
                volume=volume,
                src_bucket=state.s3_bucket,
                src_region=state.s3_region,
                src_endpoint=state.s3_endpoint,
                src_access_key_id=state.s3_access_key_id,
                src_secret_access_key=state.s3_secret_access_key,
                dst_bucket=s3_bucket,
                dst_region=s3_region,
                dst_endpoint=s3_endpoint,
                dst_access_key_id=s3_access_key_id,
                dst_secret_access_key=s3_secret_access_key,
            )
            # Restart the mount so the FUSE process talks to the NEW bucket now.
            _remount(config, s3_access_key_id, s3_secret_access_key)
        else:
            # Legacy 'disabled' zone: no data, just format+mount S3 fresh.
            format_s3_volume(
                config,
                s3_bucket=s3_bucket,
                s3_region=s3_region,
                s3_endpoint=s3_endpoint,
                s3_access_key_id=s3_access_key_id,
                s3_secret_access_key=s3_secret_access_key,
                juicefs_volume_name=volume,
            )
            mount(config, s3_access_key_id, s3_secret_access_key)

        # Persist the new state.  If this fails after a successful re-point +
        # remount we leave a "live S3 mount + local/disabled DB row"
        # inconsistency, but a subsequent configure_backend call retries
        # idempotently (sync is incremental; re-point is a no-op on an
        # already-s3 volume).
        db.execute(
            "UPDATE archive_backend SET "
            "backend='s3', s3_bucket=?, s3_region=?, s3_endpoint=?, s3_prefix=?, "
            "s3_access_key_id=?, s3_secret_access_key=?, juicefs_volume_name=?, "
            "configured_at=?, state_message=NULL "
            "WHERE id = 1",
            (
                s3_bucket,
                s3_region,
                s3_endpoint,
                # Store the ACTUAL object prefix (the volume name), so the
                # reported state matches where objects really live rather than
                # an operator-supplied prefix that a pre-existing volume ignored.
                volume,
                s3_access_key_id,
                s3_secret_access_key,
                volume,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        db.commit()
    except Exception as exc:
        # Fail-open: for a local zone, get the volume back to reading from
        # the intact local object store so the operator keeps working.  We
        # only ever re-pointed storage; the local objects were never
        # touched, so restore the storage config + remount local.
        if migrating_from_local:
            try:
                _reconfigure_volume_storage(
                    config,
                    storage="file",
                    bucket=_file_bucket(local_object_store_dir(config)),
                    s3_access_key_id=None,
                    s3_secret_access_key=None,
                )
                _remount(config, None, None)
            except Exception:  # noqa: BLE001
                # The local objects are still intact (never touched) and the
                # volume is re-pointed at them, but we couldn't bring the mount
                # back up right now (e.g. the old FUSE process was slow to
                # release while an app held it open).  attach_on_startup will
                # remount cleanly on the next boot / service restart; surface a
                # state_message so the operator knows to restart if they don't
                # want to wait.
                logger.exception("failed to restore local archive mount after aborted migration")
                try:
                    _set_state_message(
                        db,
                        "Migration to S3 failed and the local archive mount could not be "
                        "restarted automatically; your archive data is intact on local disk. "
                        "Restart the instance (or the openhost service) to bring the archive "
                        "back online, then retry.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to record archive state_message after aborted migration")
        elif migrating_from_s3:
            # Re-point the volume back at the intact OLD bucket + creds so the
            # operator keeps working.  We only ever re-pointed storage; the old
            # objects were never touched, and the DB row still describes the old
            # bucket (it is flipped only on success), so a retry is safe.
            try:
                assert state.s3_bucket is not None
                assert state.s3_access_key_id is not None
                assert state.s3_secret_access_key is not None
                _reconfigure_volume_storage(
                    config,
                    storage="s3",
                    bucket=_bucket_url(state.s3_bucket, state.s3_region or "us-east-1", state.s3_endpoint),
                    s3_access_key_id=state.s3_access_key_id,
                    s3_secret_access_key=state.s3_secret_access_key,
                )
                _remount(config, state.s3_access_key_id, state.s3_secret_access_key)
            except Exception:  # noqa: BLE001
                logger.exception("failed to restore original S3 archive mount after aborted migration")
                try:
                    _set_state_message(
                        db,
                        "Migration to a new S3 bucket failed and the original archive mount "
                        "could not be restarted automatically; your archive data is intact in "
                        "the original bucket. Restart the instance (or the openhost service) to "
                        "bring the archive back online, then retry.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to record archive state_message after aborted s3->s3 migration")
        else:
            try:
                umount(config)
            except Exception:  # noqa: BLE001
                logger.exception("failed to unmount juicefs after aborted S3 format")
        raise BackendConfigureError(f"Failed to configure S3 archive backend: {exc}") from exc

    # DB is now committed to 's3' and the volume reads from the new store.
    # Only now is it safe to reclaim the source objects.  A failure here is
    # non-fatal: the data is already durably in the new bucket; stale source
    # objects just waste storage.
    if migrating_from_local:
        _remove_local_object_store(config)
    elif migrating_from_s3:
        # Delete the old bucket's objects under this volume's prefix only.
        # Scoped strictly to ``<old-bucket>/<volume>/`` so a shared bucket's
        # other zones are never touched.
        assert state.s3_bucket is not None
        assert state.s3_access_key_id is not None
        assert state.s3_secret_access_key is not None
        # Skip reclaim in the degenerate case where the new store is the SAME
        # bucket+endpoint+prefix as the old (a no-op "migration"): deleting the
        # prefix would wipe the data we just kept in place.
        same_location = (
            state.s3_bucket == s3_bucket
            and (state.s3_endpoint or None) == (s3_endpoint or None)
            and (state.s3_region or None) == (s3_region or None)
        )
        if not same_location:
            _remove_s3_object_prefix(
                s3_bucket=state.s3_bucket,
                s3_region=state.s3_region,
                s3_endpoint=state.s3_endpoint,
                s3_access_key_id=state.s3_access_key_id,
                s3_secret_access_key=state.s3_secret_access_key,
                volume=volume,
            )


def _remove_local_object_store(config: Config) -> None:
    """Delete the local file object store after a successful migration to S3.

    The objects are plain files owned by the ``host`` user (JuiceFS's file
    backend writes them as the mounting user, not a container-mapped subuid),
    so a straightforward ``shutil.rmtree`` suffices — no ``podman unshare``
    gymnastics needed, unlike the old per-app-file copy.  Non-fatal: the data
    is already durably in S3; a stale local dir just wastes disk.
    """
    path = local_object_store_dir(config)
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, ignore_errors=False)
        os.makedirs(path, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception(
            "migrated local archive to S3 but failed to remove the local object store at %s; safe to delete manually",
            path,
        )


def _remove_s3_object_prefix(
    *,
    s3_bucket: str,
    s3_region: str | None,
    s3_endpoint: str | None,
    s3_access_key_id: str,
    s3_secret_access_key: str,
    volume: str,
) -> None:
    """Delete every object under ``<bucket>/<volume>/`` in an S3 store.

    Called to reclaim the OLD bucket after a successful ``s3`` -> ``s3``
    migration.  Strictly scoped to the ``<volume>/`` prefix so a bucket shared
    by several zones (each keyed under its own per-zone volume prefix) is never
    otherwise touched.  Non-fatal: the data is already durable in the new
    bucket; stale objects only waste storage.

    An empty/whitespace ``volume`` would produce an unscoped prefix that could
    match the whole bucket, so it is refused outright.
    """
    prefix = (volume or "").strip().strip("/").strip()
    if not prefix:
        logger.error("refusing to reclaim S3 objects: empty volume prefix would target the whole bucket")
        return
    list_prefix = f"{prefix}/"
    try:
        client = _s3_client(s3_region, s3_endpoint, s3_access_key_id, s3_secret_access_key)
        paginator = client.get_paginator("list_objects_v2")
        to_delete: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=s3_bucket, Prefix=list_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                # Defence-in-depth: only ever delete keys that really live
                # under the volume prefix (guards against any pagination or
                # server quirk returning an out-of-prefix key).
                if key and key.startswith(list_prefix):
                    to_delete.append({"Key": key})
                    if len(to_delete) == 1000:
                        client.delete_objects(Bucket=s3_bucket, Delete={"Objects": to_delete, "Quiet": True})
                        to_delete = []
        if to_delete:
            client.delete_objects(Bucket=s3_bucket, Delete={"Objects": to_delete, "Quiet": True})
    except Exception:  # noqa: BLE001
        logger.exception(
            "migrated archive to new S3 bucket but failed to reclaim old objects under %s/%s; safe to delete manually",
            s3_bucket,
            list_prefix,
        )
