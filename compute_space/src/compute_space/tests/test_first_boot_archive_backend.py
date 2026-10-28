"""A preconfigured S3 archive backend delivered in ``first_boot.toml`` (e.g. managed storage).

``seed_first_boot`` points a fresh, unclaimed instance's archive at the bucket, and ``attach_on_startup``
formats the volume there on the first full boot.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import tomllib
from contextlib import closing
from pathlib import Path
from unittest import mock

import boto3
import pytest
import yaml
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined
from jinja2 import select_autoescape

from compute_space.core import archive_backend
from compute_space.core import settings_store
from compute_space.core.archive_backend import read_state
from compute_space.core.first_boot import read_first_boot
from compute_space.core.first_boot import seed_first_boot
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENDPOINT = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
_SECRET = "0f" * 32
_BLOCK = (
    "[archive_backend]\n"
    's3_bucket = "bottle-abc"\n'
    f's3_endpoint = "{_ENDPOINT}"\n'
    's3_region = "auto"\n'
    's3_access_key_id = "ak123"\n'
    f's3_secret_access_key = "{_SECRET}"\n'
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)
    config_toml = tmp_path / "config.toml"
    config_toml.write_text("")
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    return cfg


def _write_first_boot(tmp_path: Path, extra: str = _BLOCK) -> None:
    (tmp_path / "first_boot.toml").write_text('domain = "alice.bottle.cloud"\nclaim_token = "tok"\n' + extra)


def _db(cfg) -> sqlite3.Connection:  # type: ignore[no-untyped-def]
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --- parsing ---------------------------------------------------------------------------------------


def test_parses_the_archive_backend_table(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None
    ab = fb.archive_backend
    assert (ab.s3_bucket, ab.s3_endpoint, ab.s3_region, ab.s3_access_key_id) == (
        "bottle-abc",
        _ENDPOINT,
        "auto",
        "ak123",
    )
    assert ab.s3_secret_access_key == _SECRET and _SECRET not in repr(fb)


def test_parses_an_optional_volume_name(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK + 'volume_name = "archive"\n')
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None and fb.archive_backend.volume_name == "archive"
    _write_first_boot(tmp_path)
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None and fb.archive_backend.volume_name is None


@pytest.mark.parametrize("name", ["-archive", "Archive", "a", "has_underscore", "x" * 64, "archive-"])
def test_an_invalid_volume_name_fails_loudly(cfg, tmp_path: Path, name: str) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK + f'volume_name = "{name}"\n')
    with pytest.raises(ValueError, match="volume_name"):
        read_first_boot()


def test_absent_table_means_no_preconfigured_backend(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, "")
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is None


@pytest.mark.parametrize(
    "block,message",
    [
        ('archive_backend = "oops"\n', "must be a table"),
        (_BLOCK.replace(f's3_secret_access_key = "{_SECRET}"\n', ""), "missing s3_secret_access_key"),
    ],
)
def test_malformed_table_fails_loudly(cfg, tmp_path: Path, block: str, message: str) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, block)
    with pytest.raises(ValueError, match=message):
        read_first_boot()


# --- seeding ---------------------------------------------------------------------------------------


def test_seeds_a_fresh_instance(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    seed_first_boot(cfg)  # A restart before the claim is harmless.
    with closing(_db(cfg)) as db:
        state = read_state(db)
        volume = archive_backend.default_volume_name_for_zone(db)
    assert (state.backend, state.s3_bucket, state.s3_endpoint, state.s3_region) == (
        "s3",
        "bottle-abc",
        _ENDPOINT,
        "auto",
    )
    assert (state.s3_access_key_id, state.s3_secret_access_key) == ("ak123", _SECRET)
    assert state.juicefs_volume_name == state.s3_prefix == volume and volume.startswith("oh-alice-bottle-cloud-")


def test_seeds_the_provisioners_volume_name_and_marks_it_adopted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK + 'volume_name = "archive"\n')
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        state = read_state(db)
        adopted = settings_store.get_setting(db, archive_backend._ADOPT_VOLUME_KEY)
    assert state.juicefs_volume_name == state.s3_prefix == "archive" and adopted == "1"


def test_never_seeds_an_existing_volume(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    os.makedirs(archive_backend.juicefs_state_dir(cfg), exist_ok=True)
    Path(archive_backend.juicefs_meta_db_path(cfg)).write_bytes(b"")  # Any meta DB, even unreadable.
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert read_state(db).backend == "local"


def test_never_seeds_a_claimed_instance(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_db(cfg)) as db:
        db.execute("INSERT INTO users (username, password_hash) VALUES ('owner', 'x')")
        db.commit()
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert read_state(db).backend == "local"


# --- attach_on_startup -----------------------------------------------------------------------------


class FakeS3:
    """The S3 calls the seeded-volume bring-up makes, over an in-memory bucket of keys."""

    def __init__(self, keys: list[str] | None = None, *, fail: bool = False) -> None:
        self.keys = set(keys or [])
        self.fail = fail
        self.deleted: list[str] = []

    def _list(self, Prefix: str) -> list[dict[str, str]]:  # noqa: N803
        if self.fail:
            raise RuntimeError("S3 unreachable")
        return [{"Key": key} for key in sorted(self.keys) if key.startswith(Prefix)]

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int = 1000):  # type: ignore[no-untyped-def]  # noqa: N803
        return {"Contents": self._list(Prefix)[:MaxKeys]}

    def get_paginator(self, name: str):  # type: ignore[no-untyped-def]
        fake = self

        class Paginator:
            def paginate(self, Bucket: str, Prefix: str):  # type: ignore[no-untyped-def]  # noqa: N803
                contents = fake._list(Prefix)
                for start in range(0, max(len(contents), 1), 2):  # Small pages, to exercise pagination.
                    yield {"Contents": contents[start : start + 2]}

        return Paginator()

    def delete_objects(self, Bucket: str, Delete: dict):  # type: ignore[no-untyped-def]  # noqa: N803
        for obj in Delete["Objects"]:
            self.keys.discard(obj["Key"])
            self.deleted.append(obj["Key"])
        return {}


def _attach(cfg, db, *, formatted: bool, fmt_error: Exception | None = None, s3: FakeS3 | None = None):  # type: ignore[no-untyped-def]
    with (
        mock.patch.object(archive_backend, "is_linux_host", return_value=True),
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_local_volume_formatted", return_value=formatted),
        mock.patch.object(archive_backend, "_s3_client", return_value=s3 or FakeS3()),
        mock.patch.object(archive_backend, "format_s3_volume", side_effect=fmt_error) as fmt,
        mock.patch.object(archive_backend, "_restore_volume_from_dump") as restore,
        mock.patch.object(archive_backend, "format_local_volume") as fmt_local,
        mock.patch.object(archive_backend, "mount") as mnt,
    ):
        archive_backend.attach_on_startup(cfg, db)
    _attach.restore = restore  # type: ignore[attr-defined]
    return fmt, fmt_local, mnt


def test_first_full_boot_formats_the_bucket_then_mounts(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        volume = read_state(db).juicefs_volume_name
        fmt, fmt_local, mnt = _attach(cfg, db, formatted=False)
        assert read_state(db).state_message is None
        fmt2, _, _ = _attach(cfg, db, formatted=True)  # Later boots only mount.
    assert fmt.call_args.kwargs == {
        "s3_bucket": "bottle-abc",
        "s3_region": "auto",
        "s3_endpoint": _ENDPOINT,
        "s3_access_key_id": "ak123",
        "s3_secret_access_key": _SECRET,
        "juicefs_volume_name": volume,
    }
    fmt_local.assert_not_called()
    mnt.assert_called_once_with(cfg, "ak123", _SECRET)
    fmt2.assert_not_called()


def test_format_failure_is_recorded_and_never_falls_back_to_local(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        _, fmt_local, mnt = _attach(cfg, db, formatted=False, fmt_error=RuntimeError("403 Forbidden"))
        state = read_state(db)
    assert state.backend == "s3" and "403 Forbidden" in (state.state_message or "")
    fmt_local.assert_not_called()
    mnt.assert_not_called()


def _seed(cfg, tmp_path: Path, *, volume_name: str | None = "archive") -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK + (f'volume_name = "{volume_name}"\n' if volume_name else ""))
    seed_first_boot(cfg)


def test_restores_from_the_newest_metadata_backup_instead_of_formatting(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _seed(cfg, tmp_path)
    s3 = FakeS3(
        [
            "archive/juicefs_uuid",
            "archive/chunks/0/0/1_0_4194304",
            "archive/meta/dump-2026-10-27-230000.json.gz",
            "archive/meta/dump-2026-10-28-011500.json.gz",
            "archive/meta/dump-2026-10-28-003000.json.gz",
            "archive/meta/notes.txt",
            "other/meta/dump-2026-12-31-000000.json.gz",  # Another volume's backup: never this one's.
        ]
    )
    with closing(_db(cfg)) as db:
        fmt, _, mnt = _attach(cfg, db, formatted=False, s3=s3)
        assert read_state(db).state_message is None
    ((_, _, state, key), _) = _attach.restore.call_args  # type: ignore[attr-defined]
    assert key == "archive/meta/dump-2026-10-28-011500.json.gz" and state.juicefs_volume_name == "archive"
    fmt.assert_not_called()
    mnt.assert_called_once_with(cfg, "ak123", _SECRET)
    assert not s3.deleted


def test_an_adopted_volume_with_data_but_no_backup_is_cleared_then_formatted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # The previous instance died before its first metadata backup: its objects can't be read, and the next
    # instance must still find this volume, so it is cleared rather than set aside.
    _seed(cfg, tmp_path)
    s3 = FakeS3(
        ["archive/juicefs_uuid", "archive/chunks/0/0/1_0_4194304", "archive/chunks/0/0/2_0_4194304", "other/x"]
    )
    with closing(_db(cfg)) as db:
        fmt, _, mnt = _attach(cfg, db, formatted=False, s3=s3)
        state = read_state(db)
    assert sorted(s3.deleted) == [
        "archive/chunks/0/0/1_0_4194304",
        "archive/chunks/0/0/2_0_4194304",
        "archive/juicefs_uuid",
    ]
    assert s3.keys == {"other/x"}
    assert fmt.call_args.kwargs["juicefs_volume_name"] == "archive" and state.juicefs_volume_name == "archive"
    _attach.restore.assert_not_called()  # type: ignore[attr-defined]
    mnt.assert_called_once()


def test_a_per_zone_volume_with_data_but_no_backup_is_left_alone(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # Without a provisioner-named volume nothing says this data is ours to clear: format a fresh volume instead.
    _seed(cfg, tmp_path, volume_name=None)
    with closing(_db(cfg)) as db:
        old = read_state(db).juicefs_volume_name
        s3 = FakeS3([f"{old}/juicefs_uuid", f"{old}/chunks/0/0/1_0_4194304"])
        fmt, _, _ = _attach(cfg, db, formatted=False, s3=s3)
        state = read_state(db)
    fresh = fmt.call_args.kwargs["juicefs_volume_name"]
    assert fresh != old and fresh.startswith(old[:40]) and archive_backend.VOLUME_NAME_RE.fullmatch(fresh)
    assert state.juicefs_volume_name == state.s3_prefix == fresh
    assert not s3.deleted


def test_an_empty_bucket_is_formatted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _seed(cfg, tmp_path)
    with closing(_db(cfg)) as db:
        fmt, _, _ = _attach(cfg, db, formatted=False, s3=FakeS3(["someone-else/juicefs_uuid"]))
    assert fmt.call_args.kwargs["juicefs_volume_name"] == "archive"


def test_an_s3_listing_failure_is_retried_next_boot_not_mistaken_for_an_empty_bucket(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _seed(cfg, tmp_path)
    with closing(_db(cfg)) as db:
        fmt, fmt_local, mnt = _attach(cfg, db, formatted=False, s3=FakeS3(["archive/juicefs_uuid"], fail=True))
        state = read_state(db)
    fmt.assert_not_called()
    fmt_local.assert_not_called()
    mnt.assert_not_called()
    assert state.backend == "s3" and "S3 unreachable" in (state.state_message or "")


def test_a_failed_restore_never_formats_and_never_leaves_a_meta_db(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _seed(cfg, tmp_path)
    s3 = FakeS3(["archive/juicefs_uuid", "archive/meta/dump-2026-10-28-011500.json.gz"])
    s3.download_file = lambda bucket, key, path: Path(path).write_bytes(b"not a dump")  # type: ignore[attr-defined]

    def failing_load(cmd, **kwargs):  # type: ignore[no-untyped-def]
        Path(cmd[3].removeprefix("sqlite3://")).write_bytes(b"half-loaded")
        return subprocess.CompletedProcess(cmd, 1, "", f"load failed for {_SECRET}")

    with closing(_db(cfg)) as db:
        with (
            mock.patch.object(archive_backend, "is_linux_host", return_value=True),
            mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
            mock.patch.object(archive_backend, "_s3_client", return_value=s3),
            mock.patch.object(archive_backend.subprocess, "run", side_effect=failing_load),
            mock.patch.object(archive_backend, "format_s3_volume") as fmt,
            mock.patch.object(archive_backend, "mount") as mnt,
        ):
            archive_backend.attach_on_startup(cfg, db)
        state = read_state(db)
    fmt.assert_not_called()
    mnt.assert_not_called()
    assert "juicefs load failed" in (state.state_message or "")
    state_dir = Path(archive_backend.juicefs_state_dir(cfg))
    assert not Path(archive_backend.juicefs_meta_db_path(cfg)).exists()
    assert not list(state_dir.glob("restore-*")), "scratch files left behind"


def test_restore_errors_never_show_the_secret(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _seed(cfg, tmp_path)
    s3 = FakeS3(["archive/meta/dump-2026-10-28-011500.json.gz"])
    s3.download_file = lambda bucket, key, path: Path(path).write_bytes(b"dump")  # type: ignore[attr-defined]
    calls = []

    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[2] == "config":
            return subprocess.CompletedProcess(cmd, 1, "", f"bad secret {_SECRET}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with closing(_db(cfg)) as db:
        with (
            mock.patch.object(archive_backend, "is_linux_host", return_value=True),
            mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
            mock.patch.object(archive_backend, "_s3_client", return_value=s3),
            mock.patch.object(archive_backend.subprocess, "run", side_effect=run),
            mock.patch.object(archive_backend, "mount"),
        ):
            archive_backend.attach_on_startup(cfg, db)
        message = read_state(db).state_message or ""
    assert "juicefs config failed" in message and _SECRET not in message


def test_the_mount_unit_backs_up_metadata_every_15_minutes() -> None:
    unit = (_REPO_ROOT / "ansible" / "templates" / "openhost-juicefs.service.j2").read_text()
    (exec_start,) = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert "--backup-meta 15m" in exec_start


# --- ansible template + task -----------------------------------------------------------------------


def _render(**variables: object) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_REPO_ROOT / "ansible" / "templates")),
        undefined=StrictUndefined,
        autoescape=select_autoescape(),  # Escapes only html/xml: TOML renders as ansible renders it.
    )
    env.filters["bool"] = lambda value: str(value).lower() in ("true", "1", "yes")
    env.filters["to_json"] = json.dumps  # Ansible's to_json is json.dumps.
    base = {"domain": "alice.bottle.cloud", "tls": True, "effective_claim_token": "tok"}
    return env.get_template("first_boot.toml.j2").render(**base, **variables)


_VARS = {
    "archive_backend_s3_bucket": "bottle-abc",
    "archive_backend_s3_endpoint": _ENDPOINT,
    "archive_backend_s3_region": "auto",
    "archive_backend_s3_access_key_id": "ak123",
    "archive_backend_s3_secret_access_key": _SECRET,
}


def test_template_renders_a_table_bottle_reads(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rendered = _render(**_VARS)
    assert tomllib.loads(rendered)["domain"] == "alice.bottle.cloud"  # Top-level keys stay top-level.
    (tmp_path / "first_boot.toml").write_text(rendered)
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None and fb.archive_backend.s3_secret_access_key == _SECRET
    assert "archive_backend" not in tomllib.loads(_render())


def test_template_renders_the_volume_name_only_when_set() -> None:
    assert "volume_name" not in tomllib.loads(_render(**_VARS))["archive_backend"]
    data = tomllib.loads(_render(**_VARS, archive_backend_volume_name="archive"))
    assert data["archive_backend"]["volume_name"] == "archive"


def test_template_values_cannot_break_out_of_their_string() -> None:
    hostile = 'a"\n[evil]\nx = "1'
    data = tomllib.loads(_render(**{**_VARS, "archive_backend_s3_secret_access_key": hostile}))
    assert data["archive_backend"]["s3_secret_access_key"] == hostile and "evil" not in data


def test_writing_first_boot_hides_its_contents_from_logs() -> None:
    tasks = yaml.safe_load((_REPO_ROOT / "ansible" / "tasks" / "write_openhost_config.yml").read_text())
    (write,) = [task for task in tasks if task.get("name") == "Write first_boot.toml seed"]
    assert write["no_log"] is True and write["template"]["mode"] == "0600"


# --- real JuiceFS against a real S3 endpoint --------------------------------------------------------


def _juicefs() -> str | None:
    path = os.environ.get("OPENHOST_TEST_JUICEFS") or shutil.which("juicefs")
    return path if path and os.access(path, os.X_OK) else None


@pytest.fixture
def s3_gateway(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real S3 endpoint without FUSE: ``juicefs gateway`` over a file-backed volume (its bucket)."""
    binary = _juicefs()
    if binary is None:
        pytest.skip("no juicefs binary (set OPENHOST_TEST_JUICEFS or put juicefs on PATH)")
    meta = f"sqlite3://{tmp_path / 'gw-meta.db'}"
    subprocess.run(
        [
            binary,
            "--no-agent",
            "format",
            "--storage",
            "file",
            "--bucket",
            f"{tmp_path / 'gw-store'}/",
            meta,
            "gwbucket",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, MINIO_ROOT_USER="gwadmin", MINIO_ROOT_PASSWORD="gwpassword123")
    proc = subprocess.Popen(
        [binary, "--no-agent", "gateway", meta, f"127.0.0.1:{port}", "--no-usage-report"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id="gwadmin", aws_secret_access_key="gwpassword123"
    )
    try:
        for _ in range(100):
            try:
                client.list_buckets()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        yield binary, endpoint, client
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_real_juicefs_formats_the_seeded_volume(cfg, tmp_path: Path, s3_gateway) -> None:  # type: ignore[no-untyped-def]
    binary, endpoint, client = s3_gateway
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        db.execute(  # Point the seeded row at the local plain-http gateway.
            "UPDATE archive_backend SET s3_bucket='gwbucket', s3_endpoint=?, s3_region='us-east-1', "
            "s3_access_key_id='gwadmin', s3_secret_access_key='gwpassword123'",
            (endpoint,),
        )
        db.commit()
        volume = read_state(db).juicefs_volume_name
        with (
            mock.patch.object(archive_backend, "is_linux_host", return_value=True),
            mock.patch.object(archive_backend, "_juicefs_binary", return_value=binary),
            mock.patch.object(archive_backend, "mount") as mnt,  # No FUSE here.
        ):
            archive_backend.attach_on_startup(cfg, db)
            archive_backend.attach_on_startup(cfg, db)  # Second boot: mount only.
        assert read_state(db).state_message is None
    assert archive_backend._local_volume_formatted(cfg) and mnt.call_count == 2
    keys = [o["Key"] for o in client.list_objects_v2(Bucket="gwbucket").get("Contents", [])]
    assert f"{volume}/juicefs_uuid" in keys


def _inner_gateway(binary: str, meta_db: Path):  # type: ignore[no-untyped-def]
    """A gateway over an instance's own JuiceFS volume, standing in for its FUSE mount. No storage credentials
    in its environment: it must find them in the meta DB, as the mount unit's restored volume does."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_KEY", "SECRET_KEY", "AWS_ACCESS_KEY_ID")} | {
        "MINIO_ROOT_USER": "inneradmin",
        "MINIO_ROOT_PASSWORD": "innerpassword123",
    }
    proc = subprocess.Popen(
        [
            binary,
            "--no-agent",
            "gateway",
            f"sqlite3://{meta_db}",
            f"127.0.0.1:{port}",
            "--no-usage-report",
            "--backup-meta",
            "0",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id="inneradmin",
        aws_secret_access_key="innerpassword123",
        region_name="us-east-1",
    )
    for _ in range(100):
        try:
            client.list_buckets()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return proc, client


def _seed_on_gateway(cfg, db, endpoint: str) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "UPDATE archive_backend SET s3_bucket='gwbucket', s3_endpoint=?, s3_region='us-east-1', "
        "s3_access_key_id='gwadmin', s3_secret_access_key='gwpassword123'",
        (endpoint,),
    )
    db.commit()


def test_real_juicefs_restores_the_previous_instances_archive(  # type: ignore[no-untyped-def]
    cfg, tmp_path: Path, s3_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instance A formats the adopted volume and writes files; JuiceFS's metadata backup lands in the bucket;
    A is lost. Instance B, a different domain on a fresh disk, restores A's archive from that backup and reads
    A's files, then keeps writing to the same volume."""
    binary, endpoint, outer = s3_gateway
    patches = (
        mock.patch.object(archive_backend, "is_linux_host", return_value=True),
        mock.patch.object(archive_backend, "_juicefs_binary", return_value=binary),
        mock.patch.object(archive_backend, "mount"),
    )

    # Instance A.
    _write_first_boot(tmp_path, _BLOCK + 'volume_name = "archive"\n')
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        _seed_on_gateway(cfg, db, endpoint)
        with patches[0], patches[1], patches[2]:
            archive_backend.attach_on_startup(cfg, db)
        assert read_state(db).state_message is None
    meta_a = Path(archive_backend.juicefs_meta_db_path(cfg))
    proc, inner = _inner_gateway(binary, meta_a)
    files = {"photos/a.jpg": os.urandom(5_000_000), "notes.txt": b"from instance A"}
    try:
        for key, body in files.items():
            inner.put_object(Bucket="archive", Key=key, Body=body)
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    # JuiceFS's own backup, as the mount unit writes it every 15 minutes.
    dump = tmp_path / "dump.json.gz"
    subprocess.run([binary, "--no-agent", "dump", f"sqlite3://{meta_a}", str(dump)], check=True, capture_output=True)
    outer.upload_file(str(dump), "gwbucket", "archive/meta/dump-2026-10-28-011500.json.gz")

    # Instance B: another domain, another disk, the same bucket and volume name.
    b_dir = tmp_path / "b"
    b_dir.mkdir()
    cfg_b = _make_test_config(b_dir, seed_primary=False)
    init_db(cfg_b.db_path)
    (b_dir / "config.toml").write_text("")
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(b_dir / "config.toml"))
    (b_dir / "first_boot.toml").write_text(
        'domain = "bob.bottle.cloud"\nclaim_token = "tok"\n' + _BLOCK + 'volume_name = "archive"\n'
    )
    seed_first_boot(cfg_b)
    with closing(_db(cfg_b)) as db:
        _seed_on_gateway(cfg_b, db, endpoint)
        with (
            mock.patch.object(archive_backend, "is_linux_host", return_value=True),
            mock.patch.object(archive_backend, "_juicefs_binary", return_value=binary),
            mock.patch.object(archive_backend, "mount") as mnt,
            mock.patch.object(archive_backend, "format_s3_volume") as fmt,
        ):
            archive_backend.attach_on_startup(cfg_b, db)
            archive_backend.attach_on_startup(cfg_b, db)  # Second boot: already restored, mount only.
        assert read_state(db).state_message is None
    fmt.assert_not_called()
    assert mnt.call_count == 2 and archive_backend._local_volume_formatted(cfg_b)
    assert not list(Path(archive_backend.juicefs_state_dir(cfg_b)).glob("restore-*"))

    proc, inner = _inner_gateway(binary, Path(archive_backend.juicefs_meta_db_path(cfg_b)))
    try:
        for key, body in files.items():
            assert inner.get_object(Bucket="archive", Key=key)["Body"].read() == body
        inner.put_object(Bucket="archive", Key="from-b.txt", Body=b"instance B")
        assert inner.get_object(Bucket="archive", Key="from-b.txt")["Body"].read() == b"instance B"
    finally:
        proc.terminate()
        proc.wait(timeout=20)
