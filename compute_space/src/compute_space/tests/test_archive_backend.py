"""Unit tests for ``compute_space.core.archive_backend``."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from compute_space.core import apps as apps_mod
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import configure_backend
from compute_space.core.archive_backend import juicefs_mount_dir
from compute_space.core.archive_backend import read_state
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20300)


@pytest.fixture
def db(cfg):
    """Initialised sqlite DB so the archive_backend table exists, seeded local."""
    init_db(cfg.db_path)
    conn = sqlite3.connect(cfg.db_path)
    yield conn
    conn.close()


# --- read_state ------------------------------------------------------------


def test_seeded_state_is_local(db):
    """Fresh DB comes up at backend='local': the archive tier is always
    available (a local file-backed JuiceFS volume); apps with app_archive=true
    install immediately, and the operator can upgrade to S3 later."""
    state = read_state(db)
    assert state.backend == "local"
    assert state.s3_bucket is None


# --- _bucket_url -----------------------------------------------------------


def test_bucket_url_aws_default():
    """AWS bucket URL: virtual-host style with region-suffixed endpoint.
    Per-zone isolation under a shared bucket goes via the JuiceFS volume
    name, not the URL — JuiceFS treats the first path component as the
    bucket name, so any extra path here would break the lookup."""
    assert archive_backend._bucket_url("mybucket", "us-west-2", None) == "https://mybucket.s3.us-west-2.amazonaws.com"


def test_bucket_url_aws_default_region_fallback():
    """Empty region falls back to us-east-1 (matches JuiceFS default)."""
    assert archive_backend._bucket_url("mybucket", "", None) == "https://mybucket.s3.us-east-1.amazonaws.com"


def test_bucket_url_with_custom_endpoint():
    """Non-AWS endpoint uses path-style: <endpoint>/<bucket>."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000")
        == "https://minio.example.com:9000/mybucket"
    )


def test_bucket_url_endpoint_strips_trailing_slash():
    """Operator-supplied trailing slash is normalised so we don't end up
    with ``//mybucket`` (which JuiceFS would parse as bucket-name='')."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000/")
        == "https://minio.example.com:9000/mybucket"
    )


# --- sync URL construction -------------------------------------------------


def test_s3_sync_dest_aws():
    """AWS sync DST is virtual-host style with the volume prefix appended so
    object keys line up with the source's ``<volume>/`` prefix."""
    assert (
        archive_backend._s3_sync_dest("mybucket", "us-west-2", None, "openhost")
        == "s3://mybucket.s3.us-west-2.amazonaws.com/openhost/"
    )


def test_s3_sync_dest_custom_endpoint_uses_bucket_dot_host():
    """juicefs sync parses ``s3://BUCKET.ENDPOINT/PREFIX``; a custom endpoint
    must be rendered as ``<bucket>.<endpoint-host>`` (NOT ``endpoint/bucket``),
    otherwise juicefs treats the whole host as the endpoint and fails to auth."""
    assert (
        archive_backend._s3_sync_dest("mybucket", None, "http://127.0.0.1:9199", "openhost")
        == "s3://mybucket.127.0.0.1:9199/openhost/"
    )


def test_local_sync_source_points_under_volume_prefix(cfg):
    """The local SRC is ``<object-store>/<volume>/`` so keys map 1:1 to DST."""
    src = archive_backend._local_sync_source(cfg, "openhost")
    assert src.endswith("/openhost/")
    assert src.startswith(cfg.local_archive_object_store_dir)


def test_sync_objects_passes_aws_env_not_argv(cfg):
    """Credentials must go via AWS_* env (juicefs sync ignores JuiceFS's own
    ACCESS_KEY/SECRET_KEY for endpoints), and must NOT appear in argv (ps leak)."""
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["env"] = env
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend._sync_objects(
                cfg,
                src="/store/openhost/",
                dst="s3://b.s3.us-east-1.amazonaws.com/openhost/",
                s3_access_key_id="AKIA-secret",
                s3_secret_access_key="topsecret",
            )
    assert captured["env"]["AWS_ACCESS_KEY_ID"] == "AKIA-secret"
    assert captured["env"]["AWS_SECRET_ACCESS_KEY"] == "topsecret"
    assert "--check-all" in captured["cmd"]
    # No secret in argv.
    assert not any("topsecret" in str(a) for a in captured["cmd"])
    assert "--no-agent" in captured["cmd"]
    assert captured["cmd"].index("--no-agent") < captured["cmd"].index("sync")
    # HTTPS (default) endpoint -> no --no-https.
    assert "--no-https" not in captured["cmd"]


def test_sync_objects_adds_no_https_for_insecure_endpoint(cfg):
    """A plain-HTTP endpoint (e.g. same-host MinIO) needs --no-https or juicefs
    sync forces HTTPS and fails the handshake."""
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend._sync_objects(
                cfg,
                src="/store/openhost/",
                dst="s3://b.localhost:9106/openhost/",
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
                insecure=True,
            )
    assert "--no-https" in captured["cmd"]


def test_endpoint_is_insecure_http():
    assert archive_backend._endpoint_is_insecure_http("http://localhost:9106") is True
    assert archive_backend._endpoint_is_insecure_http("HTTP://minio.local") is True
    assert archive_backend._endpoint_is_insecure_http("https://minio.example.com") is False
    assert archive_backend._endpoint_is_insecure_http(None) is False
    assert archive_backend._endpoint_is_insecure_http("") is False


