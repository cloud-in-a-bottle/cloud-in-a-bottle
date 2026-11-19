import json
import re
import subprocess
import time
from collections.abc import Callable
from collections.abc import Sequence

import attr

from compute_space.core.containers import app_image_tag
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger

# Our apps require a GitHub Actions signature for their repo and commit, wherever the image is hosted.
SIGNING_ORG = "cloud-in-a-bottle"
SIGNING_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGNING_IDENTITY_REGEXP = r"^https://github\.com/[^/]+/[^/]+/\.github/workflows/[^@]+@.+$"

_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.IGNORECASE
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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
    """The image couldn't be verified. Never retried: the caller builds from the release's source instead of running
    this image. Includes missing/invalid signatures and failures to reach the registry or trust service while verifying."""


@attr.s(auto_attribs=True, frozen=True)
class ImageToPull:
    ref: str
    # Whether ``ref`` was checked against a signature before pulling (see _image_to_pull).
    verified: bool


@attr.s(auto_attribs=True, frozen=True)
class PulledImage:
    tag: str
    # True when the source repo is in our GitHub org and the required signature was verified.
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


@attr.s(auto_attribs=True, frozen=True)
class SignaturePolicy:
    """What a prebuilt image's signing certificate must say."""

    oidc_issuer: str
    identity_regexp: str
    # owner/name of the GitHub repo whose workflow run signed it: the app's own repo.
    workflow_repository: str
    # The commit the image was built from: the one being deployed.
    workflow_sha: str


def github_repository(repo_url: str) -> str | None:
    """owner/name of a GitHub repo URL (any ``@ref`` suffix dropped), or None if it isn't one."""
    try:
        base_url, _ = parse_repo_url(repo_url)
    except ValueError:
        return None
    match = _GITHUB_REPO_RE.match(base_url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def signature_policy(repo_url: str | None, commit: str) -> SignaturePolicy | None:
    """Require a GitHub Actions signature for apps from our org, regardless of registry. Third-party apps (including
    forks outside our org) choose their own images and are pulled without this policy. A workflow or registry move
    doesn't weaken verification: the certificate must still name the app's exact repo and deployed commit."""
    repository = github_repository(repo_url) if repo_url else None
    if repository is None or repository.split("/", 1)[0].lower() != SIGNING_ORG:
        return None
    return SignaturePolicy(
        oidc_issuer=SIGNING_OIDC_ISSUER,
        identity_regexp=SIGNING_IDENTITY_REGEXP,
        workflow_repository=repository,
        workflow_sha=commit,
    )


def cosign_verify_command(image_ref: str, policy: SignaturePolicy) -> list[str]:
    return [
        "cosign",
        "verify",
        image_ref,
        "--certificate-oidc-issuer",
        policy.oidc_issuer,
        "--certificate-identity-regexp",
        policy.identity_regexp,
        "--certificate-github-workflow-repository",
        policy.workflow_repository,
        "--certificate-github-workflow-sha",
        policy.workflow_sha,
        "--output",
        "json",
    ]


def verified_digest(cosign_stdout: str) -> str:
    """The image digest cosign verified, from ``cosign verify --output json``. Every verified signature must name the
    same digest."""
    try:
        payloads = json.loads(cosign_stdout)
        if not isinstance(payloads, list):
            raise ValueError("expected a list of verified payloads")
        digests = {p["critical"]["image"]["docker-manifest-digest"] for p in payloads}
    except (ValueError, TypeError, KeyError) as e:
        raise ImageVerificationError(f"Could not read cosign's output: {e}") from e
    if len(digests) != 1:
        raise ImageVerificationError(f"cosign verified {len(digests)} digests, expected exactly one")
    digest: object = digests.pop()
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ImageVerificationError("cosign reported an invalid digest")
    return digest


def _image_to_pull(prebuilt_image: str, commit: str, repo_url: str | None, log: Callable[[str], None]) -> ImageToPull:
    """For our apps, cosign resolves and verifies the tagged image. Only its verified payload supplies the digest
    we pull, so a tag moving afterwards cannot replace the image. Other apps pull the tag without verification."""
    tagged = f"{prebuilt_image}:{commit}"
    policy = signature_policy(repo_url, commit)
    if policy is None:
        return ImageToPull(ref=tagged, verified=False)
    result = _run(cosign_verify_command(tagged, policy), log, timeout=300, timeout_error=ImageVerificationError)
    if result.returncode != 0:
        raise ImageVerificationError(
            f"Signature check failed for {tagged} (GitHub repo {policy.workflow_repository}, "
            f"commit {policy.workflow_sha}): {result.stderr.strip() or 'cosign exited non-zero'}"
        )
    digest = verified_digest(result.stdout)
    pinned = f"{prebuilt_image}@{digest}"
    log(f"Verified {tagged} as {pinned}")
    return ImageToPull(ref=pinned, verified=True)


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

    image_tag = app_image_tag(app_name)
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
    """Pull the image built from ``commit`` (verifying its signature for our app repos) and tag it as the
    app's image (what ``build_image`` would otherwise produce). An :class:`ImageUnavailableError` is retried once after
    each of ``retry_delays`` (in seconds) and then raised, for the caller to fall back to a build; any other
    :class:`PrebuiltImageError` is raised at once. podman's own retries are turned off (``--retry=0``), so these are
    the only ones."""
    if not _SHA_RE.fullmatch(commit):
        raise PrebuiltImageError(f"Expected a full commit sha to pick the prebuilt image, got {commit!r}")
    attempts = len(retry_delays) + 1
    for attempt, delay in enumerate(retry_delays, start=1):
        try:
            return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
        except ImageUnavailableError as e:
            log(f"Prebuilt image attempt {attempt}/{attempts} failed ({e}); retrying in {delay:g}s")
            time.sleep(delay)
    return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
