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


def _attach(cfg, db, *, formatted: bool, fmt_error: Exception | None = None):  # type: ignore[no-untyped-def]
    with (
        mock.patch.object(archive_backend, "is_linux_host", return_value=True),
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_local_volume_formatted", return_value=formatted),
        mock.patch.object(archive_backend, "format_s3_volume", side_effect=fmt_error) as fmt,
        mock.patch.object(archive_backend, "format_local_volume") as fmt_local,
        mock.patch.object(archive_backend, "mount") as mnt,
    ):
        archive_backend.attach_on_startup(cfg, db)
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
