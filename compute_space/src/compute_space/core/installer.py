"""Programmatic app install for the ``installer`` v2 service.

The v2 service proxy in :mod:`compute_space.web.routes.services_v2`
intercepts requests whose resolved service URL is
``INSTALLER_SERVICE_URL`` and dispatches them here.  The functions in
this module are also the seam used by the default-apps deploy hook to
install builtin apps from remote URLs (see
:mod:`compute_space.core.default_apps`).
"""

from __future__ import annotations

import shutil
import sqlite3

import attr

from compute_space.config import Config
from compute_space.core.apps import all_manifest_permissions_v2
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import validate_manifest
from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.oauth import OAuthRequired

# Service URL the v2 service proxy matches against to dispatch to this
# module instead of resolving a normal provider app.
INSTALLER_SERVICE_URL = "github.com/imbue-openhost/openhost/services/installer"

# Service version this build of the installer exposes.  v2 callers send a
# SemVer specifier (e.g. ``>=0.1.0``) in their manifest's
# [[services.v2.consumes]].version; the proxy checks it against this
# constant.
INSTALLER_SERVICE_VERSION = "0.1.0"

# Grant payload keys understood by ``check_install_allowed``.
GRANT_KEY_CAPABILITY = "capability"
GRANT_KEY_REPO_URL_PREFIX = "repo_url_prefix"
INSTALL_CAPABILITY = "install"


@attr.s(auto_attribs=True, frozen=True)
class InstallResult:
    """Successful install result returned by ``install_from_repo_url``."""

    app_name: str
    status: str  # always "building" — the actual build runs in a daemon thread


def check_install_allowed(repo_url: str, grants: list[Grant]) -> str | None:
    """Validate the caller's grants against the requested ``repo_url``.

    Returns ``None`` if at least one grant permits installing the given
    ``repo_url``; otherwise returns a human-readable reason describing
    the missing grant.  The caller is responsible for turning that into
    a 403 permission_required response.

    The installer expects dict-shaped grants with
    ``capability == "install"`` and a ``repo_url_prefix`` string.  Non-
    dict grants (strings, lists) are skipped; they may be present if the
    catalog also requests other capabilities later.
    """
    if not grants:
        return "no installer grants present"
    for g in grants:
        if not isinstance(g, dict):
            continue
        if g.get(GRANT_KEY_CAPABILITY) != INSTALL_CAPABILITY:
            continue
        prefix = g.get(GRANT_KEY_REPO_URL_PREFIX, "")
        if not isinstance(prefix, str):
            continue
        if prefix in ("", "*"):
            return None
        if repo_url.startswith(prefix):
            return None
    return "no installer grant matches the requested repo_url"


async def install_from_repo_url(
    repo_url: str,
    config: Config,
    db: sqlite3.Connection,
    *,
    app_name: str | None = None,
    installed_by: str | None = None,
) -> InstallResult:
    """Clone ``repo_url``, validate, and kick off a background build.

    Mirrors the ``/api/add_app`` flow but is callable from sync core
    contexts (default-apps deploy hook) and from the v2 service proxy.

    ``installed_by`` records the consumer app that requested the install
    (or ``None`` for owner-initiated installs).  Threaded through
    ``insert_and_deploy`` so it lands on the apps row in the same
    INSERT — no separate UPDATE that could race with the background
    build thread's status writes.

    Raises ``ValueError`` for install failures and ``OAuthRequired`` when the
    owner must authorize access to the repository.
    """
    if not repo_url:
        raise ValueError("repo_url is required")

    manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to="/")
    if authorize_url is not None:
        # The owner needs to approve GitHub OAuth to clone a private repo.
        # The installer service is server-to-server; we don't have a
        # browser session to redirect.  Surface the URL in the error.
        raise OAuthRequired(authorize_url)
    if error or manifest is None or clone_dir is None:
        raise ValueError(f"clone failed: {error or 'unknown error'}")

    effective_name = app_name or manifest.name
    validation_error = validate_manifest(manifest, db, app_name=effective_name)
    if validation_error is not None:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise ValueError(validation_error)

    final_dir = move_clone_to_app_temp_dir(clone_dir, effective_name, config)

    try:
        insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            permissions_v2_grants=all_manifest_permissions_v2(manifest),
            app_name=effective_name,
            repo_url=repo_url,
            installed_by=installed_by,
        )
    except (RuntimeError, ValueError) as exc:
        raise ValueError(f"deploy failed: {exc}") from exc

    return InstallResult(app_name=effective_name, status="building")
