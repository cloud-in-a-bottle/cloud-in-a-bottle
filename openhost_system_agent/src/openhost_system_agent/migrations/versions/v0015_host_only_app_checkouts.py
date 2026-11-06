import os
import pwd
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration

_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
_PODMAN = "/home/host/openhost/.pixi/envs/default/bin/podman"
_APP_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def _require_router_stopped() -> None:
    state = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", "openhost.service"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if state not in ("inactive", "failed"):
        raise RuntimeError(
            "Stop openhost.service before migrating app checkouts; update apply does this automatically."
        )


def _stop_containers(container_ids: list[str]) -> None:
    if not container_ids:
        return
    host = pwd.getpwnam("host")
    subprocess.run(
        [
            "runuser",
            "-u",
            "host",
            "--",
            "env",
            f"XDG_RUNTIME_DIR=/run/user/{host.pw_uid}",
            f"PATH={Path(_PODMAN).parent}:/usr/local/bin:/usr/bin:/bin",
            _PODMAN,
            "stop",
            "--ignore",
            "--time",
            "10",
            "--",
            *container_ids,
        ],
        check=True,
        timeout=120,
    )


def _move_checkout(source: Path, destination: Path) -> None:
    if source.parent.is_symlink() or source.parent.parent.is_symlink() or source.is_symlink():
        raise RuntimeError(f"Checkout source is a symlink: {source}")
    if destination.is_symlink():
        raise RuntimeError(f"Checkout destination is a symlink: {destination}")
    if source.exists():
        if not source.is_dir() or destination.exists():
            raise RuntimeError(f"Cannot move checkout {source} to {destination}")
        source.rename(destination)
    elif destination.exists() and not destination.is_dir():
        raise RuntimeError(f"Checkout destination is not a directory: {destination}")
    # Sync on retries too, when rename completed before a previous interruption.
    if destination.exists():
        for directory in (source.parent, destination.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def migrate(db_path: Path, temp_root: Path) -> None:
    """Move managed checkouts while the router is stopped, then update their stored paths."""
    if not db_path.exists():
        return
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'apps'").fetchone():
            return
        rows = db.execute("SELECT name, repo_path FROM apps").fetchall()
        pending: list[tuple[str, Path, Path]] = []
        for name, repo_path in rows:
            if not _APP_NAME.fullmatch(name):
                raise RuntimeError(f"Invalid app name: {name!r}")
            source = temp_root / "app_temp_data" / name / "repo"
            destination = temp_root / "app_checkouts" / name
            if repo_path and Path(repo_path) == source:
                pending.append((name, source, destination))
        if not pending:
            return

        _require_router_stopped()
        # Stop peers too: cross-app data access can retain open checkout handles.
        # Startup recovery restarts apps whose stored status requires them to run.
        container_ids = [row[0] for row in db.execute("SELECT container_id FROM apps WHERE container_id IS NOT NULL")]
        _stop_containers([container_id for container_id in container_ids if container_id])

        checkout_root = temp_root / "app_checkouts"
        if checkout_root.is_symlink():
            raise RuntimeError(f"Checkout directory is a symlink: {checkout_root}")
        checkout_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner = db_path.stat()
        if os.geteuid() == 0:
            os.chown(checkout_root, owner.st_uid, owner.st_gid)

        for name, source, destination in pending:
            _move_checkout(source, destination)
            # A missing source with an existing destination is a retry after rename.
            # If both are missing, retain the missing-checkout recovery behavior.
            db.execute("UPDATE apps SET repo_path = ? WHERE name = ?", (str(destination), name))
            db.commit()


class Migration0015HostOnlyAppCheckouts(SystemMigration):
    version = 15

    def up(self) -> None:
        data_dir = Path(os.environ.get("OPENHOST_DATA_DIR", _DEFAULT_DATA_DIR))
        migrate(data_dir / "router.db", data_dir.parent.parent / "temporary_data")