def test_reconfigure_volume_storage_passes_literal_secret(cfg):
    """``juicefs config`` re-point must pass the LITERAL secret to
    ``--secret-key``.  The ``env:VAR`` indirection is NOT resolved by
    ``config`` (verified on a live instance: it stored the literal
    "env:SECRET_KEY" and every upload then failed with SignatureDoesNotMatch),
    so we accept the brief argv exposure on this single-tenant host."""
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend._reconfigure_volume_storage(
                cfg,
                storage="s3",
                bucket="https://b.s3.us-east-1.amazonaws.com",
                s3_access_key_id="AKIA-x",
                s3_secret_access_key="topsecret",
            )
    assert "config" in captured["cmd"]
    assert "--storage" in captured["cmd"] and "s3" in captured["cmd"]
    # The real secret is passed literally (env:SECRET_KEY would be stored raw).
    ski = captured["cmd"].index("--secret-key") + 1
    assert captured["cmd"][ski] == "topsecret"
    assert "env:SECRET_KEY" not in captured["cmd"]


# --- format: --no-agent + backends ------------------------------------------


def test_format_s3_volume_passes_no_agent_flag(cfg):
    """``--no-agent`` must precede the format subcommand so JuiceFS doesn't
    bind 6060+ for its pprof debug HTTP server (which our security audit
    flags as an unexpected listener)."""
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend.format_s3_volume(
                cfg,
                s3_bucket="b",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
                juicefs_volume_name="zone",
            )
    cmd = captured["cmd"]
    assert "--no-agent" in cmd
    assert cmd.index("--no-agent") < cmd.index("format")
    assert "s3" in cmd


def test_format_local_volume_uses_file_storage_and_creates_store(cfg):
    """The local backend formats with ``--storage file`` and creates both the
    meta state dir and the object-store dir."""
    captured = {}
    store_dir = archive_backend.local_object_store_dir(cfg)
    state_dir = archive_backend.juicefs_state_dir(cfg)
    assert not os.path.exists(store_dir)

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["store_exists"] = os.path.isdir(store_dir)
        captured["state_exists"] = os.path.isdir(state_dir)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                archive_backend.format_local_volume(cfg, "openhost")
    cmd = captured["cmd"]
    assert "--storage" in cmd and "file" in cmd
    assert "--no-agent" in cmd and cmd.index("--no-agent") < cmd.index("format")
    # bucket is the object store dir, trailing slash.
    bucket_idx = cmd.index("--bucket") + 1
    assert cmd[bucket_idx].endswith("/")
    assert cmd[bucket_idx].startswith(store_dir)
    assert captured["store_exists"] is True
    assert captured["state_exists"] is True


def test_format_s3_volume_creates_state_dir_for_meta_db(cfg):
    """JuiceFS's sqlite3 meta backend won't mkdir its parent; format must."""
    state_dir = archive_backend.juicefs_state_dir(cfg)
    assert not os.path.exists(state_dir)
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["state_dir_exists"] = os.path.isdir(state_dir)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend.format_s3_volume(
                cfg,
                s3_bucket="b",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
                juicefs_volume_name="zone",
            )
    assert captured["state_dir_exists"] is True


# --- mount -----------------------------------------------------------------


def test_mount_writes_env_file_with_s3_creds_and_starts_service(cfg):
    """mount() must write the env file with the correct binary, meta DSN,
    mount dir, and (for s3) S3 creds, then enable+start the systemd service."""
    systemctl_calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, timeout=30):
        systemctl_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    is_mounted_calls = [False, True]
    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(
            archive_backend, "is_mounted", side_effect=lambda mp: is_mounted_calls.pop(0) if is_mounted_calls else True
        ):
            with mock.patch.object(archive_backend, "_systemctl", side_effect=fake_systemctl):
                archive_backend.mount(cfg, "ak", "sk")

    env_path = archive_backend._juicefs_env_file(cfg)
    content = open(env_path).read()
    assert "JUICEFS_BINARY=/usr/local/bin/juicefs" in content
    assert "ACCESS_KEY=ak" in content
    assert "SECRET_KEY=sk" in content
    assert f"JUICEFS_MOUNT_DIR={cfg.app_archive_dir}" in content
    assert oct(os.stat(env_path).st_mode & 0o777) == "0o600"
    assert ("daemon-reload",) in systemctl_calls
    assert ("enable", "--now", archive_backend.JUICEFS_SERVICE) in systemctl_calls


def test_mount_local_omits_s3_creds(cfg):
    """On the local (file) backend, mount() writes NO S3 creds into the env
    file — the file backend doesn't need them, and there are none to write."""
    is_mounted_calls = [False, True]
    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(
            archive_backend, "is_mounted", side_effect=lambda mp: is_mounted_calls.pop(0) if is_mounted_calls else True
        ):
            with mock.patch.object(archive_backend, "_systemctl"):
                archive_backend.mount(cfg)  # no creds -> local
    content = open(archive_backend._juicefs_env_file(cfg)).read()
    assert "ACCESS_KEY=" not in content
    assert "SECRET_KEY=" not in content
    assert "JUICEFS_MOUNT_DIR=" in content


def test_mount_idempotent_when_already_mounted(cfg):
    """mount() must be a no-op if the mount is already active."""
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        with mock.patch.object(archive_backend, "_systemctl") as sctl:
            archive_backend.mount(cfg, "ak", "sk")
    sctl.assert_not_called()


# --- on-disk layout helpers ----------------------------------------------


