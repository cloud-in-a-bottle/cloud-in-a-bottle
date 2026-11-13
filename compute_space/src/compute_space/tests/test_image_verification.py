"""Which updates take an app from a signature-verified prebuilt image to one that isn't, read from the manifests alone
before anything is pulled, and how that joins the update review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from compute_space.core.app_update_review import pending_update_review
from compute_space.core.image_verification import VerificationLoss
from compute_space.core.image_verification import verification_lost
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db.connection import init_db

from .conftest import _make_test_config

_SIGNED = "registry.cloudinabottle.org/bottled-vaultwarden"
_UNSIGNED = "ghcr.io/cloud-in-a-bottle/bottled-vaultwarden"
_REPO_URL = "https://github.com/cloud-in-a-bottle/bottled-vaultwarden"


def _raw(image: str | None, extra: str = "") -> str:
    prebuilt = f'prebuilt_image = "{image}"\n' if image else ""
    return f"""\
[app]
name = "vaultwarden"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 8080
{prebuilt}
{extra}
"""


def _lost(
    new_image: str | None,
    *,
    was_verified: bool = True,
    repo_url: str | None = _REPO_URL,
    previous: str | None = _raw(_SIGNED),
) -> VerificationLoss | None:
    return verification_lost(parse_manifest_from_string(_raw(new_image)), repo_url, was_verified, previous)


def test_a_verified_app_moving_to_a_registry_we_dont_verify_loses_verification() -> None:
    loss = _lost(_UNSIGNED)
    assert loss == VerificationLoss(previous_image=_SIGNED, new_image=_UNSIGNED)
    assert loss.previous_registry == "registry.cloudinabottle.org"
    assert loss.new_registry == "ghcr.io"
    assert "registry.cloudinabottle.org" in loss.describe() and "ghcr.io" in loss.describe()


@pytest.mark.parametrize(
    ("image", "registry"),
    [("library/vaultwarden", "docker.io"), ("localhost:5000/vaultwarden", "localhost:5000")],
)
def test_any_other_registry_counts(image: str, registry: str) -> None:
    loss = _lost(image)
    assert loss is not None and loss.new_registry == registry


def test_an_app_that_never_ran_a_verified_image_is_not_asked() -> None:
    assert _lost(_UNSIGNED, was_verified=False, previous=_raw(_UNSIGNED)) is None
    assert _lost(_UNSIGNED, was_verified=False, previous=_raw(None)) is None


def test_staying_on_our_registry_is_not_a_loss() -> None:
    assert _lost(_SIGNED) is None


def test_an_update_that_drops_prebuilt_image_builds_and_is_not_a_loss() -> None:
    assert _lost(None) is None


@pytest.mark.parametrize("pin", ["main", "feature", "v1.0.0", "d961ca45c2995c6e80fb4f539b1fb7b163e07bdd"])
def test_a_pinned_ref_ignores_prebuilt_image_and_is_not_a_loss(pin: str) -> None:
    assert _lost(_UNSIGNED, repo_url=f"{_REPO_URL}@{pin}") is None


def test_an_app_with_no_repo_url_builds_and_is_not_a_loss() -> None:
    assert _lost(_UNSIGNED, repo_url=None) is None


@pytest.mark.parametrize("previous", [None, _raw(None), _raw(_UNSIGNED), "not toml ["])
def test_a_loss_without_a_verified_image_in_the_running_manifest_names_our_registry(previous: str | None) -> None:
    """The running version may be built (it was on a pinned ref, or fell back to building after the owner approved an
    unverified image) while its last pulled image was verified. The loss is still raised, naming our registry, the
    only one whose images are verified."""
    loss = _lost(_UNSIGNED, previous=previous)
    assert loss == VerificationLoss(previous_image=None, new_image=_UNSIGNED)
    assert loss.previous_registry == "registry.cloudinabottle.org"


# --- pending_update_review ---

_NEW_PERMISSION = """\
[[services.v2.consumes]]
service = "github.com/x/secrets"
shortname = "secrets"
version = ">=0.1.0"
grants = [{ key = "API_KEY" }]
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    cfg: Any = _make_test_config(tmp_path, port=20870)
    init_db(cfg.db_path)
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _review(repo: Path, new_raw: str, was_verified: bool, repo_url: str = _REPO_URL) -> Any:
    (repo / "cloudinabottle.toml").write_text(new_raw)
    return pending_update_review("vw-id", str(repo), _raw(_SIGNED), repo_url, was_verified)


def test_an_image_losing_verification_alone_needs_review(repo: Path) -> None:
    """prebuilt_image is never review-gated as a setting, so the loss is the only reason to ask here."""
    review = _review(repo, _raw(_UNSIGNED), was_verified=True)
    assert review is not None
    assert review.verification_loss == VerificationLoss(previous_image=_SIGNED, new_image=_UNSIGNED)
    assert not review.gates_on_settings
    assert review.new_permissions == []
    # The registry move is still in the diff, for context.
    assert [c.label for c in review.settings_changed] == ["Prebuilt image"]
    assert review.describe() == (
        "approve changes to: image no longer signature-verified "
        "(moves from registry.cloudinabottle.org to ghcr.io, which isn't verified)"
    )


def test_the_same_move_on_an_app_that_never_ran_a_verified_image_needs_no_review(repo: Path) -> None:
    assert _review(repo, _raw(_UNSIGNED), was_verified=False) is None


def test_the_same_move_on_a_pinned_app_needs_no_review(repo: Path) -> None:
    assert _review(repo, _raw(_UNSIGNED), was_verified=True, repo_url=f"{_REPO_URL}@main") is None


def test_a_verified_update_needs_no_review(repo: Path) -> None:
    assert _review(repo, _raw(_SIGNED), was_verified=True) is None


def test_the_loss_joins_other_review_items(repo: Path) -> None:
    review = _review(repo, _raw(_UNSIGNED, _NEW_PERMISSION), was_verified=True)
    assert review is not None
    assert review.gates_on_settings
    assert review.verification_loss is not None
    assert [pg.service_url for pg in review.new_permissions] == ["github.com/x/secrets"]
    assert review.describe().endswith("permission github.com/x/secrets, " + review.verification_loss.describe())


def test_other_review_items_alone_have_no_loss(repo: Path) -> None:
    review = _review(repo, _raw(_SIGNED, _NEW_PERMISSION), was_verified=True)
    assert review is not None
    assert review.verification_loss is None
