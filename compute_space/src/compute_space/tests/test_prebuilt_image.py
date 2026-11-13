"""Pulling an app's prebuilt image, verifying its signature when it's from our registry: an image that can't be fetched
(or whose signature can't be checked, for want of the registry) is retried and then built locally instead, while a
verification failure fails without retrying or building."""

from __future__ import annotations

import contextlib
import json
import shutil
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
from compute_space.core.prebuilt_image import cosign_triangulate_command
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


def _manifest(image: str = _IMAGE) -> mock.Mock:
    return mock.Mock(prebuilt_image=image, container_image="Dockerfile", effective_build_memory_mb=512)


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


@pytest.mark.parametrize(
    ("verified", "source"), [(True, ImageSource.PULLED_VERIFIED), (False, ImageSource.PULLED_UNVERIFIED)]
)
def test_a_pulled_image_is_not_built_and_says_whether_it_was_verified(
    tmp_path: Path, verified: bool, source: ImageSource
) -> None:
    pulled = PulledImage(tag="openhost-app:latest", verified=verified)
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image", return_value=pulled),
        mock.patch.object(apps_mod, "build_image") as build,
    ):
        assert _prepare(tmp_path, _REPO_URL) == PreparedImage(tag="openhost-app:latest", source=source)
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
_RESOLVED = _done(stdout=_PINNED + "\n")
_BAD_SIGNATURE = _done(
    1,
    "Error: no matching attestations: failed to verify certificate identity: no matching CertificateIdentity found, "
    'last error: expected GithubWorkflowRepository to be "cloud-in-a-bottle/bottled-vaultwarden", '
    'got "someone/else"',
)
_RESOLVE_CMD = ["cosign", "triangulate", "--type", "digest", _TAGGED]
_SIGNED_PULL = ["podman", "pull", "--retry=0", _PINNED]


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


@pytest.mark.parametrize("repo_url", ["https://gitlab.com/someone/app", None])
def test_our_registry_with_a_non_github_repo_fails_verification(repo_url: str | None) -> None:
    with pytest.raises(ImageVerificationError, match="isn't a GitHub repo"):
        signature_policy(_SIGNED_IMAGE, repo_url, _SHA)


def test_cosign_command_checks_every_part_of_the_policy() -> None:
    policy = signature_policy(_SIGNED_IMAGE, _REPO_URL, _SHA)
    assert policy is not None
    cmd = cosign_verify_command(_PINNED, policy)
    flags = dict(zip(cmd[3::2], cmd[4::2], strict=True))
    assert cmd[:3] == ["cosign", "verify", _PINNED]
    assert flags == {
        "--certificate-oidc-issuer": SIGNING_OIDC_ISSUER,
        "--certificate-identity": SIGNING_IDENTITY,
        "--certificate-github-workflow-repository": "cloud-in-a-bottle/bottled-vaultwarden",
        "--certificate-github-workflow-sha": _SHA,
        "--output": "json",
    }


