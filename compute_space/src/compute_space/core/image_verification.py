import attr

from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.prebuilt_image import CIAB_REGISTRY
from compute_space.core.prebuilt_image import image_registry
from compute_space.core.prebuilt_image import is_verified_registry
from compute_space.core.releases import follows_releases


@attr.s(auto_attribs=True, frozen=True)
class VerificationLoss:
    """An update that would run an image whose signature isn't verified, on an app whose last prebuilt image was."""

    # The running manifest's prebuilt_image, when it names one from a registry we verify. None when the app is built
    # now (it was on a pinned ref, say) and its last verified image came from an earlier version.
    previous_image: str | None
    # The update's prebuilt_image, from a registry we don't verify.
    new_image: str

    @property
    def previous_registry(self) -> str:
        # Only our registry is verified, so a verified image came from it.
        return image_registry(self.previous_image) if self.previous_image else CIAB_REGISTRY

    @property
    def new_registry(self) -> str:
        return image_registry(self.new_image)

    def describe(self) -> str:
        return (
            f"image no longer signature-verified (moves from {self.previous_registry} to {self.new_registry}, "
            "which isn't verified)"
        )


def verification_lost(
    manifest: AppManifest, repo_url: str | None, was_verified: bool, previous_manifest_raw: str | None
) -> VerificationLoss | None:
    """Whether deploying ``manifest`` would take an app whose last prebuilt image was signature-verified
    (``was_verified``, the app's ``prebuilt_image_verified``) to a prebuilt image that isn't. Reads only the manifest
    and the repo URL, so it needs no network and runs before anything is pulled or stopped.

    Nothing is lost when the app never ran a verified image, when the new image is verified too, or when the update
    builds instead of pulling (no ``prebuilt_image``, or a pinned ref, which ignores it): building the release's own
    Dockerfile is how apps have always run. The update may still end up building in place of its image, if the
    registry can't be reached at the time; it's asked about all the same, since whether it pulls can't be known
    before it tries, and the flag then stays set for the next update to ask again."""
    if not was_verified or not follows_releases(repo_url, manifest):
        return None
    assert manifest.prebuilt_image is not None
    if is_verified_registry(manifest.prebuilt_image):
        return None
    return VerificationLoss(
        previous_image=_previous_verified_image(previous_manifest_raw), new_image=manifest.prebuilt_image
    )


def _previous_verified_image(previous_manifest_raw: str | None) -> str | None:
    if not previous_manifest_raw:
        return None
    try:
        previous = parse_manifest_from_string(previous_manifest_raw).prebuilt_image
    except ValueError:
        return None
    return previous if previous is not None and is_verified_registry(previous) else None