def test_juicefs_state_and_runtime_dirs_are_separate_and_under_data_path(cfg):
    state = archive_backend._juicefs_state_dir(cfg)
    runtime = archive_backend._juicefs_runtime_dir(cfg)
    base = str(cfg.openhost_data_path)
    assert state.startswith(base)
    assert runtime.startswith(base)
    assert state != runtime


def test_juicefs_meta_db_lives_in_state_dir(cfg):
    assert archive_backend._juicefs_meta_db(cfg).startswith(archive_backend._juicefs_state_dir(cfg))
    assert archive_backend._juicefs_meta_db(cfg).endswith("meta.db")


def test_juicefs_meta_db_path_is_public_alias(cfg):
    assert archive_backend.juicefs_meta_db_path(cfg) == archive_backend._juicefs_meta_db(cfg)


def test_local_object_store_under_persistent_data(cfg):
    """The local object store lives under persistent_data (so it is backed
    up) and is a DIFFERENT path from the mountpoint under data_root."""
    assert archive_backend.local_object_store_dir(cfg).startswith(cfg.persistent_data_dir)
    assert archive_backend.local_object_store_dir(cfg) != juicefs_mount_dir(cfg)


# --- list_meta_dumps -------------------------------------------------------


def test_list_meta_dumps_summarises_dump_objects():
    fake_now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(
            list_objects_v2=mock.MagicMock(
                return_value={
                    "Contents": [
                        {"Key": "p/meta/dump-1.json.gz", "LastModified": fake_now - dt.timedelta(hours=2)},
                        {"Key": "p/meta/dump-2.json.gz", "LastModified": fake_now},
                        {"Key": "p/meta/something-else.txt", "LastModified": fake_now},
                    ]
                }
            )
        ),
    ):
        summary = archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p")
    assert summary is not None
    assert summary.count == 2
    assert summary.latest_key == "p/meta/dump-2.json.gz"


def test_list_meta_dumps_empty_bucket_returns_zero_count():
    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=mock.MagicMock(return_value={"Contents": []})),
    ):
        summary = archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p")
    assert summary is not None
    assert summary.count == 0
    assert summary.latest_at is None


def test_list_meta_dumps_returns_none_on_s3_failure():
    with mock.patch.object(archive_backend, "_s3_client", side_effect=RuntimeError("boom")):
        assert archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p") is None


def test_list_meta_dumps_lists_under_volume_name():
    captured = {}

    def fake_list(*, Bucket, Prefix, MaxKeys):
        captured["prefix"] = Prefix
        return {"Contents": []}

    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=fake_list),
    ):
        archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "openhost")
    assert captured["prefix"] == "openhost/meta/"


def test_list_meta_dumps_handles_empty_volume_name():
    captured = {}

    def fake_list(*, Bucket, Prefix, MaxKeys):
        captured["prefix"] = Prefix
        return {"Contents": []}

    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=fake_list),
    ):
        archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "")
    assert captured["prefix"] == "meta/"


# --- is_archive_dir_healthy -----------------------------------------------


def test_is_archive_dir_healthy_local_uses_is_mounted(cfg, db):
    """On the local backend the archive is a JuiceFS mount, so health is
    determined by is_mounted(mountpoint) — NOT os.path.isdir."""
    with mock.patch.object(archive_backend, "is_mounted", return_value=False):
        assert not archive_backend.is_archive_dir_healthy(cfg, db)
    with mock.patch.object(archive_backend, "is_mounted", return_value=True) as m:
        assert archive_backend.is_archive_dir_healthy(cfg, db)
    assert m.call_args.args[0] == juicefs_mount_dir(cfg)


def test_is_archive_dir_healthy_disabled_returns_true(cfg, db):
    """The legacy 'disabled' state (no archive tier) always reports healthy
    so it never blocks app operations."""
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    assert archive_backend.is_archive_dir_healthy(cfg, db)


def test_is_archive_dir_healthy_s3_uses_is_mounted(cfg, db):
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=True) as m:
        assert archive_backend.is_archive_dir_healthy(cfg, db)
    assert m.call_args.args[0] == juicefs_mount_dir(cfg)


# --- attach_on_startup ----------------------------------------------------


def test_attach_on_startup_disabled_is_no_op(cfg, db):
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "install_juicefs") as inst:
        with mock.patch.object(archive_backend, "mount") as mnt:
            archive_backend.attach_on_startup(cfg, db)
    inst.assert_not_called()
    mnt.assert_not_called()


def test_attach_on_startup_local_formats_and_mounts(cfg, db):
    """Fresh DB defaults to local: attach must format the local file volume
    (first boot) and start the mount, clearing state_message."""
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_local_volume_formatted", return_value=False),
        mock.patch.object(archive_backend, "format_local_volume") as fmt,
        mock.patch.object(archive_backend, "mount") as mnt,
    ):
        archive_backend.attach_on_startup(cfg, db)
    fmt.assert_called_once()
    mnt.assert_called_once()
    assert read_state(db).state_message is None


def test_attach_on_startup_local_skips_format_when_already_formatted(cfg, db):
    """If the local volume was already formatted (meta.db present), attach
    just mounts — it must not re-format."""
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_local_volume_formatted", return_value=True),
        mock.patch.object(archive_backend, "format_local_volume") as fmt,
        mock.patch.object(archive_backend, "mount") as mnt,
    ):
        archive_backend.attach_on_startup(cfg, db)
    fmt.assert_not_called()
    mnt.assert_called_once()