def test_cosign_still_has_triangulate() -> None:
    """Digests are resolved with ``cosign triangulate``, deprecated for removal in cosign 4: an upgrade that drops it
    would make every image look unavailable, so every install would quietly build instead."""
    cosign = shutil.which("cosign")
    assert cosign is not None, "cosign is a dependency (pyproject) and must be on PATH"
    result = subprocess.run(
        [cosign, *cosign_triangulate_command(_TAGGED)[1:-1], "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "--type" in result.stdout


def _cosign_output(*digests: str) -> str:
    return json.dumps([{"critical": {"image": {"docker-manifest-digest": d}}} for d in digests])


def test_verified_digest() -> None:
    assert verified_digest(_cosign_output(_DIGEST, _DIGEST)) == _DIGEST


@pytest.mark.parametrize("output", ["not json", "[]", _cosign_output(_DIGEST, "sha256:" + "9" * 64), "[{}]"])
def test_verified_digest_rejects_unclear_output(output: str) -> None:
    with pytest.raises(ImageVerificationError):
        verified_digest(output)


def _signed(
    resolve: list[subprocess.CompletedProcess[str] | Exception],
    verify: list[subprocess.CompletedProcess[str] | Exception] | None = None,
) -> _FakeRun:
    return _FakeRun(
        {
            ("cosign", "triangulate"): resolve,
            ("cosign", "verify"): verify or [_done(stdout=_cosign_output(_DIGEST))],
            ("podman", "pull"): [_done()],
            ("podman", "tag"): [_done()],
        }
    )


def _pull_signed(fake: _FakeRun) -> PulledImage:
    return _pull(fake, image=_SIGNED_IMAGE)


def test_our_registrys_image_is_resolved_verified_and_pulled_by_digest(sleep: mock.Mock) -> None:
    fake = _signed([_RESOLVED])
    assert _pull_signed(fake) == PulledImage(tag="openhost-vaultwarden:latest", verified=True)
    assert fake.calls[0] == _RESOLVE_CMD
    assert fake.calls[1][:3] == ["cosign", "verify", _PINNED]  # the digest, not the (mutable) tag
    assert fake.calls[2:] == [_SIGNED_PULL, ["podman", "tag", _PINNED, "openhost-vaultwarden:latest"]]
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "verify",
    [
        _BAD_SIGNATURE,
        _done(1, "Error: no matching attestations: none of the attestations matched the predicate type"),
        _done(12, "Error: no matching signatures: ..."),
        _done(10, "Error: no signatures found"),
    ],
)
def test_a_bad_signature_fails_without_retrying_pulling_or_building(
    sleep: mock.Mock, verify: subprocess.CompletedProcess[str]
) -> None:
    fake = _signed([_RESOLVED], [verify])
    with pytest.raises(ImageVerificationError, match="Signature check failed"):
        _pull_signed(fake)
    assert all(c[0] == "cosign" for c in fake.calls)
    assert [c[1] for c in fake.calls].count("verify") == 1
    sleep.assert_not_called()


def test_a_verified_digest_other_than_the_resolved_one_fails(sleep: mock.Mock) -> None:
    fake = _signed([_RESOLVED], [_done(stdout=_cosign_output("sha256:" + "9" * 64))])
    with pytest.raises(ImageVerificationError, match="not the"):
        _pull_signed(fake)
    assert [c[:2] for c in fake.calls] == [["cosign", "triangulate"], ["cosign", "verify"]]


_DIAL_FAILED = _done(
    1,
    'Error: Get "https://registry.cloudinabottle.org/v2/": dial tcp 10.0.0.1:443: connect: connection refused',
)


def test_an_unreachable_registry_is_retried_without_verifying(sleep: mock.Mock) -> None:
    fake = _signed([_DIAL_FAILED])
    with pytest.raises(ImageUnavailableError, match="Looking up"):
        _pull_signed(fake)
    assert fake.calls == [_RESOLVE_CMD, _RESOLVE_CMD, _RESOLVE_CMD]
    assert sleep.call_args_list == [mock.call(5), mock.call(15)]


@pytest.mark.parametrize(
    "verify",
    [
        # Sigstore's trust root can't be fetched (cosign 3.1.4's output with the network down).
        _done(
            1,
            "Error: setting up clients and keys: getting rekor public keys: updating local metadata and targets: "
            'error updating to TUF remote mirror: tuf: failed to download 13.root.json: Get "https://tuf-repo-'
            'cdn.sigstore.dev/13.root.json": proxyconnect tcp: dial tcp 127.0.0.1:1: connect: connection refused',
        ),
        _done(1, "Error: GET https://registry.cloudinabottle.org/v2/...: unexpected status code 502 Bad Gateway"),
        subprocess.TimeoutExpired(cmd="cosign", timeout=300),
    ],
)
def test_a_signature_that_cant_be_checked_for_want_of_the_network_is_retried(
    sleep: mock.Mock, verify: subprocess.CompletedProcess[str] | Exception
) -> None:
    fake = _signed([_RESOLVED], [verify])
    with pytest.raises(ImageUnavailableError):
        _pull_signed(fake)
    assert [c[1] for c in fake.calls] == ["triangulate", "verify"] * 3
    assert sleep.call_count == 2


def test_an_unexplained_cosign_failure_with_the_registry_gone_is_unavailable(sleep: mock.Mock) -> None:
    """cosign's error doesn't say why, but the registry no longer answers: the check couldn't be made."""
    fake = _signed([_RESOLVED, _DIAL_FAILED], [_done(1, "Error: something went wrong")])
    with pytest.raises(ImageUnavailableError):
        _pull_signed(fake)
    # Retried (rather than failing as a bad signature), and the registry stays gone for the later attempts.
    assert [c[1] for c in fake.calls] == ["triangulate", "verify", "triangulate", "triangulate", "triangulate"]
    assert sleep.call_count == 2
    assert not any(c[:2] == ["podman", "pull"] for c in fake.calls)


def test_an_unexplained_cosign_failure_with_the_registry_still_up_is_a_verification_failure(
    sleep: mock.Mock,
) -> None:
    """cosign's error doesn't say why, but the registry still answers: treat it as a failed check, not an outage."""
    fake = _signed([_RESOLVED, _RESOLVED], [_done(1, "Error: something went wrong")])
    with pytest.raises(ImageVerificationError):
        _pull_signed(fake)
    assert [c[1] for c in fake.calls] == ["triangulate", "verify", "triangulate"]
    sleep.assert_not_called()
    assert not any(c[:2] == ["podman", "pull"] for c in fake.calls)


def test_a_non_github_repo_on_our_registry_fails_without_building(tmp_path: Path) -> None:
    manifest = _manifest(_SIGNED_IMAGE)
    fake = _signed([_RESOLVED])
    with (
        mock.patch.object(apps_mod, "head_commit", return_value=_SHA),
        mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake),
        mock.patch.object(apps_mod, "build_image") as build,
        pytest.raises(ImageVerificationError, match="isn't a GitHub repo"),
    ):
        prepare_app_image(
            "app", str(tmp_path), manifest, "https://gitlab.com/x/app", str(tmp_path), log=lambda _: None
        )
    build.assert_not_called()
    assert fake.calls == []


