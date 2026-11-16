"""Pulling an app's prebuilt image: an image that can't be fetched is retried and then built locally instead, while a
verification failure fails without retrying or building."""

from __future__ import annotations

import contextlib
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
from compute_space.core.manifest import ReleaseTagFormat
from compute_space.core.prebuilt_image import ImageToPull
from compute_space.core.prebuilt_image import ImageUnavailableError
from compute_space.core.prebuilt_image import ImageVerificationError
from compute_space.core.prebuilt_image import PrebuiltImageError
from compute_space.core.prebuilt_image import PulledImage
from compute_space.core.prebuilt_image import pull_prebuilt_image
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config

_SHA = "d961ca45c2995c6e80fb4f539b1fb7b163e07bdd"
_IMAGE = "registry.cloudinabottle.org/bottled-vaultwarden"
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


def _done(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


_UNREACHABLE = _done(125, "pinging container registry registry.cloudinabottle.org: dial tcp: connection refused")


@pytest.fixture
def sleep() -> Iterator[mock.Mock]:
    with mock.patch("compute_space.core.prebuilt_image.time.sleep") as s:
        yield s


def _pull(fake: _FakeRun, retry_delays: tuple[float, ...] = (0, 0)) -> PulledImage:
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        return pull_prebuilt_image(
            "vaultwarden", _IMAGE, _SHA, _REPO_URL, log=lambda _: None, retry_delays=retry_delays
        )


def test_pulls_the_commits_image_and_tags_it_as_the_app_image() -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done()]})
    assert _pull(fake) == PulledImage(tag="openhost-vaultwarden:latest", verified=False)
    assert fake.calls == [_PULL, _TAG]


@pytest.mark.parametrize(
    "failure",
    [
        _UNREACHABLE,
        _done(125, f"reading manifest {_SHA} in {_IMAGE}: manifest unknown"),
        _done(125, "received unexpected HTTP status: 503 Service Unavailable"),
        subprocess.TimeoutExpired(cmd="podman", timeout=1800),
    ],
)
def test_an_image_that_cant_be_fetched_is_retried_once_per_delay(
    failure: subprocess.CompletedProcess[str] | Exception,
) -> None:
    fake = _FakeRun({("podman", "pull"): [failure]})
    with pytest.raises(ImageUnavailableError):
        _pull(fake)
    assert fake.calls == [_PULL, _PULL, _PULL]


def test_retries_wait_the_given_delays(sleep: mock.Mock) -> None:
    with pytest.raises(ImageUnavailableError):
        _pull(_FakeRun({("podman", "pull"): [_UNREACHABLE]}), retry_delays=(5, 15))
    assert sleep.call_args_list == [mock.call(5), mock.call(15)]


def test_no_retry_delays_means_one_attempt() -> None:
    fake = _FakeRun({("podman", "pull"): [_UNREACHABLE]})
    with pytest.raises(ImageUnavailableError):
        _pull(fake, retry_delays=())
    assert fake.calls == [_PULL]


def test_a_pull_that_succeeds_on_the_second_attempt_is_used() -> None:
    fake = _FakeRun({("podman", "pull"): [_UNREACHABLE, _done()], ("podman", "tag"): [_done()]})
    assert _pull(fake).tag == "openhost-vaultwarden:latest"
    assert fake.calls == [_PULL, _PULL, _TAG]


def test_the_instances_signature_policy_rejecting_the_image_is_a_verification_failure() -> None:
    fake = _FakeRun({("podman", "pull"): [_done(125, "Source image rejected: A signature was required")]})
    with pytest.raises(ImageVerificationError, match="Source image rejected"):
        _pull(fake)
    assert fake.calls == [_PULL]


def test_a_verification_failure_in_the_hook_is_not_retried() -> None:
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


def test_the_hook_choosing_what_to_pull_is_retried_with_the_pull() -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done()]})
    to_pull = ImageToPull(ref=f"{_IMAGE}:{_SHA}", verified=True)
    with mock.patch(
        "compute_space.core.prebuilt_image._image_to_pull",
        side_effect=[ImageUnavailableError("registry unreachable"), to_pull],
    ) as hook:
        assert _pull(fake) == PulledImage(tag="openhost-vaultwarden:latest", verified=True)
    assert hook.call_count == 2


def test_a_failed_retag_fails_without_retrying() -> None:
    fake = _FakeRun({("podman", "pull"): [_done()], ("podman", "tag"): [_done(125, "no space left on device")]})
    with pytest.raises(PrebuiltImageError, match="no space left") as e:
        _pull(fake)
    assert not isinstance(e.value, ImageUnavailableError)
    assert fake.calls == [_PULL, _TAG]


def test_requires_a_full_commit_sha() -> None:
    with pytest.raises(PrebuiltImageError, match="full commit sha"):
        pull_prebuilt_image("vaultwarden", _IMAGE, "main", _REPO_URL, log=lambda _: None, retry_delays=())


def _manifest(prebuilt_image: str | None = _IMAGE) -> mock.Mock:
    return mock.Mock(
        release_tag_format=ReleaseTagFormat.SEMVER,
        prebuilt_image=prebuilt_image,
        container_image="Dockerfile",
        effective_build_memory_mb=512,
    )


def _prepare(
    tmp_path: Path,
    repo_url: str,
    log: Callable[[str], None] = lambda _: None,
    prebuilt_image: str | None = _IMAGE,
) -> PreparedImage:
    with mock.patch.object(apps_mod, "head_commit", return_value=_SHA):
        return prepare_app_image("app", str(tmp_path), _manifest(prebuilt_image), repo_url, str(tmp_path), log=log)


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


def test_a_release_without_prebuilt_image_is_built(tmp_path: Path) -> None:
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
    ):
        assert _prepare(tmp_path, _REPO_URL, prebuilt_image=None).source is ImageSource.BUILT
    pull.assert_not_called()
    build.assert_called_once()


_INSTALL_MANIFEST = f"""\
[app]
name = "vaultwarden"
version = "1.0.0"
release_tag_format = "semver"

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


def test_a_prepared_image_is_run_without_pulling_or_building(installed: tuple[Any, Path]) -> None:
    """An update hands the reload the image it produced before stopping the app, which is run as is."""
    cfg, _ = installed
    prepared = PreparedImage(tag="openhost-vaultwarden:latest", source=ImageSource.FALLBACK_BUILT)
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        with (
            mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
            mock.patch.object(apps_mod, "build_image") as build,
            mock.patch.object(apps_mod, "run_app_image") as run,
        ):
            start_app_process("vw-id", db, cfg, prepared_image=prepared)
    finally:
        db.close()
    pull.assert_not_called()
    build.assert_not_called()
    assert run.call_args.args[:2] == ("vw-id", "openhost-vaultwarden:latest")
