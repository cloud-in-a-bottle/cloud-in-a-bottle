import json
import re
import subprocess
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


class PrebuiltImageError(RuntimeError):
    pass


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


def signature_policy(image: str, repo_url: str | None, commit: str) -> SignaturePolicy | None:
    """The signature an image must carry before it runs, or None for registries we don't sign for. Raises if the image
    is from our registry but the app's repo isn't a GitHub repo, since then nothing could verify it."""
    if image_registry(image) != CIAB_REGISTRY:
        return None
    repository = github_repository(repo_url) if repo_url else None
    if repository is None:
        raise PrebuiltImageError(
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


def verified_digest(cosign_stdout: str) -> str:
    """The image digest cosign verified, from ``cosign verify --output json``. Every verified signature must name the
    same digest."""
    try:
        payloads = json.loads(cosign_stdout)
        digests = {str(p["critical"]["image"]["docker-manifest-digest"]) for p in payloads}
    except (ValueError, TypeError, KeyError) as e:
        raise PrebuiltImageError(f"Could not read cosign's output: {e}") from e
    if len(digests) != 1:
        raise PrebuiltImageError(f"cosign verified {len(digests)} digests, expected exactly one: {sorted(digests)}")
    digest = digests.pop()
    if not _DIGEST_RE.match(digest):
        raise PrebuiltImageError(f"cosign reported an invalid digest: {digest!r}")
    return digest


def _run(cmd: list[str], log: Callable[[str], None], timeout: int) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise PrebuiltImageError(f"{cmd[0]} is not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise PrebuiltImageError(f"{' '.join(cmd[:2])} timed out after {timeout}s") from e
    if result.stderr.strip():
        log(result.stderr.strip())
    return result


def _image_to_pull(prebuilt_image: str, commit: str, repo_url: str | None, log: Callable[[str], None]) -> str:
    """The reference to pull for ``commit``. From our registry: verify the signature on ``<prebuilt_image>:<sha>``,
    which resolves it to a digest, and return ``<prebuilt_image>@<digest>``, so what runs is exactly what was verified
    even if the tag moves. From any other registry: the tag itself, unverified."""
    tagged = f"{prebuilt_image}:{commit}"
    policy = signature_policy(prebuilt_image, repo_url, commit)
    if policy is None:
        return tagged
    result = _run(cosign_verify_command(tagged, policy), log, timeout=300)
    if result.returncode != 0:
        raise PrebuiltImageError(
            f"Signature check failed for {tagged} (it must be signed by the image workflow for "
            f"{policy.workflow_repository} at {commit}): {result.stderr.strip() or 'cosign exited non-zero'}"
        )
    pull_ref = f"{prebuilt_image}@{verified_digest(result.stdout)}"
    log(f"Verified {tagged} as {pull_ref}")
    return pull_ref


def pull_prebuilt_image(
    app_name: str,
    prebuilt_image: str,
    commit: str,
    repo_url: str | None,
    log: Callable[[str], None],
) -> str:
    """Pull the image built from ``commit`` (verifying its signature when it's from our registry) and tag it as the
    app's image (what ``build_image`` would otherwise produce). Returns that tag. Never falls back to building: any
    failure raises :class:`PrebuiltImageError`."""
    if not _SHA_RE.match(commit):
        raise PrebuiltImageError(f"Expected a full commit sha to pick the prebuilt image, got {commit!r}")
    pull_ref = _image_to_pull(prebuilt_image, commit, repo_url, log)

    result = _run(["podman", "pull", pull_ref], log, timeout=1800)
    if result.returncode != 0:
        raise PrebuiltImageError(f"Pulling {pull_ref} failed: {result.stderr.strip() or 'podman exited non-zero'}")

    image_tag = f"openhost-{app_name}:latest"
    result = _run(["podman", "tag", pull_ref, image_tag], log, timeout=60)
    if result.returncode != 0:
        raise PrebuiltImageError(f"Tagging {pull_ref} as {image_tag} failed: {result.stderr.strip()}")
    logger.info("{}: using prebuilt image {} as {}", app_name, pull_ref, image_tag)
    return image_tag
