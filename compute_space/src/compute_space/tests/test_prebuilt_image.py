"""Pulling an app's prebuilt image, verifying its signature when it's from our registry: an image that can't be fetched
(or whose signature can't be checked, for want of the registry) is retried and then built locally instead, while a
verification failure fails without retrying or building."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import compute_space.core.apps as apps_mod
from compute_space.core.apps import ImageSource
from compute_space.core.apps import PreparedImage
from compute_space.core.apps import deploy_app_background
from compute_space.core.apps import prepare_app_image
from compute_space.core.apps import start_app_process
from compute_space.core.prebuilt_image import SIGNING_IDENTITY
from compute_space.core.prebuilt_image import SIGNING_OIDC_ISSUER
from compute_space.core.prebuilt_image import ImageToPull
from compute_space.core.prebuilt_image import ImageUnavailableError
from compute_space.core.prebuilt_image import ImageVerificationError
from compute_space.core.prebuilt_image import PrebuiltImageError
from compute_space.core.prebuilt_image import PulledImage
from compute_space.core.prebuilt_image import SignaturePolicy
from compute_space.core.prebuilt_image import cosign_verify_command
from compute_space.core.prebuilt_image import github_repository
from compute_space.core.prebuilt_image import image_registry
from compute_space.core.prebuilt_image import pull_prebuilt_image
from compute_space.core.prebuilt_image import signature_policy
from compute_space.core.prebuilt_image import verified_digest
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config

_SHA = "d961ca45c2995c6e80fb4f539b1fb7b163e07bdd"
# Pull mechanics are tested with a registry we don't sign for; signatures, further down, with ours.
_IMAGE = "ghcr.io/cloud-in-a-bottle/bottled-vaultwarden"
_SIGNED_IMAGE = "registry.cloudinabottle.org/bottled-vaultwarden"
_REPO_URL = "https://github.com/cloud-in-a-bottle/bottled-vaultwarden"
_PULL = ["podman", "pull", "--retry=0", f"{_IMAGE}:{_SHA}"]
_TAG = ["podman", "tag", f"{_IMAGE}:{_SHA}", "openhost-vaultwarden:latest"]


class _FakeRun:
    """Stands in for subprocess.run, answering podman by its subcommand. A list of answers is used in turn; an
    exception is raised."""

    def __init__(self, answers: dict[tuple[str, str], list[subprocess.CompletedProcess[str] | Exception]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        answers = self.answers[(cmd[0], cmd[1])]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _done(returncode: int = 0, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


_UNREACHABLE = _done(125, "pinging container registry registry.cloudinabottle.org: dial tcp: connection refused")


@pytest.fixture
def sleep() -> Iterator[mock.Mock]:
    with mock.patch("compute_space.core.prebuilt_image.time.sleep") as s:
        yield s


def _pull(fake: _FakeRun, image: str = _IMAGE, repo_url: str = _REPO_URL) -> PulledImage:
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        return pull_prebuilt_image("vaultwarden", image, _SHA, repo_url, log=lambda _: None)


def test_pulls_the_commits_image_and_tags_it_as_the_app_image(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done()]})
    assert _pull(fake) == PulledImage(tag="openhost-vaultwarden:latest", verified=False)
    assert fake.calls == [_PULL, _TAG]
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        _UNREACHABLE,
        _done(125, f"reading manifest {_SHA} in {_IMAGE}: manifest unknown"),
        _done(125, "received unexpected HTTP status: 503 Service Unavailable"),
        subprocess.TimeoutExpired(cmd="podman", timeout=1800),
    ],
)
def test_an_image_that_cant_be_fetched_is_tried_three_times(
    sleep: mock.Mock, failure: subprocess.CompletedProcess[str] | Exception
) -> None:
    fake = _FakeRun({("podman", "pull"): [failure]})
    with pytest.raises(ImageUnavailableError):
        _pull(fake)
    assert fake.calls == [_PULL, _PULL, _PULL]
    assert sleep.call_args_list == [mock.call(5), mock.call(15)]


def test_a_pull_that_succeeds_on_the_second_attempt_is_used(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_UNREACHABLE, _done()], ("podman", "tag"): [_done()]})
    assert _pull(fake).tag == "openhost-vaultwarden:latest"
    assert fake.calls == [_PULL, _PULL, _TAG]
    assert sleep.call_args_list == [mock.call(5)]


def test_the_instances_signature_policy_rejecting_the_image_is_a_verification_failure(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_done(125, "Source image rejected: A signature was required")]})
    with pytest.raises(ImageVerificationError, match="Source image rejected"):
        _pull(fake)
    assert fake.calls == [_PULL]
    sleep.assert_not_called()


def test_a_verification_failure_in_the_hook_is_not_retried(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_done()]})
    with (
        mock.patch(
            "compute_space.core.prebuilt_image._image_to_pull", side_effect=ImageVerificationError("bad signature")
        ) as hook,
        pytest.raises(ImageVerificationError, match="bad signature"),
    ):
        _pull(fake)
    hook.assert_called_once()
    assert fake.calls == []
    sleep.assert_not_called()


def test_the_hook_choosing_what_to_pull_is_retried_with_the_pull(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done()]})
    to_pull = ImageToPull(ref=f"{_IMAGE}:{_SHA}", verified=True)
    with mock.patch(
        "compute_space.core.prebuilt_image._image_to_pull",
        side_effect=[ImageUnavailableError("registry unreachable"), to_pull],
    ) as hook:
        assert _pull(fake) == PulledImage(tag="openhost-vaultwarden:latest", verified=True)
    assert hook.call_count == 2


def test_a_failed_retag_fails_without_retrying(sleep: mock.Mock) -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done(125, "no space left on device")]})
    with pytest.raises(PrebuiltImageError, match="no space left") as e:
        _pull(fake)
    assert not isinstance(e.value, ImageUnavailableError)
    assert fake.calls == [_PULL, _TAG]


def test_requires_a_full_commit_sha() -> None:
    with pytest.raises(PrebuiltImageError, match="full commit sha"):
        pull_prebuilt_image("vaultwarden", _IMAGE, "main", _REPO_URL, log=lambda _: None)


def _manifest() -> mock.Mock:
    return mock.Mock(prebuilt_image=_IMAGE, container_image="Dockerfile", effective_build_memory_mb=512)


def _prepare(tmp_path: Path, repo_url: str, log: Callable[[str], None] = lambda _: None) -> PreparedImage:
    with mock.patch.object(apps_mod, "head_commit", return_value=_SHA):
        return prepare_app_image("app", str(tmp_path), _manifest(), repo_url, str(tmp_path), log=log)


def test_an_unavailable_image_is_built_locally_instead(tmp_path: Path) -> None:
    lines: list[str] = []
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image", side_effect=ImageUnavailableError("unreachable")) as pull,
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
    ):
        prepared = _prepare(tmp_path, _REPO_URL, lines.append)
    assert prepared == PreparedImage(tag="openhost-app:latest", source=ImageSource.FALLBACK_BUILT)
    pull.assert_called_once()
    build.assert_called_once()
    assert build.call_args.args[:3] == ("app", str(tmp_path), "Dockerfile")
    assert any("falling back to building it locally" in line for line in lines)


def test_a_failed_fallback_build_reports_both_failures(tmp_path: Path) -> None:
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image", side_effect=ImageUnavailableError("unreachable")),
        mock.patch.object(apps_mod, "build_image", side_effect=RuntimeError("build exploded")),
        pytest.raises(RuntimeError, match="unreachable.*building it locally failed: build exploded"),
    ):
        _prepare(tmp_path, _REPO_URL)


@pytest.mark.parametrize(
    "error", [ImageVerificationError("bad signature"), PrebuiltImageError("podman is not installed")]
)
def test_a_failure_other_than_unavailable_never_builds(tmp_path: Path, error: PrebuiltImageError) -> None:
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image", side_effect=error),
        mock.patch.object(apps_mod, "build_image") as build,
        pytest.raises(PrebuiltImageError, match=str(error)),
    ):
        _prepare(tmp_path, _REPO_URL)
    build.assert_not_called()


def test_a_pulled_image_is_not_built(tmp_path: Path) -> None:
    pulled = PulledImage(tag="openhost-app:latest", verified=False)
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image", return_value=pulled),
        mock.patch.object(apps_mod, "build_image") as build,
    ):
        assert _prepare(tmp_path, _REPO_URL) == PreparedImage(tag="openhost-app:latest", source=ImageSource.PULLED)
    build.assert_not_called()


def test_app_with_a_pinned_ref_builds_despite_prebuilt_image(tmp_path: Path) -> None:
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
    ):
        assert _prepare(tmp_path, f"{_REPO_URL}@feature").source is ImageSource.BUILT
    pull.assert_not_called()
    build.assert_called_once()


def test_a_prepared_image_is_run_without_pulling_or_building(tmp_path: Path) -> None:
    db = mock.Mock()
    db.execute.return_value.fetchone.return_value = {"repo_url": _REPO_URL}
    prepared = PreparedImage(tag="openhost-app:latest", source=ImageSource.FALLBACK_BUILT)
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
        mock.patch.object(apps_mod, "build_image") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        apps_mod._build_then_run_app(
            "app-id",
            "app",
            str(tmp_path),
            _manifest(),
            db,
            mock.Mock(temporary_data_dir=str(tmp_path)),
            prepared_image=prepared,
        )
    pull.assert_not_called()
    build.assert_not_called()
    assert run.call_args.args[:2] == ("app-id", "openhost-app:latest")


_INSTALL_MANIFEST = f"""\
[app]
name = "vaultwarden"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 8080
prebuilt_image = "{_IMAGE}"
"""


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Any, Path]:
    """An installed app that follows releases. Returns (config, its checkout)."""
    cfg: Any = _make_test_config(tmp_path, port=20850)
    init_db(cfg.db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(_INSTALL_MANIFEST)
    db = sqlite3.connect(cfg.db_path)
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
        "VALUES ('vw-id', 'vaultwarden', '1.0.0', ?, ?, 20851, 'building')",
        (str(repo), _REPO_URL),
    )
    db.commit()
    db.close()
    return cfg, repo


@contextlib.contextmanager
def _unreachable_registry() -> Iterator[dict[str, Any]]:
    """podman can't reach the registry; the build and the run are mocked."""
    fake = _FakeRun({("podman", "pull"): [_UNREACHABLE]})
    with (
        mock.patch.object(apps_mod, "head_commit", return_value=_SHA),
        mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake),
        mock.patch.object(apps_mod, "build_image", return_value="openhost-vaultwarden:latest") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        yield {"podman": fake, "build": build, "run": run}


