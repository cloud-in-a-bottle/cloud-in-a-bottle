"""Apps whose stored manifest sets prebuilt_image without release_tag_format: app releases did that before the field
existed, and instances keep those manifests (the checkout, and apps.manifest_raw from the last deploy) until the app
next updates. Restarts, reboots and plain reloads must keep running them."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from compute_space.core import apps as core_apps
from compute_space.core.apps import ImageSource
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import restart_app_process
from compute_space.core.manifest import parse_manifest_from_string

_LEGACY = """[app]
name = "legacy"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
prebuilt_image = "registry.cloudinabottle.org/legacy"
port = 8080
"""


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE apps (app_id TEXT, name TEXT, repo_path TEXT, repo_url TEXT, manifest_raw TEXT)")
    db.execute(
        "INSERT INTO apps VALUES ('a1', 'legacy', ?, 'https://github.com/cloud-in-a-bottle/legacy', ?)",
        (str(tmp_path), _LEGACY),
    )
    return db


def test_restart_recovery_runs_an_app_whose_stored_manifest_predates_release_tag_format(tmp_path: Path) -> None:
    """The boot-time recovery path (startup._restart_apps_sequential -> restart_app_process) parses manifest_raw."""
    with (
        mock.patch.object(core_apps.storage, "check_before_deploy"),
        mock.patch.object(core_apps, "run_app_image") as run,
    ):
        restart_app_process("a1", _db(tmp_path), mock.Mock())
    run.assert_called_once()
    assert run.call_args.args[1] == "openhost-legacy:latest"
    assert run.call_args.args[2].prebuilt_image is None


def test_a_rebuild_of_a_legacy_manifest_builds_instead_of_pulling(tmp_path: Path) -> None:
    """A plain reload or a rebuild on boot (start_app_process) prepares the image: with the prebuilt image ignored,
    that's a local build of the checkout, as before prebuilt images existed."""
    manifest = parse_manifest_from_string(_LEGACY)
    with (
        mock.patch.object(core_apps, "_build_with_retries", return_value="openhost-legacy:latest") as build,
        mock.patch.object(core_apps, "pull_prebuilt_image") as pull,
    ):
        prepared = core_apps.prepare_app_image(
            "legacy", str(tmp_path), manifest, "https://github.com/cloud-in-a-bottle/legacy", str(tmp_path), print
        )
    pull.assert_not_called()
    build.assert_called_once()
    assert prepared.source is ImageSource.BUILT


@pytest.mark.parametrize("clone_failed, asks_for_token", [(True, True), (False, False)])
def test_only_a_failed_clone_asks_for_a_github_token(clone_failed: bool, asks_for_token: bool) -> None:
    """A bad manifest (or no release) cloned fine: a token can't fix it, and asking for one hid the real error behind
    "GitHub authorization required"."""

    async def fake(_url: str, _token: Any) -> tuple[None, None, str, bool]:
        return None, None, "the error", clone_failed

    with (
        mock.patch.object(core_apps, "_clone_and_read_manifest", side_effect=fake),
        mock.patch.object(core_apps, "get_oauth_token", side_effect=RuntimeError("no oauth here")) as token,
    ):
        manifest, clone_dir, error, authorize_url = asyncio.run(
            clone_with_github_fallback("https://github.com/cloud-in-a-bottle/legacy", return_to="/")
        )
    assert manifest is None and clone_dir is None and authorize_url is None
    assert token.called is asks_for_token
    assert error is not None and error.startswith("the error")
