"""Auto-deploy ``config.default_apps`` apps at /setup completion.

Each entry in ``config.default_apps`` is either:

- A bare dirname under ``config.apps_dir`` (vendored builtin, e.g.
  ``"file_browser"``) — copied from disk, same as the original
  default-apps behavior.
- A remote git URL (e.g.
  ``"https://github.com/imbue-openhost/openhost-catalog"``) — cloned
  on demand via the same path used by ``/api/add_app``.  The repo
  does not need to be present on disk ahead of time.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from typing import Any

import attr

from compute_space.config import Config
from compute_space.core.apps import all_manifest_permissions_v2
from compute_space.core.apps import clone_and_read_manifest
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import validate_manifest
from compute_space.core.email.enablement import email_enabled
from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest

MAX_RETRY_ATTEMPTS = 3

# Hard upper bound on a single remote clone attempt before
# ``_run_clone_in_thread`` gives up and returns a failure.  ``clone_and_read_manifest``
# already imposes a 120s timeout on the ``git clone`` subprocess; this is a
# defence-in-depth ceiling on the whole worker (DNS, manifest parse,
# token-strip subcommand, etc.) so a stuck clone cannot freeze the ``/setup``
# request indefinitely.  Failures here are retried by ``deploy_default_apps``
# up to ``MAX_RETRY_ATTEMPTS``.
REMOTE_CLONE_TIMEOUT_SECONDS = 180


@attr.s(auto_attribs=True, frozen=True)
class DefaultAppOutcome:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    attempts: int
    error: str | None = None


def _is_remote_url(spec: str) -> bool:
    """A default_apps entry is a remote URL (vs a bare dirname) if it
    contains a scheme separator.  ``file://`` is treated as remote-style
    (handled by the clone path) so operators can use it interchangeably.
    """
    return "://" in spec


def _load_sentinel(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("default_apps sentinel at %s is unreadable; ignoring", path)
        return {}


def _write_sentinel(path: str, state: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write default_apps sentinel %s: %s", path, exc)


def _install_vendored(dir_name: str, config: Config, db: sqlite3.Connection) -> tuple[str, str | None]:
    """Install a vendored builtin app by its dirname under ``config.apps_dir``."""
    app_dir = os.path.join(config.apps_dir, dir_name)
    if not os.path.isdir(app_dir) or not os.path.isfile(os.path.join(app_dir, "openhost.toml")):
        return "failed", f"builtin app dir or openhost.toml missing: {app_dir}"

    tmp_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    clone_dir = os.path.join(tmp_parent, "repo")
    try:
        shutil.copytree(app_dir, clone_dir)
        manifest = parse_manifest(clone_dir)
    except Exception as exc:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "failed", f"clone/parse: {exc}"

    return _finalize_install(manifest, clone_dir, tmp_parent, config, db, repo_url=f"file://{app_dir}")


def _run_clone_in_thread(
    repo_url: str, timeout_seconds: float | None = None
) -> tuple[AppManifest | None, str | None, str | None]:
    """Run ``clone_and_read_manifest`` from a sync context.

    Cannot use ``asyncio.run`` directly because this module is called
    from an already-running event loop (the ``/setup`` Quart handler).
    A fresh thread with its own event loop sidesteps that.

    Bounded by ``timeout_seconds`` (defaults to
    ``REMOTE_CLONE_TIMEOUT_SECONDS``, resolved at call time so tests can
    monkeypatch the module constant) so a hung clone (DNS stall,
    unresponsive git remote past its own subprocess timeout, etc.) cannot
    block the caller indefinitely.  On timeout the worker thread is left
    running as a daemon — it will exit when the underlying ``git clone``
    subprocess eventually returns — and the caller sees a ``RuntimeError``.
    """
    if timeout_seconds is None:
        timeout_seconds = REMOTE_CLONE_TIMEOUT_SECONDS
    result: dict[str, Any] = {}

    def _worker() -> None:
        try:
            loop = asyncio.new_event_loop()
        except Exception as exc:  # noqa: BLE001 — surfaced via result["error"]
            result["error"] = exc
            return
        try:
            result["value"] = loop.run_until_complete(clone_and_read_manifest(repo_url))
        except Exception as exc:  # noqa: BLE001 — surfaced via result["error"]
            result["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    if t.is_alive():
        raise RuntimeError(f"clone exceeded {timeout_seconds}s timeout")
    if "error" in result:
        raise result["error"]
    return result["value"]  # type: ignore[no-any-return]


def _install_remote(repo_url: str, config: Config, db: sqlite3.Connection) -> tuple[str, str | None]:
    """Install an app from a remote git URL by cloning it on demand.

    Uses the unauthenticated clone path on purpose: this code runs at
    /setup completion (and on every boot to retry pending installs), so
    the Secrets app may not be installed yet and there is no browser
    session to start a GitHub OAuth flow against.  Default apps are
    expected to be public repos.  Operators who need private-repo
    builtins should install them via the dashboard's /add_app flow
    after first boot.
    """
    try:
        manifest, clone_dir, error = _run_clone_in_thread(repo_url)
    except Exception as exc:
        return "failed", f"clone: {exc}"

    if error or manifest is None or clone_dir is None:
        return "failed", f"clone: {error or 'unknown error'}"

    tmp_parent = os.path.dirname(clone_dir)
    return _finalize_install(manifest, clone_dir, tmp_parent, config, db, repo_url=repo_url)


def _finalize_install(
    manifest: AppManifest,
    clone_dir: str,
    tmp_parent: str,
    config: Config,
    db: sqlite3.Connection,
    *,
    repo_url: str,
) -> tuple[str, str | None]:
    """Common tail after either a copytree or a remote clone has produced
    a parsed manifest in ``clone_dir``."""
    if db.execute("SELECT 1 FROM apps WHERE name = ?", (manifest.name,)).fetchone():
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "skipped", None

    validation_error = validate_manifest(manifest, db)
    if validation_error is not None:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        return "failed", validation_error

    final_dir = move_clone_to_app_temp_dir(clone_dir, manifest.name, config)
    try:
        insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            permissions_v2_grants=all_manifest_permissions_v2(manifest),
            repo_url=repo_url,
        )
    except Exception as exc:
        return "failed", f"insert_and_deploy: {exc}"
    return "ok", None


def _install_one(spec: str, config: Config, db: sqlite3.Connection) -> tuple[str, str | None]:
    """Returns (status, error).  status in {"ok", "skipped", "failed"}."""
    if _is_remote_url(spec):
        return _install_remote(spec, config, db)
    return _install_vendored(spec, config, db)


def deploy_default_apps(config: Config, db: sqlite3.Connection) -> list[DefaultAppOutcome]:
    """Idempotent across boots.  ok/skipped are terminal; failed retries
    up to MAX_RETRY_ATTEMPTS.  Never raises."""
    # The email apps (mailbox + webmail) are appended only when email is enabled,
    # which needs the DB (the shared Imbue identity lives in the settings table).
    specs = config.effective_default_apps(email_on=email_enabled(config, db))
    if not specs:
        return []

    sentinel = _load_sentinel(config.default_apps_sentinel_path)
    outcomes: list[DefaultAppOutcome] = []

    for spec in specs:
        prior = sentinel.get(spec) or {}
        prior_status = prior.get("status")
        try:
            prior_attempts = int(prior.get("attempts", 0))
        except (TypeError, ValueError):
            prior_attempts = 0

        if prior_status in ("ok", "skipped"):
            outcomes.append(DefaultAppOutcome(spec, prior_status, prior_attempts))
            continue
        if prior_status == "failed" and prior_attempts >= MAX_RETRY_ATTEMPTS:
            outcomes.append(DefaultAppOutcome(spec, "failed", prior_attempts, prior.get("error")))
            continue

        try:
            status, error = _install_one(spec, config, db)
        except Exception as exc:
            status, error = "failed", f"unexpected: {exc}"

        attempts = prior_attempts + 1 if status == "failed" else 1
        outcomes.append(DefaultAppOutcome(spec, status, attempts, error))
        sentinel[spec] = {"status": status, "attempts": attempts, "error": error}

    _write_sentinel(config.default_apps_sentinel_path, sentinel)

    for o in outcomes:
        if o.status == "ok":
            logger.info("default_apps: %s deployed", o.name)
        elif o.status == "skipped":
            logger.info("default_apps: %s skipped (already installed)", o.name)
        else:
            logger.warning("default_apps: %s failed (attempt %d): %s", o.name, o.attempts, o.error)

    return outcomes
