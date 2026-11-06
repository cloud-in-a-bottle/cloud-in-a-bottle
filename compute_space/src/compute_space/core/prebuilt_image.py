import re
import subprocess
from collections.abc import Callable

from compute_space.core.logging import logger

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class PrebuiltImageError(RuntimeError):
    pass


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
    """The reference to pull for ``commit``: its ``<prebuilt_image>:<sha>`` tag."""
    return f"{prebuilt_image}:{commit}"


def pull_prebuilt_image(
    app_name: str,
    prebuilt_image: str,
    commit: str,
    repo_url: str | None,
    log: Callable[[str], None],
) -> str:
    """Pull the image built from ``commit`` and tag it as the app's image (what ``build_image`` would otherwise
    produce). Returns that tag. Never falls back to building: any failure raises :class:`PrebuiltImageError`."""
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