def test_attach_on_startup_local_selfheals_unformatted_metadb(cfg, db):
    """Regression: an existing-but-UNFORMATTED meta.db (created by the
    restart-looping mount unit before the first format) must NOT be treated as
    formatted.  On such a legacy-'openhost' zone attach must rename to a unique
    per-zone volume AND format it, rather than skipping format and leaving the
    mount to fatal-loop forever.

    Here ``_local_volume_formatted`` is NOT mocked; instead an empty meta.db
    file is placed on disk so the real (fixed) check runs against it.
    """
    os.makedirs(archive_backend.juicefs_state_dir(cfg), exist_ok=True)
    # Empty file == created but never formatted (no jfs_setting 'format' row).
    open(archive_backend.juicefs_meta_db_path(cfg), "wb").close()
    # Legacy shared default volume name, as a pre-per-zone-name zone would carry.
    db.execute("UPDATE archive_backend SET juicefs_volume_name='openhost' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "format_local_volume") as fmt,
        mock.patch.object(archive_backend, "mount") as mnt,
    ):
        archive_backend.attach_on_startup(cfg, db)
    # Must (re)format because the DB was never actually formatted.
    fmt.assert_called_once()
    mnt.assert_called_once()
    state = read_state(db)
    assert state.state_message is None
    # And the legacy 'openhost' name must have been replaced with a unique one.
    assert state.juicefs_volume_name != "openhost"
    assert state.juicefs_volume_name.startswith("oh-")
    # format was invoked with that unique name.
    assert fmt.call_args[0][1] == state.juicefs_volume_name


def test_attach_on_startup_local_records_error_on_failure(cfg, db):
    """A failure bringing up the local mount is recorded in state_message and
    doesn't crash boot."""
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_local_volume_formatted", return_value=True),
        mock.patch.object(archive_backend, "mount", side_effect=RuntimeError("mount boom")),
    ):
        archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state_message is not None
    assert "boom" in state.state_message


def test_attach_on_startup_s3_happy_path(cfg, db):
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
        "s3_access_key_id='ak', s3_secret_access_key='sk' WHERE id=1"
    )
    db.commit()
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "mount") as mnt:
            archive_backend.attach_on_startup(cfg, db)
    mnt.assert_called_once()
    assert read_state(db).state_message is None


def test_attach_on_startup_s3_missing_creds_records_error(cfg, db):
    db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b' WHERE id=1")
    db.commit()
    archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state_message is not None
    assert "credentials" in state.state_message.lower()


# --- effective_archive_dir -------------------------------------------------


def test_effective_archive_dir_is_always_the_mount(cfg, db):
    """The archive tier is always the JuiceFS mountpoint regardless of backend."""
    assert archive_backend.effective_archive_dir(cfg, db) == juicefs_mount_dir(cfg)
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    assert archive_backend.effective_archive_dir(cfg, db) == juicefs_mount_dir(cfg)
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    assert archive_backend.effective_archive_dir(cfg, db) == juicefs_mount_dir(cfg)


# --- local_archive_apps_with_data ------------------------------------------


def test_local_archive_apps_with_data_reads_mount(cfg, db):
    """Lists per-app subdirs of the (mounted) archive that contain data.
    Empty per-app dirs don't count; a live mount is required."""
    mount_dir = juicefs_mount_dir(cfg)
    os.makedirs(os.path.join(mount_dir, "emptyapp"), exist_ok=True)
    os.makedirs(os.path.join(mount_dir, "nextcloud", "files"), exist_ok=True)
    with open(os.path.join(mount_dir, "nextcloud", "files", "a.txt"), "wb") as f:
        f.write(b"hello")

    # Not mounted -> nothing (defensive).
    with mock.patch.object(archive_backend, "is_mounted", return_value=False):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []
    # Mounted -> only apps with content.
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == ["nextcloud"]

    # Wrong backend -> empty regardless.
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []


# --- configure_backend -----------------------------------------------------


def test_configure_backend_s3_to_s3_refuses_without_source_creds(cfg, db):
    """An s3 zone missing its stored bucket/creds can't be read for a copy, so
    an s3->s3 migration is refused up front rather than losing data."""
    db.execute("UPDATE archive_backend SET backend='s3', s3_bucket=NULL WHERE id=1")
    db.commit()
    with pytest.raises(BackendConfigureError, match="missing its bucket/credentials"):
        configure_backend(
            cfg,
            db,
            s3_bucket="newbucket",
            s3_region=None,
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )


