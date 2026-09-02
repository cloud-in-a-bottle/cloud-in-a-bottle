from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compute_space.config import Config
from compute_space.core import apps
from compute_space.core import startup
from compute_space.core.domains import PRIMARY_DOMAIN_APP_RESTART_MARKER
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import complete_primary_domain_app_restart
from compute_space.core.domains import pending_primary_domain_restart_app_ids
from compute_space.core.domains import set_primary_domain
from compute_space.core.domains import upsert_record
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

_MANIFEST = """\
[app]
name = "test-app"
version = "1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


def _seed_running_app(cfg: Config, app_id: str, name: str, port: int) -> None:
    with closing(open_db(cfg)) as db:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw, container_id) "
            "VALUES (?, ?, '1.0', '/tmp/repo', ?, 'running', ?, ?)",
            (app_id, name, port, _MANIFEST, f"old-{app_id}"),
        )
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, hashlib.sha256(f"old-token-{app_id}".encode()).hexdigest()),
        )
        db.commit()


def test_primary_change_recreates_running_app_with_new_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) "
            "VALUES ('app-id', 'api', 9000, 19500)"
        )
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True, cert_status=DomainCertStatus.ACTIVE))
        change = set_primary_domain(db, "new.local", expected_primary="testzone.local")

    events: list[tuple[str, object]] = []
    monkeypatch.setattr(apps, "stop_container", lambda name: events.append(("stop", name)))
    monkeypatch.setattr(apps, "build_image", lambda *_a, **_kw: pytest.fail("must reuse deployed image"))

    def run_container(
        app_name: str,
        image_tag: str,
        _manifest: AppManifest,
        local_port: int,
        env_vars: dict[str, str],
        _data_dir: str,
        _temp_data_dir: str,
        _archive_dir: str,
        port_mappings: list[PortMapping] | None = None,
    ) -> str:
        events.append(
            (
                "run",
                {
                    "name": app_name,
                    "image": image_tag,
                    "port": local_port,
                    "zone": env_vars["OPENHOST_ZONE_DOMAIN"],
                    "port_mappings": port_mappings,
                },
            )
        )
        return "new-container"

    monkeypatch.setattr(apps, "run_container", run_container)
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)

    assert change.restart_app_ids == ("app-id",)
    apps.recreate_apps_after_primary_change(cfg)

    assert events == [
        ("stop", "old-app-id"),
        (
            "run",
            {
                "name": "my-app",
                "image": "openhost-my-app:latest",
                "port": 19001,
                "zone": "new.local",
                "port_mappings": [PortMapping(label="api", container_port=9000, host_port=19500)],
            },
        ),
    ]
    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("running", "new-container")
        token_hash = db.execute("SELECT token_hash FROM app_tokens WHERE app_id = 'app-id'").fetchone()[0]
        assert token_hash != hashlib.sha256(b"old-token-app-id").hexdigest()
        assert pending_primary_domain_restart_app_ids(db) == ()


def test_queue_release_failure_does_not_kill_successful_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")

    stopped: list[str] = []
    launches: list[str] = []
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    completion_attempts = 0

    def run_container(*_args: object, **_kwargs: object) -> str:
        launches.append("new-container")
        return "new-container"

    def flaky_complete(db: sqlite3.Connection, app_id: str) -> None:
        nonlocal completion_attempts
        completion_attempts += 1
        if completion_attempts == 1:
            raise sqlite3.OperationalError("database busy")
        complete_primary_domain_app_restart(db, app_id)

    class FakeTimer:
        daemon = False

        def __init__(self, _delay: float, target: object, args: tuple[object, ...]) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.target, self.args))

    monkeypatch.setattr(apps, "stop_container", stopped.append)
    monkeypatch.setattr(apps, "is_container_running", lambda container_id: container_id == "new-container")
    monkeypatch.setattr(apps, "run_container", run_container)
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)
    monkeypatch.setattr(apps, "complete_primary_domain_app_restart", flaky_complete)
    monkeypatch.setattr(apps.threading, "Timer", FakeTimer)

    apps.recreate_apps_after_primary_change(cfg)

    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id, error_message FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("running", "new-container", PRIMARY_DOMAIN_APP_RESTART_MARKER)
        assert pending_primary_domain_restart_app_ids(db) == ("app-id",)
    assert stopped == ["old-app-id"]
    assert launches == ["new-container"]

    target, args = scheduled.pop()
    target(*args)  # type: ignore[operator]

    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id, error_message FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("running", "new-container", None)
        assert pending_primary_domain_restart_app_ids(db) == ()
    assert stopped == ["old-app-id"]
    assert launches == ["new-container"]


def test_dead_completed_replacement_is_recreated_before_queue_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")
        db.execute(
            "UPDATE apps SET status = 'running', container_id = 'dead-replacement', error_message = ? "
            "WHERE app_id = 'app-id'",
            (PRIMARY_DOMAIN_APP_RESTART_MARKER,),
        )
        db.commit()

    stopped: list[str] = []
    launches: list[str] = []
    monkeypatch.setattr(apps, "is_container_running", lambda _container: False)
    monkeypatch.setattr(apps, "stop_container", stopped.append)
    monkeypatch.setattr(
        apps,
        "run_container",
        lambda *_args, **_kwargs: launches.append("replacement") or "replacement",
    )
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)

    apps.recreate_apps_after_primary_change(cfg)

    assert stopped == ["dead-replacement"]
    assert launches == ["replacement"]
    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id, error_message FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("running", "replacement", None)
        assert pending_primary_domain_restart_app_ids(db) == ()


def test_launch_cleans_up_container_when_id_cannot_be_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    stopped: list[str] = []
    monkeypatch.setattr(apps, "run_container", lambda *_args, **_kwargs: "new-container")
    monkeypatch.setattr(apps, "stop_container", stopped.append)

    with closing(open_db(cfg)) as db:
        app_row = db.execute("SELECT * FROM apps WHERE app_id = 'app-id'").fetchone()
        db.execute(
            "CREATE TRIGGER reject_new_container BEFORE UPDATE OF container_id ON apps "
            "WHEN NEW.container_id = 'new-container' BEGIN SELECT RAISE(ABORT, 'persist failed'); END"
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="persist failed"):
            apps._launch_app_image(
                app_row,
                parse_manifest_from_string(_MANIFEST),
                "openhost-my-app:latest",
                {},
                [],
                db,
                cfg,
            )

    assert stopped == ["new-container"]


def test_primary_change_restart_continues_after_one_app_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "bad-id", "bad-app", 19001)
    _seed_running_app(cfg, "good-id", "good-app", 19002)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        change = set_primary_domain(db, "new.local", expected_primary="testzone.local")

    monkeypatch.setattr(apps, "stop_container", lambda _name: None)

    def run_container(
        app_name: str,
        _image_tag: str,
        _manifest: AppManifest,
        _local_port: int,
        _env_vars: dict[str, str],
        _data_dir: str,
        _temp_data_dir: str,
        _archive_dir: str,
        port_mappings: list[PortMapping] | None = None,
    ) -> str:
        if app_name == "bad-app":
            raise RuntimeError("container failed")
        return "good-container"

    monkeypatch.setattr(apps, "run_container", run_container)
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)

    assert change.restart_app_ids == ("bad-id", "good-id")
    apps.recreate_apps_after_primary_change(cfg)

    with closing(open_db(cfg)) as db:
        rows = {
            row["name"]: (row["status"], row["container_id"], row["error_message"])
            for row in db.execute("SELECT name, status, container_id, error_message FROM apps")
        }
    assert rows["bad-app"] == ("error", None, "container failed")
    assert rows["good-app"] == ("running", "good-container", None)
    with closing(open_db(cfg)) as db:
        assert pending_primary_domain_restart_app_ids(db) == ()


def test_primary_change_refuses_to_launch_archive_wide_app_when_mount_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        db.execute(
            "UPDATE apps SET manifest_raw = ? WHERE app_id = 'app-id'",
            (_MANIFEST + "\n[data]\naccess_all_archive = true\n",),
        )
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")

    monkeypatch.setattr(apps, "stop_container", lambda _container: None)
    monkeypatch.setattr(apps.archive_backend, "is_archive_dir_healthy", lambda _config, _db: False)
    monkeypatch.setattr(apps, "run_container", lambda *_args, **_kwargs: pytest.fail("must not launch"))

    apps.recreate_apps_after_primary_change(cfg)

    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id, error_message FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("error", None, "Cannot restart app while archive storage is unavailable")
        assert pending_primary_domain_restart_app_ids(db) == ()


def test_primary_change_keeps_retry_queued_when_old_container_cannot_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")

    def fail_stop(_container: str) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(apps, "stop_container", fail_stop)
    monkeypatch.setattr(apps, "is_container_running", lambda _container: True)
    scheduled: list[tuple[float, object, tuple[object, ...]]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, delay: float, target: object, args: tuple[object, ...]) -> None:
            self.delay = delay
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.delay, self.target, self.args))

    monkeypatch.setattr(apps.threading, "Timer", FakeTimer)

    apps.recreate_apps_after_primary_change(cfg)

    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("error", "old-app-id")
        assert pending_primary_domain_restart_app_ids(db) == ("app-id",)
    assert scheduled == [(apps._PRIMARY_DOMAIN_RESTART_RETRY_SECONDS, apps.recreate_apps_after_primary_change, (cfg,))]


def test_primary_change_keeps_app_queued_while_existing_operation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")
        db.execute("UPDATE apps SET status = 'building' WHERE app_id = 'app-id'")
        db.commit()

    stopped: list[str] = []
    scheduled: list[tuple[object, tuple[object, ...]]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, _delay: float, target: object, args: tuple[object, ...]) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.target, self.args))

    monkeypatch.setattr(apps, "stop_container", stopped.append)
    monkeypatch.setattr(apps.threading, "Timer", FakeTimer)

    apps.recreate_apps_after_primary_change(cfg)

    assert stopped == []
    assert scheduled == [(apps.recreate_apps_after_primary_change, (cfg,))]
    with closing(open_db(cfg)) as db:
        assert pending_primary_domain_restart_app_ids(db) == ("app-id",)


def test_primary_restart_worker_reschedules_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    scheduled: list[tuple[object, tuple[object, ...]]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, _delay: float, target: object, args: tuple[object, ...]) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.target, self.args))

    monkeypatch.setattr(apps, "pending_primary_domain_restart_app_ids", lambda _db: 1 / 0)
    monkeypatch.setattr(apps.threading, "Timer", FakeTimer)

    apps.recreate_apps_after_primary_change(cfg)

    assert scheduled == [(apps.recreate_apps_after_primary_change, (cfg,))]


def test_startup_retry_preserves_interrupted_recovery_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path)
    scheduled: list[tuple[object, tuple[object, ...]]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, _delay: float, target: object, args: tuple[object, ...]) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.target, self.args))

    monkeypatch.setattr(apps, "pending_primary_domain_restart_app_ids", lambda _db: 1 / 0)
    monkeypatch.setattr(apps.threading, "Timer", FakeTimer)

    apps.recreate_apps_after_primary_change(cfg, recover_interrupted=True)

    assert scheduled == [(apps.recreate_apps_after_primary_change, (cfg, True))]


def test_startup_resume_reclaims_interrupted_app_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")
        db.execute("UPDATE apps SET status = 'building' WHERE app_id = 'app-id'")
        db.commit()

    monkeypatch.setattr(apps, "stop_container", lambda _container: None)
    monkeypatch.setattr(apps, "is_container_running", lambda _container: False)
    monkeypatch.setattr(apps, "run_container", lambda *_a, **_kw: "new-container")
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)

    apps.recreate_apps_after_primary_change(cfg, recover_interrupted=True)

    with closing(open_db(cfg)) as db:
        row = db.execute("SELECT status, container_id FROM apps WHERE app_id = 'app-id'").fetchone()
        assert tuple(row) == ("running", "new-container")
        assert pending_primary_domain_restart_app_ids(db) == ()


def test_startup_resume_continues_deferred_recovery_after_queue_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_test_config(tmp_path)
    _seed_running_app(cfg, "app-id", "my-app", 19001)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")

    resumed: list[str] = []
    monkeypatch.setattr(apps, "stop_container", lambda _container: None)
    monkeypatch.setattr(apps, "is_container_running", lambda _container: False)
    monkeypatch.setattr(apps, "run_container", lambda *_args, **_kwargs: "new-container")
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)
    monkeypatch.setattr(startup, "resume_deferred_cache_recovery", lambda config: resumed.append(config.db_path))

    apps.recreate_apps_after_primary_change(cfg, recover_interrupted=True)

    assert resumed == [cfg.db_path]


def test_install_refreshes_domain_after_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_test_config(tmp_path)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("new.local", tls=False, mdns=True))
        set_primary_domain(db, "new.local", expected_primary="testzone.local")
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('app-id', 'my-app', '1.0', '/tmp/repo', 19001, 'building', ?)",
            (_MANIFEST,),
        )
        db.commit()

    captured_zones: list[str] = []
    monkeypatch.setattr(apps.storage, "check_before_deploy", lambda _config: None)
    monkeypatch.setattr(apps, "build_image", lambda *_a, **_kw: "openhost-my-app:latest")
    monkeypatch.setattr(
        apps,
        "run_container",
        lambda _name, _image, _manifest, _port, env, *_a, **_kw: (
            captured_zones.append(env["OPENHOST_ZONE_DOMAIN"]) or "container-id"
        ),
    )
    monkeypatch.setattr(apps, "wait_for_ready", lambda _port: True)

    apps.deploy_app_background(
        parse_manifest_from_string(_MANIFEST),
        "/tmp/repo",
        19001,
        {"OPENHOST_ZONE_DOMAIN": "testzone.local"},
        cfg,
        app_id="app-id",
        app_name="my-app",
    )

    assert captured_zones == ["new.local"]
