import asyncio
import subprocess

from compute_space.config import Config
from compute_space.core.apps import PreparedImage
from compute_space.core.apps import app_log_writer
from compute_space.core.apps import prepare_app_image
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest
from compute_space.core.releases import ReleaseError
from compute_space.core.releases import checkout_newest_release
from compute_space.core.releases import follows_releases

# The steps an update (from the app's page, or the scheduled one) takes between pulling the app's upstream and stopping
# the running version. Either step failing means the update can't be applied: the caller rolls the checkout back to the
# running version and leaves the app running.


class UpdateNotApplied(RuntimeError):
    pass


def _parsed_manifest(repo_path: str) -> AppManifest | None:
    try:
        return parse_manifest(repo_path)
    except ValueError:
        return None  # the reload itself reports an unparseable manifest


async def check_out_update_release(repo_path: str, repo_url: str | None) -> str | None:
    """After a pull: an app that follows releases (see releases.follows_releases) deploys its newest release, not the
    head just pulled, so check that release out and return it. Returns None for an app that deploys the pulled head.
    Raises :class:`UpdateNotApplied` if there's no release to check out."""
    manifest = _parsed_manifest(repo_path)
    if manifest is None or not follows_releases(repo_url, manifest):
        return None
    assert manifest.release_tag_format is not None
    try:
        return await asyncio.to_thread(checkout_newest_release, repo_path, manifest.release_tag_format, True)
    except ReleaseError as e:
        raise UpdateNotApplied(str(e)) from e


async def prepare_update_image(
    app_name: str, repo_path: str, repo_url: str | None, config: Config
) -> PreparedImage | None:
    """Produce the image an app that follows releases will run, before its running version is stopped: the release's
    prebuilt image if it sets one, otherwise (or when that can't be fetched) a local build of the release (see
    apps.prepare_app_image). The app's image tag only moves once the pull or build succeeds, so a failure leaves the
    running version untouched. Returns None for an app that doesn't follow releases, which the reload builds after the
    stop as it always has. Raises :class:`UpdateNotApplied`."""
    manifest = _parsed_manifest(repo_path)
    if manifest is None or not follows_releases(repo_url, manifest):
        return None
    try:
        return await asyncio.to_thread(
            prepare_app_image,
            app_name,
            repo_path,
            manifest,
            repo_url,
            config.temporary_data_dir,
            app_log_writer(app_name, config),
        )
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise UpdateNotApplied(str(e)) from e
