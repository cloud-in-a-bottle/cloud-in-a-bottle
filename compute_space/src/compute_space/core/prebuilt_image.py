import json
import re
import subprocess
import time
from collections.abc import Callable

import attr

from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger

# Images from our registry must carry a cosign keyless signature made by app-template's shared image workflow, running
# on main for the app's own repo, at the commit being deployed. Images from any other registry are pulled unverified.
CIAB_REGISTRY = "registry.cloudinabottle.org"
SIGNING_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGNING_IDENTITY = "https://github.com/cloud-in-a-bottle/app-template/.github/workflows/app-image.yml@refs/heads/main"

_GITHUB_REPO_RE = re.compile(r"^(?:https?://)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# cosign's own exit codes for a check that ran and failed (cmd/cosign/errors/exit_codes.go): no signature, no matching
# signature, no certificate on the signature. Other failures, including most verification failures in cosign 3's
# bundle format, exit 1, so those are told apart by whether the registry was reachable (see _verify_failure).
_COSIGN_VERIFICATION_EXIT_CODES = frozenset({10, 12, 13})
# Go's errors for a request that never got an answer (DNS, connect, proxy, TLS handshake and timeouts), and the
# registry answering that it can't serve right now. Any of these in cosign's error means it couldn't reach the
# registry or Sigstore's trust root, not that a signature was wrong.
_UNREACHABLE_ERRORS = (
    "dial tcp",
    "proxyconnect",
    "no such host",
    "connection refused",
    "connection reset by peer",
    "network is unreachable",
    "i/o timeout",
    "TLS handshake timeout",
    "context deadline exceeded",
    "Client.Timeout exceeded",
    "unexpected EOF",
    "unexpected status code 5",
    "TOOMANYREQUESTS",
)

# A pull that can't fetch the image is tried PULL_ATTEMPTS times, waiting these delays in between, before the caller
# falls back to building the release's Dockerfile. podman's own retries are turned off (``--retry=0``) so this is the
# only retry policy.
PULL_RETRY_DELAYS_SECONDS = (5, 15)
PULL_ATTEMPTS = len(PULL_RETRY_DELAYS_SECONDS) + 1
# containers/image's error when the instance's signature policy (policy.json) refuses an image.
_POLICY_REJECTED = "Source image rejected"


class PrebuiltImageError(RuntimeError):
    """The prebuilt image can't be used, and building instead wouldn't help or isn't allowed: the install or update
    fails. Raised as is for local failures (podman missing, retagging failed, a malformed commit)."""


class ImageUnavailableError(PrebuiltImageError):
    """The image couldn't be fetched: the registry is unreachable or erroring, the request timed out, or the image or
    its tag isn't there (yet). Retried, then the caller builds the release's Dockerfile instead."""


class ImageVerificationError(PrebuiltImageError):
    """The image isn't trusted: its signature is missing or doesn't match, or nothing could verify it. Never retried
    and never falls back to a build: an image that fails verification is a sign the registry is serving something it
    shouldn't."""


@attr.s(auto_attribs=True, frozen=True)
class ImageToPull:
    ref: str
    # Whether ``ref`` was checked against a signature before pulling (see _image_to_pull).
    verified: bool


@attr.s(auto_attribs=True, frozen=True)
class PulledImage:
    tag: str
    # Whether its signature was verified (see _image_to_pull): from our registry it always is, from others never.
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
    identity: str
    # owner/name of the GitHub repo whose workflow run signed it: the app's own repo.
    workflow_repository: str
    # The commit the image was built from: the one being deployed.
    workflow_sha: str


def image_registry(image: str) -> str:
    """The registry host of an image reference, following the docker convention: the first path component is a
    registry only if it looks like a host (has a dot or a port, or is localhost)."""
    first, sep, _ = image.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def github_repository(repo_url: str) -> str | None:
    """owner/name of a GitHub repo URL (any ``@ref`` suffix dropped), or None if it isn't one."""
    try:
        base_url, _ = parse_repo_url(repo_url)
    except ValueError:
        return None
    match = _GITHUB_REPO_RE.match(base_url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def is_verified_registry(image: str) -> bool:
    """Whether images from ``image``'s registry are signature-verified before they run (see signature_policy). Reads
    only the reference, so it needs no network."""
    return image_registry(image) == CIAB_REGISTRY


def signature_policy(image: str, repo_url: str | None, commit: str) -> SignaturePolicy | None:
    """The signature an image must carry before it runs, or None for registries we don't sign for. Raises
    :class:`ImageVerificationError` if the image is from our registry but the app's repo isn't a GitHub repo, since
    then nothing could verify it."""
    if not is_verified_registry(image):
        return None
    repository = github_repository(repo_url) if repo_url else None
    if repository is None:
        raise ImageVerificationError(
            f"{image} is from {CIAB_REGISTRY}, whose images are verified against the app's GitHub repo, "
            f"but this app's repo ({repo_url or 'none'}) isn't a GitHub repo"
        )
    return SignaturePolicy(
        oidc_issuer=SIGNING_OIDC_ISSUER,
        identity=SIGNING_IDENTITY,
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
        "--certificate-identity",
        policy.identity,
        "--certificate-github-workflow-repository",
        policy.workflow_repository,
        "--certificate-github-workflow-sha",
        policy.workflow_sha,
        "--output",
        "json",
    ]


def cosign_triangulate_command(tagged: str) -> list[str]:
    # Prints ``<repo>@<digest>``, asking the registry with the same client and credentials ``cosign verify`` uses.
    # triangulate is deprecated for removal in cosign 4; pyproject pins cosign ~=3.1, and
    # test_cosign_still_has_triangulate catches an upgrade that drops it.
    return ["cosign", "triangulate", "--type", "digest", tagged]


def verified_digest(cosign_stdout: str) -> str:
    """The image digest cosign verified, from ``cosign verify --output json``. Every verified signature must name the
    same digest."""
    try:
        payloads = json.loads(cosign_stdout)
        digests = {str(p["critical"]["image"]["docker-manifest-digest"]) for p in payloads}
    except (ValueError, TypeError, KeyError) as e:
        raise ImageVerificationError(f"Could not read cosign's output: {e}") from e
    if len(digests) != 1:
        raise ImageVerificationError(
            f"cosign verified {len(digests)} digests, expected exactly one: {sorted(digests)}"
        )
    digest = digests.pop()
    if not _DIGEST_RE.match(digest):
        raise ImageVerificationError(f"cosign reported an invalid digest: {digest!r}")
    return digest


def resolve_digest(tagged: str, log: Callable[[str], None]) -> str:
    """The digest the registry serves for ``tagged``. Raises :class:`ImageUnavailableError` when the registry can't
    say: it's unreachable or erroring, the tag isn't there (yet), or the answer is unreadable."""
    result = _run(cosign_triangulate_command(tagged), log, timeout=120, timeout_error=ImageUnavailableError)
    if result.returncode != 0:
        # Leave out the deprecation notice triangulate prints first.
        error = "\n".join(line for line in result.stderr.strip().splitlines() if "is deprecated" not in line)
        raise ImageUnavailableError(f"Looking up {tagged} on its registry failed: {error or 'cosign exited non-zero'}")
    _, _, digest = result.stdout.strip().rpartition("@")
    if not _DIGEST_RE.match(digest):
        raise ImageUnavailableError(f"cosign resolved {tagged} to an unexpected reference: {result.stdout.strip()!r}")
    return digest


def _verify_failure(
    tagged: str, policy: SignaturePolicy, result: subprocess.CompletedProcess[str], log: Callable[[str], None]
) -> PrebuiltImageError:
    """Classify a failed ``cosign verify``. It needs the registry (for the signature) and Sigstore's trust root, so it
    also fails when it can't reach them, which only makes the image unavailable. It's a verification failure when
    cosign says so by exit code, or when its error isn't a network one and the registry is still serving the image.
    Anything in doubt counts as unavailable, which is the safe side: that builds the release from source, and never
    runs an image that wasn't verified."""
    stderr = result.stderr.strip() or "cosign exited non-zero"
    unavailable = ImageUnavailableError(f"Couldn't check the signature on {tagged}: {stderr}")
    if result.returncode not in _COSIGN_VERIFICATION_EXIT_CODES:
        if any(marker in stderr for marker in _UNREACHABLE_ERRORS):
            return unavailable
        try:
            resolve_digest(tagged, log)
        except ImageUnavailableError:
            return unavailable
    return ImageVerificationError(
        f"Signature check failed for {tagged} (it must be signed by the image workflow for "
        f"{policy.workflow_repository} at {policy.workflow_sha}): {stderr}"
    )


def _image_to_pull(prebuilt_image: str, commit: str, repo_url: str | None, log: Callable[[str], None]) -> ImageToPull:
    """The reference to pull for ``commit``. From our registry: resolve ``<prebuilt_image>:<sha>`` to a digest, verify
    the signature on that digest, and return ``<prebuilt_image>@<digest>``, so what runs is exactly what was verified
    even if the tag moves. From any other registry: the tag itself, unverified.

    Raises :class:`ImageVerificationError` when the check fails, and :class:`ImageUnavailableError` only when it
    couldn't reach what it needed to check."""
    tagged = f"{prebuilt_image}:{commit}"
    policy = signature_policy(prebuilt_image, repo_url, commit)
    if policy is None:
        return ImageToPull(ref=tagged, verified=False)
    pinned = f"{prebuilt_image}@{resolve_digest(tagged, log)}"
    result = _run(cosign_verify_command(pinned, policy), log, timeout=300, timeout_error=ImageUnavailableError)
    if result.returncode != 0:
        raise _verify_failure(tagged, policy, result, log)
    digest = verified_digest(result.stdout)
    if f"{prebuilt_image}@{digest}" != pinned:
        raise ImageVerificationError(f"cosign verified {digest}, not the {pinned} it was asked to")
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
) -> PulledImage:
    """Pull the image built from ``commit`` (verifying its signature when it's from our registry) and tag it as the
    app's image (what ``build_image`` would otherwise produce). An :class:`ImageUnavailableError` is retried up to
    :data:`PULL_ATTEMPTS` times in all and then raised, for the caller to fall back to a build; any other
    :class:`PrebuiltImageError` is raised at once."""
    if not _SHA_RE.match(commit):
        raise PrebuiltImageError(f"Expected a full commit sha to pick the prebuilt image, got {commit!r}")
    for attempt, delay in enumerate(PULL_RETRY_DELAYS_SECONDS, start=1):
        try:
            return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
        except ImageUnavailableError as e:
            log(f"Prebuilt image attempt {attempt}/{PULL_ATTEMPTS} failed ({e}); retrying in {delay}s")
            time.sleep(delay)
    return _pull_once(app_name, prebuilt_image, commit, repo_url, log)
