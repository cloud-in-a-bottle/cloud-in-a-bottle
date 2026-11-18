import os

import attr

from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
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
    that never hold an update back on their own (shown for context); ``new_permissions`` are the newly declared grants."""

    manifest: AppManifest
    settings_changed: list[SettingChange]
    new_permissions: list[PermissionGrant]

    def describe(self) -> str:
        parts = [c.label for c in self.settings_changed if c.review_required]
        parts += [f"permission {pg.service_url}" for pg in self.new_permissions]
        return "approve changes to: " + ", ".join(parts)


def pending_update_review(app_id: str, repo_path: str, previous_manifest_raw: str | None) -> UpdateReview | None:
    """The review the manifest checked out at ``repo_path`` needs before it can replace the running one, or None if it
    can be applied without asking: no review-required setting changed and no permission is newly declared."""
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
    # Fields that don't change what the app can do (description, authors, version,
    # health check, links) are reported in the diff but don't hold the update back on
    # their own: an app that only rewords its description gains nothing, and gating on
    # that trains the owner to click through every review.
    if not new_perms and not settings_changes_require_review(settings_changed):
        return None
    return UpdateReview(manifest=manifest, settings_changed=settings_changed, new_permissions=new_perms)