def _assert_fell_back(m: dict[str, Any], cfg: Any) -> None:
    assert m["podman"].calls == [_PULL, _PULL, _PULL]
    m["build"].assert_called_once()
    m["run"].assert_called_once()
    assert m["run"].call_args.args[:2] == ("vw-id", "openhost-vaultwarden:latest")
    log = Path(apps_mod.app_log_path("vaultwarden", cfg)).read_text()
    assert "falling back to building it locally" in log


def test_install_falls_back_to_building_when_the_registry_is_unreachable(
    installed: tuple[Any, Path], sleep: mock.Mock
) -> None:
    """Through the install's background deploy: three failed pulls, then a local build that runs."""
    cfg, repo = installed
    manifest = apps_mod.parse_manifest_from_string(_INSTALL_MANIFEST)
    with _unreachable_registry() as m:
        deploy_app_background(manifest, str(repo), cfg, "vw-id", "vaultwarden")
    _assert_fell_back(m, cfg)
    db = sqlite3.connect(cfg.db_path)
    try:
        assert db.execute("SELECT status FROM apps WHERE app_id = 'vw-id'").fetchone()[0] == "building"
    finally:
        db.close()


def test_plain_reload_falls_back_to_building_when_the_registry_is_unreachable(
    installed: tuple[Any, Path], sleep: mock.Mock
) -> None:
    """A plain reload (and any rebuild) starts the app through start_app_process, after the app has stopped."""
    cfg, _ = installed
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        with _unreachable_registry() as m:
            start_app_process("vw-id", db, cfg)
    finally:
        db.close()
    _assert_fell_back(m, cfg)


