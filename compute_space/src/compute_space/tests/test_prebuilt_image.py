"""Pulling an app's prebuilt image, verifying its signature when it's from our registry, and that a failure never
falls back to a build."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import compute_space.core.apps as apps_mod
from compute_space.core.prebuilt_image import SIGNING_IDENTITY
from compute_space.core.prebuilt_image import SIGNING_OIDC_ISSUER
from compute_space.core.prebuilt_image import PrebuiltImageError
from compute_space.core.prebuilt_image import SignaturePolicy
from compute_space.core.prebuilt_image import cosign_verify_command
from compute_space.core.prebuilt_image import github_repository
from compute_space.core.prebuilt_image import image_registry
from compute_space.core.prebuilt_image import pull_prebuilt_image
from compute_space.core.prebuilt_image import signature_policy
from compute_space.core.prebuilt_image import verified_digest

_SHA = "d961ca45c2995c6e80fb4f539b1fb7b163e07bdd"
_IMAGE = "registry.cloudinabottle.org/bottled-vaultwarden"
_REPO_URL = "https://github.com/cloud-in-a-bottle/bottled-vaultwarden"
_OTHER_IMAGE = "ghcr.io/cloud-in-a-bottle/bottled-vaultwarden"


class _FakeRun:
    """Stands in for subprocess.run, answering podman by its subcommand."""

    def __init__(self, answers: dict[tuple[str, str], subprocess.CompletedProcess[str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.answers[(cmd[0], cmd[1])]


def _done(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_pulls_the_commits_image_and_tags_it_as_the_app_image() -> None:
    """From a registry we don't sign for, the commit's tag is pulled as is."""
    fake = _FakeRun({("podman", "pull"): _done(), ("podman", "tag"): _done()})
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        tag = pull_prebuilt_image("vaultwarden", _OTHER_IMAGE, _SHA, _REPO_URL, log=lambda _: None)
    assert tag == "openhost-vaultwarden:latest"
    assert fake.calls == [
        ["podman", "pull", f"{_OTHER_IMAGE}:{_SHA}"],
        ["podman", "tag", f"{_OTHER_IMAGE}:{_SHA}", "openhost-vaultwarden:latest"],
    ]


def test_missing_image_fails() -> None:
    fake = _FakeRun({("podman", "pull"): _done(returncode=125, stderr="manifest unknown")})
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        with pytest.raises(PrebuiltImageError, match="manifest unknown"):
            pull_prebuilt_image("vaultwarden", _OTHER_IMAGE, _SHA, _REPO_URL, log=lambda _: None)


def test_requires_a_full_commit_sha() -> None:
    with pytest.raises(PrebuiltImageError, match="full commit sha"):
        pull_prebuilt_image("vaultwarden", _IMAGE, "main", _REPO_URL, log=lambda _: None)


def test_app_using_a_prebuilt_image_never_builds(tmp_path: Path) -> None:
    """A failed pull propagates; build_image is never called as a fallback."""
    manifest = mock.Mock(prebuilt_image=_IMAGE)
    db = mock.Mock()
    db.execute.return_value.fetchone.return_value = {"repo_url": _REPO_URL}
    with (
        mock.patch.object(apps_mod, "follows_releases", return_value=True),
        mock.patch.object(apps_mod, "head_commit", return_value=_SHA),
        mock.patch.object(apps_mod, "pull_prebuilt_image", side_effect=PrebuiltImageError("no such image")),
        mock.patch.object(apps_mod, "build_image") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        with pytest.raises(PrebuiltImageError, match="no such image"):
            apps_mod._build_then_run_app(
                "app-id",
                "app",
                str(tmp_path),
                manifest,
                db,
                mock.Mock(temporary_data_dir=str(tmp_path)),
                max_build_attempts=3,
            )
    build.assert_not_called()
    run.assert_not_called()


def test_app_with_a_pinned_ref_builds_despite_prebuilt_image(tmp_path: Path) -> None:
    manifest = mock.Mock(prebuilt_image=_IMAGE, container_image="Dockerfile", effective_build_memory_mb=512)
    db = mock.Mock()
    db.execute.return_value.fetchone.return_value = {"repo_url": f"{_REPO_URL}@feature"}
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        apps_mod._build_then_run_app(
            "app-id", "app", str(tmp_path), manifest, db, mock.Mock(temporary_data_dir=str(tmp_path))
        )
    pull.assert_not_called()
    build.assert_called_once()
    run.assert_called_once()


# ─── signatures (registry.cloudinabottle.org) ───

_DIGEST = "sha256:" + "8" * 64


@pytest.mark.parametrize(
    ("image", "registry"),
    [
        (_IMAGE, "registry.cloudinabottle.org"),
        ("localhost:5000/app", "localhost:5000"),
        ("ghcr.io/org/app", "ghcr.io"),
        ("library/nginx", "docker.io"),
        ("nginx", "docker.io"),
    ],
)
def test_image_registry(image: str, registry: str) -> None:
    assert image_registry(image) == registry


@pytest.mark.parametrize(
    ("repo_url", "repository"),
    [
        (_REPO_URL, "cloud-in-a-bottle/bottled-vaultwarden"),
        (f"{_REPO_URL}.git", "cloud-in-a-bottle/bottled-vaultwarden"),
        ("https://gitlab.com/someone/app", None),
        ("file:///srv/app", None),
    ],
)
def test_github_repository(repo_url: str, repository: str | None) -> None:
    assert github_repository(repo_url) == repository


def test_our_registry_requires_a_signature_from_the_apps_own_repo_at_the_commit() -> None:
    assert signature_policy(_IMAGE, _REPO_URL, _SHA) == SignaturePolicy(
        oidc_issuer="https://token.actions.githubusercontent.com",
        identity="https://github.com/cloud-in-a-bottle/app-template/.github/workflows/app-image.yml@refs/heads/main",
        workflow_repository="cloud-in-a-bottle/bottled-vaultwarden",
        workflow_sha=_SHA,
    )


def test_other_registries_are_not_verified() -> None:
    assert signature_policy("ghcr.io/org/app", _REPO_URL, _SHA) is None


def test_our_registry_with_a_non_github_repo_fails() -> None:
    with pytest.raises(PrebuiltImageError, match="isn't a GitHub repo"):
        signature_policy(_IMAGE, "https://gitlab.com/someone/app", _SHA)


def test_cosign_command_checks_every_part_of_the_policy() -> None:
    policy = signature_policy(_IMAGE, _REPO_URL, _SHA)
    assert policy is not None
    cmd = cosign_verify_command(f"{_IMAGE}:{_SHA}", policy)
    flags = dict(zip(cmd[3::2], cmd[4::2], strict=True))
    assert cmd[:3] == ["cosign", "verify", f"{_IMAGE}:{_SHA}"]
    assert flags == {
        "--certificate-oidc-issuer": SIGNING_OIDC_ISSUER,
        "--certificate-identity": SIGNING_IDENTITY,
        "--certificate-github-workflow-repository": "cloud-in-a-bottle/bottled-vaultwarden",
        "--certificate-github-workflow-sha": _SHA,
        "--output": "json",
    }


def _cosign_output(*digests: str) -> str:
    return json.dumps([{"critical": {"image": {"docker-manifest-digest": d}}} for d in digests])


def test_verified_digest() -> None:
    assert verified_digest(_cosign_output(_DIGEST, _DIGEST)) == _DIGEST


@pytest.mark.parametrize("output", ["not json", "[]", _cosign_output(_DIGEST, "sha256:" + "9" * 64), "[{}]"])
def test_verified_digest_rejects_unclear_output(output: str) -> None:
    with pytest.raises(PrebuiltImageError):
        verified_digest(output)


def test_pulls_the_verified_digest_from_our_registry() -> None:
    fake = _FakeRun(
        {
            ("cosign", "verify"): subprocess.CompletedProcess(
                args=[], returncode=0, stdout=_cosign_output(_DIGEST), stderr=""
            ),
            ("podman", "pull"): _done(),
            ("podman", "tag"): _done(),
        }
    )
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        pull_prebuilt_image("vaultwarden", _IMAGE, _SHA, _REPO_URL, log=lambda _: None)
    assert [c[:2] for c in fake.calls] == [["cosign", "verify"], ["podman", "pull"], ["podman", "tag"]]
    assert fake.calls[1][2] == f"{_IMAGE}@{_DIGEST}"  # by digest, not the (mutable) tag


def test_bad_signature_fails_without_pulling() -> None:
    fake = _FakeRun({("cosign", "verify"): _done(returncode=1, stderr="no matching signatures")})
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        with pytest.raises(PrebuiltImageError, match="Signature check failed"):
            pull_prebuilt_image("vaultwarden", _IMAGE, _SHA, _REPO_URL, log=lambda _: None)
    assert [c[:2] for c in fake.calls] == [["cosign", "verify"]]