def test_configure_backend_s3_to_s3_syncs_repoints_and_reclaims(cfg, db):
    """Migrating s3->s3 remounts the source, syncs old->new, re-points+remounts,
    flips the DB to the new bucket, and reclaims the OLD bucket's prefix."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='oldbucket', s3_region='us-west-2', "
        "s3_endpoint=NULL, s3_access_key_id='oldak', s3_secret_access_key='oldsk', "
        "s3_prefix='oh-zone-1234', juicefs_volume_name='oh-zone-1234' WHERE id=1"
    )
    db.commit()
    calls = []
    captured = {}

    def fake_migrate_s3(config, **kwargs):
        calls.append("migrate")
        captured.update(kwargs)

    def fake_reclaim(**kwargs):
        calls.append("reclaim")
        captured["reclaim"] = kwargs

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "mount", side_effect=lambda *a, **k: calls.append("mount")),
        mock.patch.object(archive_backend, "umount", side_effect=lambda *a, **k: calls.append("umount")),
        mock.patch.object(archive_backend, "_migrate_s3_to_s3", side_effect=fake_migrate_s3),
        mock.patch.object(archive_backend, "_migrate_local_to_s3") as local_mig,
        mock.patch.object(archive_backend, "_remove_s3_object_prefix", side_effect=fake_reclaim),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="newbucket",
            s3_region="us-east-1",
            s3_endpoint="http://minio.local:9000",
            s3_prefix=None,
            s3_access_key_id="newak",
            s3_secret_access_key="newsk",
        )
    local_mig.assert_not_called()
    # mount source, migrate (sync+repoint), remount (umount+mount), reclaim old.
    assert calls == ["mount", "migrate", "umount", "mount", "reclaim"]
    # The migration read from the OLD store and wrote to the NEW store.
    assert captured["src_bucket"] == "oldbucket"
    assert captured["src_access_key_id"] == "oldak"
    assert captured["dst_bucket"] == "newbucket"
    assert captured["dst_access_key_id"] == "newak"
    # Volume prefix preserved across the migration.
    assert captured["volume"] == "oh-zone-1234"
    # Reclaim targets the OLD bucket under the volume prefix only.
    assert captured["reclaim"]["s3_bucket"] == "oldbucket"
    assert captured["reclaim"]["volume"] == "oh-zone-1234"
    state = read_state(db)
    assert state.backend == "s3"
    assert state.s3_bucket == "newbucket"
    assert state.s3_endpoint == "http://minio.local:9000"
    assert state.s3_access_key_id == "newak"
    assert state.juicefs_volume_name == "oh-zone-1234"
    assert state.s3_prefix == "oh-zone-1234"


def test_configure_backend_s3_to_s3_preserves_legacy_openhost_volume(cfg, db):
    """A legacy zone whose s3 volume name is the shared default 'openhost' MUST
    keep it during an s3->s3 migration: the source objects live under
    <old-bucket>/openhost/ and both sync source + dest use this one volume, so
    renaming would read from an empty prefix and copy nothing.  An operator
    prefix on the request must be ignored here."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='oldbucket', s3_region='us-west-2', "
        "s3_endpoint=NULL, s3_access_key_id='oldak', s3_secret_access_key='oldsk', "
        "s3_prefix='openhost', juicefs_volume_name='openhost' WHERE id=1"
    )
    db.commit()
    captured = {}

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_s3_to_s3", side_effect=lambda config, **k: captured.update(k)),
        mock.patch.object(
            archive_backend, "_remove_s3_object_prefix", side_effect=lambda **k: captured.update(reclaim=k)
        ),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="newbucket",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix="operator-tried-to-rename",  # must be ignored
            s3_access_key_id="newak",
            s3_secret_access_key="newsk",
        )
    # Source AND dest prefixes both stay 'openhost' so the sync reads real data.
    assert captured["volume"] == "openhost"
    assert captured["reclaim"]["volume"] == "openhost"
    state = read_state(db)
    assert state.juicefs_volume_name == "openhost"
    assert state.s3_prefix == "openhost"


def test_configure_backend_s3_to_s3_same_location_skips_reclaim(cfg, db):
    """A no-op 'migration' to the SAME bucket/endpoint/region must NOT reclaim
    the prefix (that would delete the data we kept in place)."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_region='us-east-1', "
        "s3_endpoint=NULL, s3_access_key_id='ak', s3_secret_access_key='sk', "
        "s3_prefix='oh-zone-1234', juicefs_volume_name='oh-zone-1234' WHERE id=1"
    )
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_s3_to_s3"),
        mock.patch.object(archive_backend, "_remove_s3_object_prefix") as reclaim,
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak2",
            s3_secret_access_key="sk2",
        )
    reclaim.assert_not_called()
    state = read_state(db)
    assert state.backend == "s3"
    # Credentials still updated even though location is unchanged.
    assert state.s3_access_key_id == "ak2"


def test_configure_backend_s3_to_s3_failopen_restores_old_bucket(cfg, db):
    """Fail-open: if the s3->s3 migration fails, the DB stays on the OLD bucket
    and the volume is re-pointed back to it + remounted; nothing is reclaimed."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='oldbucket', s3_region='us-west-2', "
        "s3_endpoint=NULL, s3_access_key_id='oldak', s3_secret_access_key='oldsk', "
        "s3_prefix='oh-zone-1234', juicefs_volume_name='oh-zone-1234' WHERE id=1"
    )
    db.commit()
    restored = {}

    def fake_reconfig(config, *, storage, bucket, s3_access_key_id, s3_secret_access_key):
        restored["storage"] = storage
        restored["bucket"] = bucket
        restored["ak"] = s3_access_key_id

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_s3_to_s3", side_effect=RuntimeError("sync exploded")),
        mock.patch.object(archive_backend, "_reconfigure_volume_storage", side_effect=fake_reconfig),
        mock.patch.object(archive_backend, "_remove_s3_object_prefix") as reclaim,
    ):
        with pytest.raises(BackendConfigureError):
            configure_backend(
                cfg,
                db,
                s3_bucket="newbucket",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_prefix=None,
                s3_access_key_id="newak",
                s3_secret_access_key="newsk",
            )
    state = read_state(db)
    # DB unchanged: still the OLD bucket + creds.
    assert state.backend == "s3"
    assert state.s3_bucket == "oldbucket"
    assert state.s3_access_key_id == "oldak"
    # Volume re-pointed back at the OLD bucket with OLD creds.
    assert restored.get("storage") == "s3"
    assert "oldbucket" in restored.get("bucket", "")
    assert restored.get("ak") == "oldak"
    reclaim.assert_not_called()