# --- signatures (registry.cloudinabottle.org) ---

_DIGEST = "sha256:" + "8" * 64
_TAGGED = f"{_SIGNED_IMAGE}:{_SHA}"
_PINNED = f"{_SIGNED_IMAGE}@{_DIGEST}"


@pytest.mark.parametrize(
    ("image", "registry"),
    [
        (_SIGNED_IMAGE, "registry.cloudinabottle.org"),
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
    assert signature_policy(_SIGNED_IMAGE, _REPO_URL, _SHA) == SignaturePolicy(
        oidc_issuer="https://token.actions.githubusercontent.com",
        identity="https://github.com/cloud-in-a-bottle/app-template/.github/workflows/app-image.yml@refs/heads/main",
        workflow_repository="cloud-in-a-bottle/bottled-vaultwarden",
        workflow_sha=_SHA,
    )


def test_other_registries_are_not_verified() -> None:
    assert signature_policy("ghcr.io/org/app", _REPO_URL, _SHA) is None


def test_our_registry_with_a_non_github_repo_fails() -> None:
    with pytest.raises(PrebuiltImageError, match="isn't a GitHub repo"):
        signature_policy(_SIGNED_IMAGE, "https://gitlab.com/someone/app", _SHA)


def test_cosign_command_checks_every_part_of_the_policy() -> None:
    policy = signature_policy(_SIGNED_IMAGE, _REPO_URL, _SHA)
    assert policy is not None
    cmd = cosign_verify_command(_TAGGED, policy)
    flags = dict(zip(cmd[3::2], cmd[4::2], strict=True))
    assert cmd[:3] == ["cosign", "verify", _TAGGED]
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


def test_pulls_the_verified_digest_from_our_registry(sleep: mock.Mock) -> None:
    fake = _FakeRun(
        {
            ("cosign", "verify"): [_done(stdout=_cosign_output(_DIGEST))],
            ("podman", "pull"): [_done()],
            ("podman", "tag"): [_done()],
        }
    )
    assert _pull(fake, image=_SIGNED_IMAGE) == PulledImage(tag="openhost-vaultwarden:latest", verified=True)
    assert [c[:2] for c in fake.calls] == [["cosign", "verify"], ["podman", "pull"], ["podman", "tag"]]
    assert fake.calls[1] == ["podman", "pull", "--retry=0", _PINNED]  # by digest, not the (mutable) tag


def test_bad_signature_fails_without_retrying_or_pulling(sleep: mock.Mock) -> None:
    fake = _FakeRun({("cosign", "verify"): [_done(returncode=1, stderr="no matching signatures")]})
    with pytest.raises(ImageVerificationError, match="Signature check failed"):
        _pull(fake, image=_SIGNED_IMAGE)
    assert [c[:2] for c in fake.calls] == [["cosign", "verify"]]
    sleep.assert_not_called()
