"""A preconfigured S3 archive backend delivered in ``first_boot.toml`` (e.g. managed storage).

The provisioner renders an ``[archive_backend]`` table; ``seed_first_boot`` points a pristine
instance's archive at that bucket (before the claim), and ``attach_on_startup`` formats and mounts the
volume there on the first full boot.  An instance whose archive was already formatted or configured is
never touched, and failures never fall back to local disk.
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


def _format_marker(cfg) -> None:  # type: ignore[no-untyped-def]
    """A real JuiceFS format marker in the meta DB, as ``juicefs format`` writes."""
    os.makedirs(archive_backend.juicefs_state_dir(cfg), exist_ok=True)
    with closing(sqlite3.connect(archive_backend.juicefs_meta_db_path(cfg))) as meta:
        meta.execute("CREATE TABLE jfs_setting (name TEXT PRIMARY KEY, value TEXT)")
        meta.execute("INSERT INTO jfs_setting VALUES ('format', '{\"Name\": \"x\"}')")
        meta.commit()


# --- parsing -----------------------------------------------------------------------------------------


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
    assert ab.s3_secret_access_key == _SECRET
    assert _SECRET not in repr(fb) and _SECRET not in repr(ab)


def test_region_is_optional(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK.replace('s3_region = "auto"\n', ""))
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None and fb.archive_backend.s3_region is None


def test_absent_table_means_no_preconfigured_backend(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, "")
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is None


@pytest.mark.parametrize(
    "block,message",
    [
        ('archive_backend = "oops"\n', "must be a table"),
        (_BLOCK.replace('s3_secret_access_key = "' + _SECRET + '"\n', ""), "missing s3_secret_access_key"),
        (_BLOCK.replace('s3_bucket = "bottle-abc"\n', 's3_bucket = ""\n'), "missing s3_bucket"),
        (_BLOCK.replace('s3_access_key_id = "ak123"\n', "s3_access_key_id = 5\n"), "must be a string"),
        (_BLOCK.replace(_ENDPOINT, "http://insecure.example"), "https://"),
        ("[archive_backend]\n", "missing s3_bucket, s3_endpoint, s3_access_key_id, s3_secret_access_key"),
    ],
)
def test_malformed_table_fails_loudly(cfg, tmp_path: Path, block: str, message: str) -> None:  # type: ignore[no-untyped-def]
    # Ignoring it would silently put a managed instance's archive on local disk.
    _write_first_boot(tmp_path, block)
    with pytest.raises(ValueError, match=message):
        read_first_boot()


def test_error_messages_never_include_the_secret(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path, _BLOCK.replace(_ENDPOINT, "http://x"))
    with pytest.raises(ValueError) as raised:
        read_first_boot()
    assert _SECRET not in str(raised.value)


# --- seeding -----------------------------------------------------------------------------------------


def test_seeds_a_pristine_instance(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        state = read_state(db)
        expected_volume = archive_backend.default_volume_name_for_zone(db)
    assert state.backend == "s3"
    assert (state.s3_bucket, state.s3_endpoint, state.s3_region) == ("bottle-abc", _ENDPOINT, "auto")
    assert (state.s3_access_key_id, state.s3_secret_access_key) == ("ak123", _SECRET)
    # Per-zone volume (object prefix), derived from the just-seeded primary domain.
    assert state.juicefs_volume_name == state.s3_prefix == expected_volume
    assert expected_volume.startswith("oh-alice-bottle-cloud-")
    assert state.configured_at is None  # Seeded, not yet formatted.


def test_reseeding_on_later_boots_is_inert(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    seed_first_boot(cfg)  # Restart before the claim.
    with closing(_db(cfg)) as db:
        db.execute("UPDATE archive_backend SET configured_at = '2026-01-01T00:00:00Z'")
        db.commit()
    # A stale first_boot.toml (left on disk) with other credentials changes nothing.
    _write_first_boot(tmp_path, _BLOCK.replace("bottle-abc", "bottle-other").replace("ak123", "ak999"))
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        state = read_state(db)
    assert (state.s3_bucket, state.s3_access_key_id) == ("bottle-abc", "ak123")


def test_never_seeds_over_a_formatted_local_archive(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # E.g. a warm host or an older build already formatted the local volume: its data must stay put.
    _format_marker(cfg)
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        state = read_state(db)
    assert state.backend == "local" and state.s3_bucket is None


def test_never_overrides_a_dashboard_configuration(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_db(cfg)) as db:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='mine', s3_access_key_id='a', "
            "s3_secret_access_key='s', configured_at='2026-01-01T00:00:00Z'"
        )
        db.commit()
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert read_state(db).s3_bucket == "mine"


def test_rest_of_first_boot_still_seeds_with_an_archive_block(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from compute_space.core import settings_store  # noqa: PLC0415

    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert settings_store.get_setting(db, settings_store.CLAIM_TOKEN_KEY) == "tok"
        assert db.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 1


# --- attach_on_startup: formatting the seeded volume ---------------------------------------------------


def _seeded(cfg, tmp_path: Path) -> sqlite3.Connection:  # type: ignore[no-untyped-def]
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    return _db(cfg)


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


def test_first_full_boot_formats_the_seeded_bucket_then_mounts(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_seeded(cfg, tmp_path)) as db:
        volume = read_state(db).juicefs_volume_name
        fmt, fmt_local, mnt = _attach(cfg, db, formatted=False)
        state = read_state(db)
    fmt.assert_called_once_with(
        cfg,
        s3_bucket="bottle-abc",
        s3_region="auto",
        s3_endpoint=_ENDPOINT,
        s3_access_key_id="ak123",
        s3_secret_access_key=_SECRET,
        juicefs_volume_name=volume,
    )
    fmt_local.assert_not_called()
    mnt.assert_called_once_with(cfg, "ak123", _SECRET)
    assert state.configured_at is not None and state.state_message is None


def test_formatted_volume_is_only_mounted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_seeded(cfg, tmp_path)) as db:
        _attach(cfg, db, formatted=False)
        fmt, _, mnt = _attach(cfg, db, formatted=True)
    fmt.assert_not_called()
    mnt.assert_called_once()


def test_format_failure_is_recorded_retried_and_never_falls_back_to_local(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_seeded(cfg, tmp_path)) as db:
        fmt, fmt_local, mnt = _attach(cfg, db, formatted=False, fmt_error=RuntimeError("403 Forbidden"))
        state = read_state(db)
        assert state.backend == "s3" and state.configured_at is None
        assert "403 Forbidden" in (state.state_message or "")
        fmt_local.assert_not_called()
        mnt.assert_not_called()
        # Next boot (e.g. the token finished propagating) succeeds.
        fmt, _, mnt = _attach(cfg, db, formatted=False)
        state = read_state(db)
    fmt.assert_called_once()
    mnt.assert_called_once()
    assert state.configured_at is not None and state.state_message is None


def test_configured_volume_is_never_reformatted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # A configured backend with a missing meta DB has lost metadata: formatting fresh would hide that.
    with closing(_seeded(cfg, tmp_path)) as db:
        _attach(cfg, db, formatted=False)
        fmt, _, mnt = _attach(cfg, db, formatted=False)
        message = read_state(db).state_message or ""
    fmt.assert_not_called()
    mnt.assert_not_called()
    assert "refusing to format" in message


def test_configure_from_the_dashboard_recovers_a_never_formatted_seed(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # Rejected credentials: the owner enters working ones; there is no data to migrate.
    with closing(_seeded(cfg, tmp_path)) as db:
        volume = read_state(db).juicefs_volume_name
        with (
            mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
            mock.patch.object(archive_backend, "_local_volume_formatted", return_value=False),
            mock.patch.object(archive_backend, "format_s3_volume") as fmt,
            mock.patch.object(archive_backend, "mount") as mnt,
            mock.patch.object(archive_backend, "_migrate_s3_to_s3") as migrate,
        ):
            archive_backend.configure_backend(
                cfg,
                db,
                s3_bucket="fixed",
                s3_region="auto",
                s3_endpoint=_ENDPOINT,
                s3_prefix=None,
                s3_access_key_id="ak2",
                s3_secret_access_key="sk2",
            )
        state = read_state(db)
    migrate.assert_not_called()
    assert fmt.call_args.kwargs["s3_bucket"] == "fixed" and fmt.call_args.kwargs["juicefs_volume_name"] == volume
    mnt.assert_called_once_with(cfg, "ak2", "sk2")
    assert (state.s3_bucket, state.configured_at is not None) == ("fixed", True)


def test_dashboard_s3_to_s3_migration_is_unchanged_once_formatted(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_seeded(cfg, tmp_path)) as db:
        _attach(cfg, db, formatted=False)
        with (
            mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
            mock.patch.object(archive_backend, "_local_volume_formatted", return_value=True),
            mock.patch.object(archive_backend, "format_s3_volume") as fmt,
            mock.patch.object(archive_backend, "mount"),
            mock.patch.object(archive_backend, "_remount"),
            mock.patch.object(archive_backend, "_migrate_s3_to_s3") as migrate,
            mock.patch.object(archive_backend, "_remove_s3_object_prefix"),
        ):
            archive_backend.configure_backend(
                cfg,
                db,
                s3_bucket="other",
                s3_region="auto",
                s3_endpoint=_ENDPOINT,
                s3_prefix=None,
                s3_access_key_id="ak2",
                s3_secret_access_key="sk2",
            )
    migrate.assert_called_once()
    fmt.assert_not_called()


# --- the ansible template + task ----------------------------------------------------------------------


def _render(**variables: object) -> str:
    # select_autoescape escapes only html/xml templates, so the TOML template renders as ansible renders it.
    env = Environment(
        loader=FileSystemLoader(str(_REPO_ROOT / "ansible" / "templates")),
        undefined=StrictUndefined,
        autoescape=select_autoescape(),
    )
    env.filters["bool"] = lambda value: str(value).lower() in ("true", "1", "yes")
    env.filters["to_json"] = json.dumps  # Ansible's to_json is json.dumps.
    base = {"domain": "alice.bottle.cloud", "tls": True, "effective_claim_token": "tok"}
    return env.get_template("first_boot.toml.j2").render(**base, **variables)


def _archive_vars(**overrides: str) -> dict[str, str]:
    values = {
        "archive_backend_s3_bucket": "bottle-abc",
        "archive_backend_s3_endpoint": _ENDPOINT,
        "archive_backend_s3_region": "auto",
        "archive_backend_s3_access_key_id": "ak123",
        "archive_backend_s3_secret_access_key": _SECRET,
    }
    values.update(overrides)
    return values


def test_template_renders_a_parseable_archive_table(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rendered = _render(**_archive_vars())
    data = tomllib.loads(rendered)
    assert data["domain"] == "alice.bottle.cloud"  # Top-level keys stay top-level.
    assert data["archive_backend"] == {
        "s3_bucket": "bottle-abc",
        "s3_endpoint": _ENDPOINT,
        "s3_region": "auto",
        "s3_access_key_id": "ak123",
        "s3_secret_access_key": _SECRET,
    }
    # And Bottle reads exactly what the template wrote.
    (tmp_path / "first_boot.toml").write_text(rendered)
    fb = read_first_boot()
    assert fb is not None and fb.archive_backend is not None and fb.archive_backend.s3_bucket == "bottle-abc"


def test_template_with_identity_and_archive_tables(cfg) -> None:  # type: ignore[no-untyped-def]
    rendered = _render(
        imbue_identity_issuer_url="https://kc/realms/x",
        imbue_identity_client_id="cid",
        imbue_identity_client_secret="csec",
        **_archive_vars(),
    )
    data = tomllib.loads(rendered)
    assert data["imbue_identity"]["client_id"] == "cid" and data["archive_backend"]["s3_bucket"] == "bottle-abc"


def test_template_omits_the_table_without_archive_vars(cfg) -> None:  # type: ignore[no-untyped-def]
    assert "archive_backend" not in tomllib.loads(_render())
    assert "archive_backend" not in tomllib.loads(_render(archive_backend_s3_bucket=""))


def test_template_region_defaults_to_auto(cfg) -> None:  # type: ignore[no-untyped-def]
    values = _archive_vars()
    del values["archive_backend_s3_region"]
    assert tomllib.loads(_render(**values))["archive_backend"]["s3_region"] == "auto"


@pytest.mark.parametrize("hostile", ['a"\n[evil]\nx = "1', "back\\slash", "uni\u00e9", "{{ not_jinja }}"])
def test_template_values_cannot_break_out_of_their_string(cfg, hostile: str) -> None:  # type: ignore[no-untyped-def]
    data = tomllib.loads(_render(**_archive_vars(archive_backend_s3_secret_access_key=hostile)))
    assert data["archive_backend"]["s3_secret_access_key"] == hostile and "evil" not in data


def test_writing_first_boot_hides_its_contents_from_logs() -> None:
    tasks = yaml.safe_load((_REPO_ROOT / "ansible" / "tasks" / "write_openhost_config.yml").read_text())
    (write,) = [task for task in tasks if task.get("name") == "Write first_boot.toml seed"]
    assert write["no_log"] is True and write["template"]["mode"] == "0600"


# --- real JuiceFS: the seeded volume is formatted on an actual S3 endpoint ---------------------------


def _juicefs() -> str | None:
    path = os.environ.get("OPENHOST_TEST_JUICEFS") or shutil.which("juicefs")
    return path if path and os.access(path, os.X_OK) else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def s3_gateway(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real S3 endpoint without FUSE: ``juicefs gateway`` over a file-backed volume (its bucket)."""
    binary = _juicefs()
    if binary is None:
        pytest.skip("no juicefs binary (set OPENHOST_TEST_JUICEFS or put juicefs on PATH)")
    store, meta = tmp_path / "gw-store", f"sqlite3://{tmp_path / 'gw-meta.db'}"
    subprocess.run(
        [binary, "--no-agent", "format", "--storage", "file", "--bucket", f"{store}/", meta, "gwbucket"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    port = _free_port()
    env = dict(os.environ, MINIO_ROOT_USER="gwadmin", MINIO_ROOT_PASSWORD="gwpassword123")
    proc = subprocess.Popen(
        [binary, "--no-agent", "gateway", meta, f"127.0.0.1:{port}", "--no-usage-report"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="gwadmin",
        aws_secret_access_key="gwpassword123",
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


def test_real_juicefs_formats_the_seeded_volume_on_s3(cfg, tmp_path: Path, s3_gateway) -> None:  # type: ignore[no-untyped-def]
    binary, endpoint, client = s3_gateway
    _write_first_boot(tmp_path, _BLOCK.replace(_ENDPOINT, "https://placeholder.invalid"))
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        # The gateway serves plain http on loopback; the seeded row points at it.
        db.execute(
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
            archive_backend.attach_on_startup(cfg, db)  # Second boot: mount only, no re-format.
        state = read_state(db)
    assert state.state_message is None and state.configured_at is not None
    assert archive_backend._local_volume_formatted(cfg)
    assert mnt.call_count == 2
    keys = [o["Key"] for o in client.list_objects_v2(Bucket="gwbucket").get("Contents", [])]
    assert f"{volume}/juicefs_uuid" in keys


def test_never_seeds_when_a_metadata_db_exists_even_if_unreadable(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # A locked or corrupt meta DB reads as "unformatted"; seeding must not trust that.
    os.makedirs(archive_backend.juicefs_state_dir(cfg), exist_ok=True)
    Path(archive_backend.juicefs_meta_db_path(cfg)).write_bytes(b"not a sqlite database")
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert read_state(db).backend == "local"


def test_recovery_that_formats_but_fails_to_mount_keeps_the_row_consistent(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with closing(_seeded(cfg, tmp_path)) as db:
        with (
            mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
            mock.patch.object(archive_backend, "_local_volume_formatted", return_value=False),
            mock.patch.object(archive_backend, "format_s3_volume"),
            mock.patch.object(archive_backend, "mount", side_effect=RuntimeError("mount timed out")),
            mock.patch.object(archive_backend, "umount"),
            pytest.raises(RuntimeError, match="mount timed out"),
        ):
            archive_backend.configure_backend(
                cfg,
                db,
                s3_bucket="fixed",
                s3_region="auto",
                s3_endpoint=_ENDPOINT,
                s3_prefix=None,
                s3_access_key_id="ak2",
                s3_secret_access_key="sk2",
            )
        state = read_state(db)
        # The metadata now points at the new bucket; so does the row (not yet configured).
        assert (state.s3_bucket, state.s3_access_key_id, state.configured_at) == ("fixed", "ak2", None)
        # The next boot mounts it (already formatted) with the new credentials.
        fmt, _, mnt = _attach(cfg, db, formatted=True)
        fmt.assert_not_called()
        mnt.assert_called_once_with(cfg, "ak2", "sk2")
        assert read_state(db).configured_at is not None


def test_never_seeds_a_claimed_instance(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # E.g. a claimed instance whose archive never formatted (no meta DB) and a first_boot.toml rewritten later.
    with closing(_db(cfg)) as db:
        db.execute("INSERT INTO users (username, password_hash) VALUES ('owner', 'x')")
        db.commit()
    _write_first_boot(tmp_path)
    seed_first_boot(cfg)
    with closing(_db(cfg)) as db:
        assert read_state(db).backend == "local"