def test_migrate_s3_to_s3_syncs_then_repoints(cfg):
    """The s3->s3 helper syncs objects first, THEN re-points storage."""
    order = []
    with (
        mock.patch.object(archive_backend, "_sync_objects_s3_to_s3", side_effect=lambda *a, **k: order.append("sync")),
        mock.patch.object(
            archive_backend, "_reconfigure_volume_storage", side_effect=lambda *a, **k: order.append("repoint")
        ),
    ):
        archive_backend._migrate_s3_to_s3(
            cfg,
            volume="oh-zone-1234",
            src_bucket="oldbucket",
            src_region="us-west-2",
            src_endpoint=None,
            src_access_key_id="oldak",
            src_secret_access_key="oldsk",
            dst_bucket="newbucket",
            dst_region="us-east-1",
            dst_endpoint=None,
            dst_access_key_id="newak",
            dst_secret_access_key="newsk",
        )
    assert order == ["sync", "repoint"]


def test_s3_url_with_creds_percent_encodes(cfg):
    """Credentials with URL-special characters must be percent-encoded so they
    can't corrupt the s3://AK:SK@host structure."""
    url = archive_backend._s3_url_with_creds(
        "s3://bucket.s3.us-east-1.amazonaws.com/vol/",
        "AKIA/EXAMPLE+KEY",
        "secret/with+special:chars@here",
    )
    assert url.startswith("s3://")
    # The raw special chars must NOT appear unescaped in the creds section.
    creds = url[len("s3://") : url.index("@")]
    assert "/" not in creds
    assert "@" not in creds
    # Host/prefix preserved after the '@'.
    assert url.endswith("@bucket.s3.us-east-1.amazonaws.com/vol/")


def test_configure_backend_from_disabled_formats_s3_fresh(cfg, db):
    """A legacy 'disabled' zone has no volume/data, so configure formats S3
    fresh (no sync/re-point) and flips to s3."""
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "format_s3_volume") as fmt,
        mock.patch.object(archive_backend, "mount") as mnt,
        mock.patch.object(archive_backend, "_migrate_local_to_s3") as mig,
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="mybucket",
            s3_region="us-west-2",
            s3_endpoint=None,
            s3_prefix="andrew-3",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    fmt.assert_called_once()
    mnt.assert_called_once()
    mig.assert_not_called()
    state = read_state(db)
    assert state.backend == "s3"
    assert state.s3_bucket == "mybucket"
    assert state.s3_prefix == "andrew-3"
    assert state.juicefs_volume_name == "andrew-3"
    assert state.configured_at is not None


def test_configure_backend_from_local_syncs_and_repoints(cfg, db):
    """Migrating local->s3 syncs objects, re-points the volume, remounts, and
    reclaims the local object store."""
    # Unique per-zone volume name (as a fresh zone would have) is PRESERVED — its
    # objects already live under that prefix.
    db.execute("UPDATE archive_backend SET juicefs_volume_name='oh-testzone-local-abcd1234' WHERE id=1")
    db.commit()
    calls = []
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount", side_effect=lambda *a, **k: calls.append("mount")),
        mock.patch.object(archive_backend, "umount", side_effect=lambda *a, **k: calls.append("umount")),
        mock.patch.object(
            archive_backend, "_migrate_local_to_s3", side_effect=lambda *a, **k: calls.append("migrate")
        ),
        mock.patch.object(
            archive_backend, "_remove_local_object_store", side_effect=lambda *a, **k: calls.append("remove")
        ),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix="ignored-prefix",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    state = read_state(db)
    assert state.backend == "s3"
    # The existing unique volume name wins (its objects are already keyed under
    # it); the operator prefix does not silently rename an existing volume.
    assert state.juicefs_volume_name == "oh-testzone-local-abcd1234"
    # s3_prefix in the row reflects the ACTUAL object prefix (the volume name),
    # not the ignored operator input, so reported state matches reality.
    assert state.s3_prefix == "oh-testzone-local-abcd1234"
    # Order: mount local, migrate (sync+repoint), remount (umount+mount), remove.
    assert calls == ["mount", "migrate", "umount", "mount", "remove"]


def test_legacy_openhost_local_zone_honors_prefix_on_migration(cfg, db):
    """A legacy zone still on the shared 'openhost' volume MUST NOT migrate into
    a shared bucket under 'openhost' (that collides with other zones). When the
    operator supplies an s3_prefix it is used as the volume name so the migrated
    objects are isolated; the stored s3_prefix reflects that actual prefix."""
    db.execute("UPDATE archive_backend SET juicefs_volume_name='openhost' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_local_to_s3"),
        mock.patch.object(archive_backend, "_remove_local_object_store"),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix="alice-zone",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    state = read_state(db)
    assert state.backend == "s3"
    assert state.juicefs_volume_name == "alice-zone"
    assert state.s3_prefix == "alice-zone"


def test_legacy_openhost_local_zone_without_prefix_gets_unique_name(cfg, db):
    """A legacy 'openhost' zone migrating with NO explicit prefix falls back to a
    unique per-zone volume name (never the shared 'openhost') so it can't collide
    in a shared bucket."""
    db.execute("UPDATE archive_backend SET juicefs_volume_name='openhost' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_local_to_s3"),
        mock.patch.object(archive_backend, "_remove_local_object_store"),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    state = read_state(db)
    assert state.backend == "s3"
    assert state.juicefs_volume_name != "openhost"
    assert state.juicefs_volume_name.startswith("oh-")
    assert state.s3_prefix == state.juicefs_volume_name


