from __future__ import annotations

import os
import pwd
import re
import subprocess
import tomllib
from pathlib import Path

_PODMAN = "/home/host/openhost/.pixi/envs/default/bin/podman"


def configure_migration_data_dir() -> None:
    """Use the router's data root for deployments that do not inherit its environment."""
    if "OPENHOST_DATA_DIR" in os.environ:
        return
    if data_root := os.environ.get("OPENHOST_DATA_ROOT_DIR"):
        os.environ["OPENHOST_DATA_DIR"] = str(Path(data_root) / "persistent_data" / "openhost")
        return
    selected_config = os.environ.get("OPENHOST_ROUTER_CONFIG") or os.environ.get("OPENHOST_CONFIG")
    config_path = Path(selected_config or "/home/host/.openhost/local_compute_space/config.toml")
    if not selected_config and not config_path.exists():
        return
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    data_root = config.get("openhost", config).get("data_root_dir", "/opt/openhost")
    os.environ["OPENHOST_DATA_DIR"] = str(Path(data_root) / "persistent_data" / "openhost")


def require_router_stopped() -> None:
    state = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", "openhost.service"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if state not in ("inactive", "failed"):
        raise RuntimeError(
            "Stop openhost.service before applying system migrations; update apply does this automatically."
        )


def stop_app_containers(container_ids: list[str]) -> None:
    """Stop recorded and unrecorded managed containers in the host user's Podman storage."""
    host_uid = get_host_uid()
    command = [
        "runuser",
        "-u",
        "host",
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{host_uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{host_uid}/bus",
        f"PATH={Path(_PODMAN).parent}:/usr/local/bin:/usr/bin:/bin",
        _PODMAN,
    ]
    # Include containers started before the router could persist their IDs.
    managed = subprocess.run(
        [*command, "ps", "--all", "--filter", "name=^openhost-", "--format", "{{.ID}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split()
    to_stop = list(dict.fromkeys([*container_ids, *managed]))
    if to_stop:
        subprocess.run(
            [*command, "stop", "--ignore", "--time", "10", "--", *to_stop],
            check=True,
            timeout=120,
        )


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def write_file(path: str, content: str, *, mode: int = 0o600) -> None:
    """Write *content* to *path*, creating parent dirs as needed.

    ``mode`` defaults to ``0o600`` (owner-only).  System config files
    that need to be world-readable (sysctl snippets, systemd units,
    container registries) should pass ``mode=0o644`` explicitly.

    Note: this agent runs as root, so created files will be owned by
    root:root and ``mode`` bits apply relative to that.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(content)
    finally:
        os.umask(old_umask)


def ensure_line(path: str, line: str) -> None:
    p = Path(path)
    if p.exists():
        text = p.read_text()
        if line in text:
            return
        with open(path, "a") as f:
            f.write(line + "\n")


def set_sshd_option(key: str, value: str) -> None:
    config = Path("/etc/ssh/sshd_config")
    text = config.read_text()
    pattern = re.compile(rf"^#?\s*{re.escape(key)}\b.*$", re.MULTILINE)
    replacement = f"{key} {value}"
    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = text.rstrip("\n") + f"\n{replacement}\n"
    config.write_text(text)


def get_host_uid() -> int:
    return pwd.getpwnam("host").pw_uid
