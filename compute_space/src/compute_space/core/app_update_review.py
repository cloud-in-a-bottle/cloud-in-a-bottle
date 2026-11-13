import os

import attr

from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.image_verification import VerificationLoss
from compute_space.core.image_verification import verification_lost
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PermissionGrant
from compute_space.core.manifest import SettingChange
from compute_space.core.manifest import manifest_newly_declared_permissions_v2
from compute_space.core.manifest import manifest_settings_changes
from compute_space.core.manifest import parse_manifest
from compute_space.core.manifest import settings_changes_require_review


@attr.s(auto_attribs=True, frozen=True)
class UpdateReview:
    """An update the owner must approve before it's applied. ``settings_changed`` is the full diff, including fields
    that never hold an update back on their own (shown for context); ``new_permissions`` are the newly declared grants;
    ``verification_loss`` is set when the update's image would no longer be signature-verified."""

    manifest: AppManifest
    settings_changed: list[SettingChange]
    new_permissions: list[PermissionGrant]
    verification_loss: VerificationLoss | None

    @property
    def gates_on_settings(self) -> bool:
        """Whether the manifest's own changes (a review-required setting, or a new permission) need approval, as
        opposed to only the image losing its signature verification."""
        return bool(self.new_permissions) or settings_changes_require_review(self.settings_changed)

    def describe(self) -> str:
        parts = [c.label for c in self.settings_changed if c.review_required]
        parts += [f"permission {pg.service_url}" for pg in self.new_permissions]
        if self.verification_loss is not None:
            parts.append(self.verification_loss.describe())
        return "approve changes to: " + ", ".join(parts)


def pending_update_review(
    app_id: str,
    repo_path: str,
    previous_manifest_raw: str | None,
    repo_url: str | None,
    prebuilt_image_verified: bool,
) -> UpdateReview | None:
    """The review the manifest checked out at ``repo_path`` needs before it can replace the running one, or None if it
    can be applied without asking: no review-required setting changed, no permission is newly declared, and the image
    stays signature-verified if the app's last one was (``prebuilt_image_verified``; see
    image_verification.verification_lost). ``repo_url`` is the app's, which decides whether it pulls its image."""
    if not repo_path or not os.path.isdir(repo_path):
        return None
    try:
        manifest = parse_manifest(repo_path)
    except ValueError:
        return None

    new_perms = manifest_newly_declared_permissions_v2(
        manifest, get_all_permissions_v2(consumer_app_id=app_id), previous_manifest_raw
    )
    settings_changed = manifest_settings_changes(manifest, previous_manifest_raw)
    loss = verification_lost(manifest, repo_url, prebuilt_image_verified, previous_manifest_raw)
    # Fields that don't change what the app can do (description, authors, version,
    # health check, links) are reported in the diff but don't hold the update back on
    # their own: an app that only rewords its description gains nothing, and gating on
    # that trains the owner to click through every review.
    review = UpdateReview(
        manifest=manifest, settings_changed=settings_changed, new_permissions=new_perms, verification_loss=loss
    )
    return review if review.gates_on_settings or loss is not None else None