def test_configure_backend_quiesces_apps_before_sync(cfg, db):
    """The quiesce callback (which stops archive apps) must run BEFORE the sync
    (so no app can write into the local store once copying starts — that write
    would be lost when the volume re-points to S3) and therefore also before
    the remount (so nothing holds the FUSE mount open when it is unmounted)."""
    order = []
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount", side_effect=lambda *a, **k: order.append("mount")),
        mock.patch.object(archive_backend, "umount", side_effect=lambda *a, **k: order.append("umount")),
        mock.patch.object(
            archive_backend, "_migrate_local_to_s3", side_effect=lambda *a, **k: order.append("migrate")
        ),
        mock.patch.object(archive_backend, "_remove_local_object_store"),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
            quiesce_archive_apps=lambda: order.append("quiesce"),
        )
    # quiesce (stop apps for a consistent snapshot + free the mount) ->
    # migrate (sync+repoint) -> remount (umount+mount)
    assert order == ["mount", "quiesce", "migrate", "umount", "mount"]


def test_configure_backend_migration_failopen_restores_local(cfg, db):
    """Fail-open: if the migration step fails, the backend stays 'local' and
    the volume is re-pointed back to the (intact) local file store + remounted."""
    restored = {}

    def fake_reconfig(config, *, storage, bucket, s3_access_key_id, s3_secret_access_key):
        restored["storage"] = storage

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_local_to_s3", side_effect=RuntimeError("sync exploded")),
        mock.patch.object(archive_backend, "_reconfigure_volume_storage", side_effect=fake_reconfig),
        mock.patch.object(archive_backend, "_remove_local_object_store") as rm,
    ):
        with pytest.raises(BackendConfigureError):
            configure_backend(
                cfg,
                db,
                s3_bucket="b",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_prefix=None,
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
            )
    state = read_state(db)
    assert state.backend == "local"
    # We restored the file storage and never reclaimed the local objects.
    assert restored.get("storage") == "file"
    rm.assert_not_called()


def test_migrate_local_to_s3_sync_then_repoint(cfg):
    """The migration helper syncs objects first, THEN re-points storage — so a
    sync failure leaves the volume reading from the intact local store."""
    order = []
    with (
        mock.patch.object(archive_backend, "_sync_objects", side_effect=lambda *a, **k: order.append("sync")),
        mock.patch.object(
            archive_backend, "_reconfigure_volume_storage", side_effect=lambda *a, **k: order.append("repoint")
        ),
    ):
        archive_backend._migrate_local_to_s3(
            cfg,
            volume="openhost",
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    assert order == ["sync", "repoint"]


def test_remove_local_object_store_recreates_empty_root(cfg):
    """After migration the object store is deleted and an empty root left in
    its place (so later reads of the path don't hit a missing dir)."""
    store = archive_backend.local_object_store_dir(cfg)
    os.makedirs(os.path.join(store, "openhost", "chunks"), exist_ok=True)
    with open(os.path.join(store, "openhost", "chunks", "1_0_5"), "wb") as f:
        f.write(b"objdata")
    archive_backend._remove_local_object_store(cfg)
    assert os.path.isdir(store)
    leftover = []
    for _dp, _d, files in os.walk(store):
        leftover.extend(files)
    assert leftover == []


# --- _remove_s3_object_prefix (s3->s3 old-bucket reclaim) ------------------


def _paginator_client(pages):
    """A fake boto3 S3 client whose paginator yields the given pages and records
    every delete_objects call."""
    deleted = []
    client = mock.MagicMock()
    paginator = mock.MagicMock()
    paginator.paginate.return_value = iter(pages)
    client.get_paginator.return_value = paginator
    client.delete_objects.side_effect = lambda **kw: deleted.append(kw)
    return client, deleted


def test_remove_s3_object_prefix_deletes_only_under_volume_prefix():
    pages = [
        {
            "Contents": [
                {"Key": "oh-zone-1234/chunks/0/1_0_5"},
                {"Key": "oh-zone-1234/meta/dump-1.json.gz"},
            ]
        }
    ]
    client, deleted = _paginator_client(pages)
    with mock.patch.object(archive_backend, "_s3_client", return_value=client):
        archive_backend._remove_s3_object_prefix(
            s3_bucket="oldbucket",
            s3_region="us-west-2",
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
            volume="oh-zone-1234",
        )
    client.get_paginator.assert_called_once_with("list_objects_v2")
    # Listing is scoped to the volume prefix.
    _, list_kwargs = client.get_paginator.return_value.paginate.call_args
    assert list_kwargs["Prefix"] == "oh-zone-1234/"
    assert list_kwargs["Bucket"] == "oldbucket"
    # Both keys deleted, in one batch.
    keys = [o["Key"] for call in deleted for o in call["Delete"]["Objects"]]
    assert keys == ["oh-zone-1234/chunks/0/1_0_5", "oh-zone-1234/meta/dump-1.json.gz"]


def test_remove_s3_object_prefix_refuses_empty_volume():
    """An empty volume prefix would match the whole bucket; refuse to delete."""
    client, deleted = _paginator_client([])
    with mock.patch.object(archive_backend, "_s3_client", return_value=client) as mk:
        archive_backend._remove_s3_object_prefix(
            s3_bucket="oldbucket",
            s3_region=None,
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
            volume="   ",
        )
    # Never even constructs a client / lists / deletes.
    mk.assert_not_called()
    assert deleted == []


def test_remove_s3_object_prefix_skips_out_of_prefix_keys():
    """Defence-in-depth: any key not under the prefix is never deleted."""
    pages = [
        {
            "Contents": [
                {"Key": "oh-zone-1234/chunks/0/1_0_5"},
                {"Key": "other-zone/chunks/0/9_0_5"},  # must be ignored
            ]
        }
    ]
    client, deleted = _paginator_client(pages)
    with mock.patch.object(archive_backend, "_s3_client", return_value=client):
        archive_backend._remove_s3_object_prefix(
            s3_bucket="oldbucket",
            s3_region=None,
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
            volume="oh-zone-1234",
        )
    keys = [o["Key"] for call in deleted for o in call["Delete"]["Objects"]]
    assert keys == ["oh-zone-1234/chunks/0/1_0_5"]


def test_remove_s3_object_prefix_swallows_errors():
    """Reclaim is non-fatal: an S3 error must not raise (data already durable in
    the new bucket)."""
    with mock.patch.object(archive_backend, "_s3_client", side_effect=RuntimeError("boom")):
        # Must not raise.
        archive_backend._remove_s3_object_prefix(
            s3_bucket="oldbucket",
            s3_region=None,
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
            volume="oh-zone-1234",
        )


# --- storage_summary -------------------------------------------------------


_ARCHIVE_MANIFEST = 'name="x"\n[data]\napp_data=true\napp_archive=true\n'
_PLAIN_MANIFEST = 'name="x"\n[data]\napp_data=true\n'


def test_storage_summary_local_backend(cfg, db):
    s = archive_backend.storage_summary(_ARCHIVE_MANIFEST, db)
    assert s.uses_archive is True
    assert s.requires_archive is True
    assert s.archive_backend == "local"
    assert s.archive_is_durable is False


def test_storage_summary_s3_backend(cfg, db):
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    s = archive_backend.storage_summary(_ARCHIVE_MANIFEST, db)
    assert s.archive_backend == "s3"
    assert s.archive_is_durable is True


def test_storage_summary_non_archive_app(cfg, db):
    s = archive_backend.storage_summary(_PLAIN_MANIFEST, db)
    assert s.uses_archive is False


# --- stop_running_archive_apps / start_apps_by_id --------------------------


def _seed_app(db, app_id, name, status, manifest_raw, port):
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw, container_id) "
        "VALUES (?, ?, '1.0', ?, ?, ?, ?, ?)",
        (app_id, name, f"/tmp/{name}", port, status, manifest_raw, f"ctr-{app_id}"),
    )
    db.commit()


