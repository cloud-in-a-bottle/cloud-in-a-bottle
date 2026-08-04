from __future__ import annotations

import os
import secrets as secrets_mod
import shutil
import stat
import subprocess

from compute_space.core.containers import ROUTER_GATEWAY_HOST
from compute_space.core.containers import ROUTER_LOOPBACK_HOST
from compute_space.core.email.service import ROUTER_SMTP_PORT
from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest


def rmtree_with_sudo_fallback(path: str, *, raise_on_failure: bool = False) -> None:
    """Remove a directory tree, falling back to ``sudo -n rm -rf``.

    The fast path is ``shutil.rmtree`` with an onexc hook that chmods
    read-only entries (git-clone artefacts) and retries.  The sudo
    fallback relies on the NOPASSWD sudoers rule installed by ansible.

    With ``raise_on_failure=False`` both stages log and swallow errors
    so cleanup failures can't block the deprovision flow.  With
    ``raise_on_failure=True`` the error re-raises.
    """
    if not os.path.exists(path):
        return

    def _make_writable_and_retry(func, err_path, _exc):  # type: ignore[no-untyped-def]
        # Unlinking a file needs write perms on the parent directory, not
        # the file itself — so chmod both.  Git clones leave read-only
        # dirs as well as read-only files.
        os.chmod(err_path, stat.S_IRWXU)
        parent = os.path.dirname(err_path)
        if parent and parent != err_path:
            try:
                os.chmod(parent, stat.S_IRWXU)
            except OSError:
                pass
        func(err_path)

    try:
        shutil.rmtree(path, onexc=_make_writable_and_retry)
        return
    except OSError as rmtree_err:
        logger.warning(
            "rmtree failed on %s (%s), falling back to sudo rm -rf",
            path,
            rmtree_err,
        )

    try:
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", path],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as sudo_err:
        # Surface captured stderr so the operator sees 'sudo: a password
        # is required' etc. without re-running the command by hand.
        stderr = (sudo_err.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.warning(
            "Failed to clean data dir %s via sudo (exit %d): %s",
            path,
            sudo_err.returncode,
            stderr or "<no stderr>",
        )
        if raise_on_failure:
            raise
    except (subprocess.TimeoutExpired, OSError) as sudo_err:
        logger.warning("Failed to clean data dir %s via sudo: %s", path, sudo_err)
        if raise_on_failure:
            raise


def provision_data(
    app_id: str,
    app_name: str,
    manifest: AppManifest,
    data_dir: str,
    temp_data_dir: str,
    archive_dir: str,
    my_openhost_redirect_domain: str,
    zone_domain: str,
    port: int,
    owner_username: str,
) -> dict[str, str]:
    """Create data directories for an app based on manifest permissions.
    Returns a dict of environment variable name -> value.

    By default, apps receive a permanent data directory (app_data defaults
    to True).  Additional storage tiers must be explicitly requested via
    app_temp_data, app_archive, access_all_app_data, access_all_archive,
    or the convenience shorthand access_all_data (which implies both
    access_all_app_data and access_all_archive).
    SQLite entries also implicitly enable app_data.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    app_archive_dir = os.path.join(archive_dir, app_name)
    env_vars = {}

    # Determine if permanent data dir is needed:
    # app_data defaults true; also enabled by sqlite entries or cross-app access.
    wants_all_app_data = manifest.access_all_app_data
    wants_all_archive = manifest.access_all_archive
    needs_app_data = manifest.app_data or manifest.sqlite_dbs or wants_all_app_data

    if needs_app_data:
        os.makedirs(app_data_dir, exist_ok=True)
        env_vars["OPENHOST_APP_DATA_DIR"] = app_data_dir

        sqlite_dir = os.path.join(app_data_dir, "sqlite")
        if manifest.sqlite_dbs:
            os.makedirs(sqlite_dir, exist_ok=True)
        for db_name in manifest.sqlite_dbs:
            db_path = os.path.join(sqlite_dir, f"{db_name}.db")
            # Don't create the file — let the app create it so its init logic
            # (e.g. "if not exists: create tables") triggers correctly.
            env_key = f"OPENHOST_SQLITE_{db_name}"
            env_vars[env_key] = db_path
            upper_key = f"OPENHOST_SQLITE_{db_name.upper()}"
            if upper_key != env_key:
                env_vars[upper_key] = db_path

    if manifest.app_temp_data or wants_all_app_data:
        os.makedirs(app_temp_dir, exist_ok=True)
        env_vars["OPENHOST_APP_TEMP_DIR"] = app_temp_dir

    archive_available = os.path.isdir(archive_dir)
    if manifest.app_archive:
        # Do NOT auto-mkdir a missing archive_dir: a local path created
        # here would be shadowed once the JuiceFS mount lands, silently
        # losing whatever apps wrote in the meantime.  Fail loudly.
        if not archive_available:
            raise RuntimeError(
                f"archive_dir {archive_dir!r} does not exist or is not a "
                f"directory.  If the S3 backend is configured, check "
                f"that ``juicefs-mount`` is healthy; otherwise check "
                f"that ``Config.make_all_dirs()`` ran on startup."
            )
        os.makedirs(app_archive_dir, exist_ok=True)
        env_vars["OPENHOST_APP_ARCHIVE_DIR"] = app_archive_dir
    elif wants_all_archive and archive_available:
        # access_all_archive is permissive — skip the archive mount when
        # the tier isn't configured rather than refusing to provision.
        os.makedirs(app_archive_dir, exist_ok=True)
        env_vars["OPENHOST_APP_ARCHIVE_DIR"] = app_archive_dir

    # Always create temp dir for internal use (repo clone, logs) even if
    # the app doesn't get access to it
    os.makedirs(app_temp_dir, exist_ok=True)

    env_vars["OPENHOST_APP_NAME"] = app_name
    env_vars["OPENHOST_APP_ID"] = app_id

    # Generate app token for cross-app service calls
    env_vars["OPENHOST_APP_TOKEN"] = secrets_mod.token_urlsafe(32)

    # The host apps use to reach the router (same for HTTP and the email SMTP
    # listener): the loopback for network_host containers, the podman host-gateway
    # alias for pasta containers.
    router_host = ROUTER_LOOPBACK_HOST if manifest.network_host else ROUTER_GATEWAY_HOST
    env_vars["OPENHOST_ROUTER_URL"] = f"http://{router_host}:{port}"

    # Endpoint of the router's outbound email SMTP submission listener (the
    # 'email' v2 service). Apps granted the email 'send' permission relay through
    # this with SMTP AUTH (username = app name, password = OPENHOST_APP_TOKEN); the
    # router attaches the real relay credential and forwards to the Imbue smarthost.
    env_vars["OPENHOST_SMTP_HOST"] = router_host
    env_vars["OPENHOST_SMTP_PORT"] = str(ROUTER_SMTP_PORT)

    # Zone identity info so apps can build federated auth flows
    env_vars["OPENHOST_ZONE_DOMAIN"] = zone_domain

    env_vars["OPENHOST_MY_REDIRECT_DOMAIN"] = my_openhost_redirect_domain

    env_vars["OPENHOST_OWNER_USERNAME"] = owner_username

    return env_vars


def deprovision_temp_data(app_name: str, temp_data_dir: str) -> None:
    """Remove the app's temp dir (repo clone, build artefacts, logs,
    OPENHOST_APP_TEMP_DIR contents).  Persistent data is not touched.
    """
    rmtree_with_sudo_fallback(os.path.join(temp_data_dir, "app_temp_data", app_name))


def deprovision_data(
    app_name: str,
    data_dir: str,
    temp_data_dir: str,
    archive_dir: str,
) -> None:
    """Remove all data for an app from persistent, archive, and temp disks."""
    rmtree_with_sudo_fallback(os.path.join(data_dir, "app_data", app_name))
    rmtree_with_sudo_fallback(os.path.join(archive_dir, app_name))
    deprovision_temp_data(app_name, temp_data_dir)
