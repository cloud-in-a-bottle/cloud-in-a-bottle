import re
import subprocess
import time
from collections.abc import Callable
from collections.abc import Sequence

import attr

from compute_space.core.logging import logger

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# How long an install or update waits before each retry of a pull that can't fetch the image (so it tries 3 times in
# all) before falling back to building the release's Dockerfile.
PULL_RETRY_DELAYS_SECONDS = (5.0, 15.0)
# containers/image's error when the instance's signature policy (policy.json) refuses an image.
_POLICY_REJECTED = "Source image rejected"


class PrebuiltImageError(RuntimeError):
    """The prebuilt image can't be used, and building instead wouldn't help or isn't allowed: the install or update
    fails. Raised as is for local failures (podman missing, retagging failed, a malformed commit)."""


class ImageUnavailableError(PrebuiltImageError):
    """The image couldn't be fetched: the registry is unreachable or erroring, the request timed out, or the image or
    its tag isn't there (yet). Retried, then the caller builds the release's Dockerfile instead."""


class ImageVerificationError(PrebuiltImageError):
    """The image was fetched but isn't trusted (e.g. a missing or wrong signature). Never retried and never falls back
    to a build: an image that fails verification is a sign the registry is serving something it shouldn't."""


@attr.s(auto_attribs=True, frozen=True)
class ImageToPull:
    ref: str
    # Whether ``ref`` was checked against a signature before pulling (see _image_to_pull).
    verified: bool


@attr.s(auto_attribs=True, frozen=True)
class PulledImage:
    tag: str
    verified: bool


def _run(
    cmd: list[str],
    log: Callable[[str], None],
    timeout: int,
    timeout_error: type[PrebuiltImageError] = PrebuiltImageError,
) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise PrebuiltImageError(f"{cmd[0]} is not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise timeout_error(f"{' '.join(cmd[:2])} timed out after {timeout}s") from e
    if result.stderr.strip():
        log(result.stderr.strip())
    return result


def _image_to_pull(prebuilt_image: str, commit: str, repo_url: str | None, log: Callable[[str], None]) -> ImageToPull:
    """The reference to pull for ``commit``: its ``<prebuilt_image>:<sha>`` tag, unverified.

    A hook that checks the image before it's pulled must raise :class:`ImageVerificationError` when the check fails,
    and :class:`ImageUnavailableError` only when it couldn't reach what it needed to check."""
    return ImageToPull(ref=f"{prebuilt_image}:{commit}", verified=False)


def _pull_failure(pull_ref: str, stderr: str) -> PrebuiltImageError:
    """Classify a failed ``podman pull``. Only the instance's own signature policy refusing the image is a
    verification failure; anything else (unreachable, timeouts, auth, a missing tag, a full disk) counts as
    unavailable, since building the same commit from its Dockerfile is no less trusted than a pinned app's build."""
    message = f"Pulling {pull_ref} failed: {stderr or 'podman exited non-zero'}"
    if _POLICY_REJECTED in stderr:
        return ImageVerificationError(message)
    return ImageUnavailableError(message)


def _pull_once(
    app_name: str, prebuilt_image: str, commit: str, repo_url: str | None, log: Callable[[str], None]
) -> PulledImage:
    to_pull = _image_to_pull(prebuilt_image, commit, repo_url, log)
    result = _run(["podman", "pull", "--retry=0", to_pull.ref], log, timeout=1800, timeout_error=ImageUnavailableError)
    if result.returncode != 0:
        raise _pull_failure(to_pull.ref, result.stderr.strip())

    image_tag = f"openhost-{app_name}:latest"
    result = _run(["podman", "tag", to_pull.ref, image_tag], log, timeout=60)
    if result.returncode != 0:
        raise PrebuiltImageError(f"Tagging {to_pull.ref} as {image_tag} failed: {result.stderr.strip()}")
    logger.info("{}: using prebuilt image {} as {}", app_name, to_pull.ref, image_tag)
    return PulledImage(tag=image_tag, verified=to_pull.verified)


def pull_prebuilt_image(
    app_name: str,
    prebuilt_image: str,
    commit: str,
    repo_url: str | None,
    log: Callable[[str], None],
    retry_delays: Sequence[float],
) -> PulledImage:
    """Pull the image built from ``commit`` and tag it as the app's image (what ``build_image`` would otherwise
    produce). An :class:`ImageUnavailableError` is retried once after each of ``retry_delays`` (in seconds) and then
    raised, for the caller to fall back to a build; any other :class:`PrebuiltImageError` is raised at once. podman's
    own retries are turned off (``--retry=0``), so these are the only ones."""
    if not _SHA_RE.match(commit):
        raise PrebuiltImageError(f"Expected a full commit sha to pick the prebuilt image, got {commit!r}")
    attempts = len(retry_delays) + 1
    for attempt, delay in enumerate(retry_delays, start=1):
        try:
            return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
        except ImageUnavailableError as e:
            log(f"Prebuilt image attempt {attempt}/{attempts} failed ({e}); retrying in {delay:g}s")
            time.sleep(delay)
    return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
