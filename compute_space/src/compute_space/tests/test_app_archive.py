"""Tests for the ``app_archive`` storage tier.

The archive tier is a per-app bind mount whose host backing is
operator-selected: defaults to local disk under ``persistent_data_dir``
and can be overridden to point at a JuiceFS mount (or any other
host-mounted POSIX filesystem).  Apps see the same in-container path
either way.

These tests cover the manifest opt-in plumbing that flows from
``provision_data`` through ``deprovision_data``, plus the
``Config.app_archive_dir`` resolution rules.
"""

from __future__ import annotations

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.data import deprovision_data
from compute_space.core.data import provision_data
from compute_space.core.manifest import AppManifest


def _manifest(**kwargs) -> AppManifest:  # type: ignore[no-untyped-def]
    """Build a minimally-populated AppManifest for tests; mirrors the helper in test_containers.py so divergence is easier to spot."""
    base = dict(
        name="archiveapp",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=128,
        cpu_cores=0.1,
    )
    base.update(kwargs)
    return AppManifest(**base)  # type: ignore[arg-type]


def test_provision_data_creates_archive_subdir_when_opted_in(tmp_path) -> None:
    """``app_archive=True`` should produce a per-app subdir under the
    operator-configured archive root and stamp
    ``OPENHOST_APP_ARCHIVE_DIR`` on the env-vars dict.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    env = provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )

    expected = archive_dir / manifest.name
    assert expected.is_dir()
    assert env["OPENHOST_APP_ARCHIVE_DIR"] == str(expected)


def test_provision_data_skips_archive_when_not_opted_in(tmp_path) -> None:
    """An app that doesn't ask for ``app_archive`` must NOT get the
    env var or a subdir created — apps that don't opt in shouldn't
    surface operator-configured backings to themselves."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True)
    env = provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )

    assert "OPENHOST_APP_ARCHIVE_DIR" not in env
    assert not (archive_dir / manifest.name).exists()


def test_provision_data_archive_subdir_under_access_all_archive(tmp_path) -> None:
    """Under ``access_all_archive``, provision_data must still create the per-app subdir so the app's files have a stable location inside the archive namespace; the parent bind-mount in the container exposes that subdir at its existing path."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(access_all_archive=True)
    env = provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )

    assert (archive_dir / manifest.name).is_dir()
    assert env["OPENHOST_APP_ARCHIVE_DIR"] == str(archive_dir / manifest.name)


def test_provision_data_refuses_when_archive_dir_missing(tmp_path) -> None:
    """When the configured archive root doesn't exist (e.g. the S3 backend's JuiceFS mount has dropped), provisioning must fail loudly rather than silently creating a local-disk ghost path that JuiceFS shadows when the mount eventually attaches."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    with pytest.raises(RuntimeError, match="archive_dir"):
        provision_data(
            app_id="archiveapp-id",
            app_name=manifest.name,
            manifest=manifest,
            data_dir=str(data_dir),
            temp_data_dir=str(temp_dir),
            archive_dir=str(archive_dir),
            my_openhost_redirect_domain="my.example.com",
            zone_domain="example.com",
            port=8080,
            owner_username="owner",
        )


def test_provision_data_skips_archive_for_access_all_archive_when_archive_dir_missing(tmp_path) -> None:
    """``access_all_archive`` is permissive: provisioning must not fail when the archive backend is disabled or the mount has dropped, in contrast to ``app_archive = true`` which is a hard requirement."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(access_all_archive=True)
    env = provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )

    assert "OPENHOST_APP_ARCHIVE_DIR" not in env
    assert not archive_dir.exists()


def test_provision_data_does_not_fail_when_archive_dir_missing_and_no_archive_opt_in(
    tmp_path,
) -> None:
    """Apps that don't ask for app_archive must NOT fail just
    because the configured archive_dir doesn't exist.  The whole
    point of the opt-in is that operators with no JuiceFS (and
    therefore potentially with a fallback path that hasn't been
    populated by Config.make_all_dirs yet) can still deploy
    archive-free apps."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(app_data=True)
    provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )


def test_provision_data_archive_idempotent_on_redeploy(tmp_path) -> None:
    """Calling provision_data twice (the redeploy path) must succeed without complaining about the archive subdir already existing and must preserve data the first deploy wrote — same idempotency contract as app_data and app_temp_data."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )
    marker = archive_dir / manifest.name / "marker.txt"
    marker.write_text("hello")

    provision_data(
        app_id="archiveapp-id",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=str(data_dir),
        temp_data_dir=str(temp_dir),
        archive_dir=str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        owner_username="owner",
    )
    assert marker.read_text() == "hello"


def test_deprovision_data_removes_archive_subdir(tmp_path) -> None:
    """A non-keep_data uninstall must remove the app's archive subdirectory along with its app_data and temp dirs so a subsequent reinstall doesn't inherit stale archive contents."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    app_data_dir = data_dir / "app_data" / "myapp"
    app_temp_dir = temp_dir / "app_temp_data" / "myapp"
    app_archive_dir = archive_dir / "myapp"
    for d in (app_data_dir, app_temp_dir, app_archive_dir):
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("hi")

    deprovision_data("myapp", str(data_dir), str(temp_dir), str(archive_dir))

    assert not app_data_dir.exists()
    assert not app_temp_dir.exists()
    assert not app_archive_dir.exists()


def test_deprovision_data_handles_missing_archive_subdir(tmp_path) -> None:
    """A best-effort deprovision must not raise when the archive subdir doesn't exist (app never opted in, operator wiped manually); otherwise uninstall would be blocked for every non-archive app on a misconfigured-archive_dir instance."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    deprovision_data("myapp", str(data_dir), str(temp_dir), str(archive_dir))


def _config(**kwargs) -> DefaultConfig:  # type: ignore[no-untyped-def]
    base = dict(my_openhost_redirect_domain="my.example.com")
    base.update(kwargs)
    return DefaultConfig(**base)  # type: ignore[arg-type]


def test_config_archive_dir_lives_under_data_root() -> None:
    """``app_archive_dir`` is the JuiceFS mount point; under ``data_root_dir``
    (NOT ``persistent_data_dir``) so restic backups don't double-store bytes
    that already live in S3."""
    cfg = _config(data_root_dir="/opt/openhost")
    assert cfg.app_archive_dir == "/opt/openhost/app_archive"


def test_config_make_all_dirs_does_not_create_archive_dir(tmp_path) -> None:
    """make_all_dirs must NOT mkdir app_archive_dir: a stray local dir at that
    path would shadow the JuiceFS mount once attach_on_startup brings it up."""
    cfg = _config(data_root_dir=str(tmp_path))
    cfg.make_all_dirs()
    assert not (tmp_path / "app_archive").exists()