def test_stop_running_archive_apps_only_stops_running_archive_apps(cfg, db):
    """The migration quiesce must stop exactly the RUNNING apps whose manifest
    uses the archive tier — not stopped apps, not non-archive apps — and return
    their app_ids so the caller can restart them afterwards."""
    db.row_factory = sqlite3.Row
    arch = 'name="a"\n[data]\napp_archive=true\n'
    plain = 'name="b"\n[data]\napp_data=true\n'
    _seed_app(db, "id_arch_run", "arch-run", "running", arch, 20401)
    _seed_app(db, "id_arch_stop", "arch-stop", "stopped", arch, 20402)
    _seed_app(db, "id_plain_run", "plain-run", "running", plain, 20403)

    # Quiescence is verified against the real container state, not the DB
    # status column (stop_app_process doesn't touch the DB).  The container is
    # gone after a successful stop -> is_container_running False.
    with (
        mock.patch.object(apps_mod, "stop_app_process") as stop,
        mock.patch.object(apps_mod, "is_container_running", return_value=False),
    ):
        stopped = apps_mod.stop_running_archive_apps(db, cfg)

    assert stopped == ["id_arch_run"]
    assert stop.call_count == 1


def test_stop_running_archive_apps_aborts_if_container_still_running(cfg, db):
    """stop_app_process is best-effort and never raises; if a container is
    still running after the stop attempt (verified via is_container_running),
    we must abort the migration rather than risk the sync racing a live
    writer.  The already-recorded ids are still available to the caller via
    stopped_out."""
    db.row_factory = sqlite3.Row
    _seed_app(db, "stubborn", "stubborn", "running", 'name="a"\n[data]\napp_archive=true\n', 20409)
    recorded: list[str] = []
    with (
        mock.patch.object(apps_mod, "stop_app_process"),
        mock.patch.object(apps_mod, "is_container_running", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="could not stop archive-using app"):
            apps_mod.stop_running_archive_apps(db, cfg, stopped_out=recorded)
    # The id was recorded before the raise so the caller can restart it.
    assert recorded == ["stubborn"]


def test_start_apps_by_id_starts_each(cfg, db):
    """start_apps_by_id restarts every id it's given (companion to the quiesce)."""
    db.row_factory = sqlite3.Row
    with mock.patch.object(apps_mod, "start_app_process") as start:
        apps_mod.start_apps_by_id(["a", "b"], db, cfg)
    assert [c.args[0] for c in start.call_args_list] == ["a", "b"]


def test_start_apps_by_id_continues_on_failure(cfg, db):
    """A failure starting one app must not block the others (best-effort)."""
    db.row_factory = sqlite3.Row
    with mock.patch.object(apps_mod, "start_app_process", side_effect=[RuntimeError("boom"), None]) as start:
        apps_mod.start_apps_by_id(["a", "b"], db, cfg)
    assert start.call_count == 2