def _prepare_signed(tmp_path: Path, fake: _FakeRun) -> tuple[PreparedImage | None, Exception | None, mock.Mock]:
    manifest = _manifest(_SIGNED_IMAGE)
    with (
        mock.patch.object(apps_mod, "head_commit", return_value=_SHA),
        mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake),
        mock.patch("compute_space.core.prebuilt_image.time.sleep"),
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
    ):
        try:
            return (
                prepare_app_image("app", str(tmp_path), manifest, _REPO_URL, str(tmp_path), log=lambda _: None),
                None,
                build,
            )
        except Exception as e:  # noqa: BLE001
            return None, e, build


def test_with_our_registry_unreachable_the_release_is_built_locally(tmp_path: Path) -> None:
    prepared, error, build = _prepare_signed(tmp_path, _signed([_DIAL_FAILED]))
    assert error is None
    assert prepared == PreparedImage(tag="openhost-app:latest", source=ImageSource.FALLBACK_BUILT)
    build.assert_called_once()


def test_with_a_bad_signature_the_release_is_not_built(tmp_path: Path) -> None:
    prepared, error, build = _prepare_signed(tmp_path, _signed([_RESOLVED], [_BAD_SIGNATURE]))
    assert prepared is None
    assert isinstance(error, ImageVerificationError)
    build.assert_not_called()


def test_a_verified_pull_says_so(tmp_path: Path) -> None:
    prepared, error, build = _prepare_signed(tmp_path, _signed([_RESOLVED]))
    assert error is None
    assert prepared == PreparedImage(tag="openhost-app:latest", source=ImageSource.PULLED_VERIFIED)
    build.assert_not_called()


def test_cosigns_verification_exit_codes_are_believed_whatever_the_registry_does(sleep: mock.Mock) -> None:
    """Exit 12 is cosign saying no signature matched: a verification failure even if the registry drops out after."""
    fake = _signed([_RESOLVED, _DIAL_FAILED], [_done(12, "Error: no matching signatures: unexpected EOF")])
    with pytest.raises(ImageVerificationError):
        _pull_signed(fake)
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "resolve",
    [
        subprocess.TimeoutExpired(cmd="cosign", timeout=120),
        _done(stdout="registry.cloudinabottle.org/bottled-vaultwarden:not-a-digest\n"),
    ],
)
def test_a_lookup_that_times_out_or_cant_be_read_is_retried(
    sleep: mock.Mock, resolve: subprocess.CompletedProcess[str] | Exception
) -> None:
    fake = _signed([resolve])
    with pytest.raises(ImageUnavailableError):
        _pull_signed(fake)
    assert fake.calls == [_RESOLVE_CMD, _RESOLVE_CMD, _RESOLVE_CMD]


def test_an_unreadable_recheck_after_a_failed_verify_is_unavailable(sleep: mock.Mock) -> None:
    fake = _signed([_RESOLVED, _done(stdout="garbage\n")], [_done(1, "Error: something went wrong")])
    with pytest.raises(ImageUnavailableError):
        _pull_signed(fake)
    assert sleep.call_count == 2


def test_a_failed_lookup_leaves_out_triangulates_deprecation_notice(sleep: mock.Mock) -> None:
    notice = 'Command "triangulate" is deprecated, triangulate will be removed in v4.0.0'
    fake = _signed([_done(1, f"{notice}\nError: GET ...: MANIFEST_UNKNOWN: manifest unknown")])
    with pytest.raises(ImageUnavailableError) as e:
        _pull_signed(fake)
    assert "MANIFEST_UNKNOWN" in str(e.value)
    assert "deprecated" not in str(e.value)
