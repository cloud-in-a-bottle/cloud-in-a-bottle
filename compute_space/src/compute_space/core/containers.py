"""Container lifecycle using rootless Podman.

Every container runs under the unprivileged ``host`` user in podman's
default rootless user namespace.  Host bind mounts use ``:idmap`` so
container-root writes land on disk owned by the ``host`` user.  Each
app sees only its own ``/data/...`` subdirectory unless it requests
``access_all_app_data``, ``access_all_archive``, or the convenience
shorthand ``access_all_data`` (which implies both).

Security defaults: ``--cap-drop=ALL`` then re-add ``DEFAULT_CAPABILITIES``
(Docker's default set) plus anything the manifest requests from
``SAFE_CAPABILITIES``.  Devices restricted to ``SAFE_DEVICE_PATHS``.
``--security-opt=no-new-privileges=true``.

``OPENHOST_ROUTER_URL`` points at ``host.containers.internal`` (podman's
native host-gateway alias); ``host.docker.internal`` is also registered
via ``--add-host`` so existing Dockerfiles keep resolving.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC
from datetime import datetime

import attr

from compute_space.core import egress as egress_mod
from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping

CONTAINER_ROOT = "/data"

# Dummy-interface IP that the host kernel accepts as local; the router (and a
# container-facing CoreDNS view) bind here so pasta containers can reach host
# services via ``host.containers.internal``.  Created by ansible as the
# ``openhost0`` dummy interface.  Keep in sync with ansible/tasks/containers.yml
# and the openhost0 systemd-networkd unit.
CONTAINER_GATEWAY_IP = "10.200.0.1"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-9]|\x1b[=>]|\x0f|\r")

# Prefix on RuntimeError messages when the build failure is a corrupted
# local build cache.  The HTTP layer uses this to show a "drop cache"
# remediation.
BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"

# Docker's default capability set.  Matches Docker's implicit behaviour
# so debian-packaged daemons (tor, postgres, redis, nginx, …) that
# expect container-root to CHOWN image-layer files, bind low ports,
# setuid, etc. work without per-app manifest changes.  Confined to
# the userns; no host effect.
DEFAULT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "NET_BIND_SERVICE",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_CHROOT",
        "NET_RAW",
        "MKNOD",
        "AUDIT_WRITE",
    }
)

# Build-output fragments that indicate the local containers-storage
# cache is corrupt and can be fixed by pruning.  Narrow enough not to
# match normal build progress, registry pull failures, or missing-
# Dockerfile errors.
_BUILD_CACHE_CORRUPT_FRAGMENTS_UNCONDITIONAL = (
    "storage-driver errored",
    "layer not known",
    # buildah aborts a build step when the overlay layer it just tried to
    # mount is unusable (see _OVERLAY_LINK_CORRUPT_RE) and then reports the
    # discarded build container as "identifier is not a container".
    "identifier is not a container",
)

# The "missing local layer blob" error from containers-storage.  The
# full "content digest" prefix rules out registry-side "not found"
# errors that happen to mention a sha256 digest.
_MISSING_LAYER_RE = re.compile(r"content digest sha256:[0-9a-f]+:\s*not found", re.IGNORECASE)

# containers-storage overlay corruption: when a layer's short-name "link"
# file is empty or missing, the overlay driver assembles a lowerdir that
# resolves to the bare `overlay`/`overlay/l` directory, and the mount
# fails with `readlink <...>/overlay[/l]: invalid argument` — at build
# time ("mounting new container") or at container start.  Retrying reuses
# the same corrupt cached layer, so only a cache prune fixes it.
# Requiring "readlink", "overlay" and "invalid argument" on one line keeps
# this from matching unrelated readlink/EINVAL noise.
_OVERLAY_LINK_CORRUPT_RE = re.compile(r"readlink .*overlay.*: invalid argument", re.IGNORECASE)


def _is_build_cache_corrupt_line(line: str) -> bool:
    if any(frag in line for frag in _BUILD_CACHE_CORRUPT_FRAGMENTS_UNCONDITIONAL):
        return True
    return bool(_MISSING_LAYER_RE.search(line) or _OVERLAY_LINK_CORRUPT_RE.search(line))


def _log_path(app_name: str, temp_data_dir: str) -> str:
    # Name preserved as docker.log for compatibility with existing log
    # tooling on deployed hosts.
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


_MAX_LOG_FILES = 5


def log_timestamp(log_file: str) -> str | None:
    """Return a timestamp string from a log file's mtime, or None if the file is absent/empty."""
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        return None
    mtime = os.path.getmtime(log_file)
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y%m%d_%H%M%S")


def archive_old_log(log_file: str, ts: str | None = None) -> None:
    """Rename an existing log file with a timestamp suffix, pruning oldest if over limit.

    ts is used as the suffix when provided (so multiple files from the same
    session share a timestamp).  Falls back to the file's own mtime.
    """
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        return
    if ts is None:
        ts = log_timestamp(log_file)
    os.rename(log_file, f"{log_file}.{ts}")
    log_dir = os.path.dirname(log_file)
    base = os.path.basename(log_file) + "."
    archived = sorted(f for f in os.listdir(log_dir) if f.startswith(base))
    for old in archived[:-_MAX_LOG_FILES]:
        os.remove(os.path.join(log_dir, old))


def _is_build_cache_corrupt(output: str) -> bool:
    return any(_is_build_cache_corrupt_line(line) for line in output.splitlines())


def _raise_if_build_cache_corrupt(output: str) -> None:
    """Raise a tagged RuntimeError if ``output`` matches a cache-corruption pattern."""
    if _is_build_cache_corrupt(output):
        raise RuntimeError(f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.")


def build_image(
    app_name: str,
    repo_path: str,
    dockerfile_rel_path: str,
    temp_data_dir: str | None = None,
) -> str:
    """Build the container image for an app.  Returns the image tag."""
    image_tag = f"openhost-{app_name}:latest"
    dockerfile_path = os.path.join(repo_path, dockerfile_rel_path)
    cmd = [
        "podman",
        "build",
        "-t",
        image_tag,
        "-f",
        dockerfile_path,
        repo_path,
    ]
    logger.info("Building container image: %s", " ".join(cmd))

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, f"=== Building image: {image_tag} ===\n")

    if temp_data_dir:
        # Stream build output line-by-line so the dashboard can show progress.
        build_output = ""
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    build_output += line
                    _append_log(app_name, temp_data_dir, line)
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("Build process %d did not exit within 5s of SIGKILL", proc.pid)
                raise
        if proc.returncode != 0:
            _raise_if_build_cache_corrupt(build_output)
            tail = build_output[-2000:] if len(build_output) > 2000 else build_output
            raise RuntimeError(f"Container build failed (exit code {proc.returncode}):\n{tail}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            _raise_if_build_cache_corrupt(combined)
            raise RuntimeError(f"Container build failed:\n{combined}")

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, "=== Build complete ===\n\n")

    return image_tag


def _bind_mount_arg(host_path: str, container_path: str, *, read_only: bool = False) -> str:
    """Render a ``-v`` value with ``:idmap`` (or ``:ro,idmap`` for read-only)."""
    options = "ro,idmap" if read_only else "idmap"
    return f"{host_path}:{container_path}:{options}"


@attr.s(auto_attribs=True, frozen=True)
class EgressSetup:
    """Everything ``run_container`` needs to route an app through an egress tunnel.

    Assembled by the caller (``apps.py``) from the app's manifest + DB row +
    the loaded egress profile.  When passed, the app runs inside an infra
    netns whose sole route is the tunnel (fail-closed), and the router reaches
    it at ``netns_ip:container_port`` via a host<->netns veth instead of
    loopback.
    """

    profile: egress_mod.EgressProfile
    ingress_index: int
    # Router-owned resolv.conf path (bind-mounted into the app container) that
    # points at a DNS reachable through the tunnel.
    resolv_conf_path: str


def egress_infra_name(app_name: str) -> str:
    return f"openhost-egress-{app_name}"


def _start_egress_infra(app_name: str, temp_data_dir: str) -> tuple[str, int]:
    """Start the ``--network none`` infra container and return (id, pid).

    The infra container is a minimal always-up holder for the app's private
    netns.  ``--network none`` is essential: it has NO default route, so after
    the helper injects the tunnel as the sole route a dropped tunnel leaves the
    app with no egress (kill-switch), and no pasta route can leak around it.
    """
    infra_name = egress_infra_name(app_name)
    subprocess.run(["podman", "rm", "-f", infra_name], capture_output=True, timeout=30)
    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        infra_name,
        "--network",
        "none",
        "--restart",
        "unless-stopped",
        "--memory",
        "16m",
        # A tiny, always-available base.  We only need the netns to persist.
        "docker.io/library/alpine:latest",
        "sleep",
        "infinity",
    ]
    logger.info("Starting egress infra container: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        _append_log(app_name, temp_data_dir, f"ERROR (egress infra): {result.stderr}\n")
        raise RuntimeError(f"Egress infra container start failed:\n{result.stderr}")
    infra_id = result.stdout.strip()
    pid_result = subprocess.run(
        ["podman", "inspect", "--format", "{{.State.Pid}}", infra_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if pid_result.returncode != 0 or not pid_result.stdout.strip().isdigit():
        subprocess.run(["podman", "rm", "-f", infra_name], capture_output=True, timeout=30)
        raise RuntimeError(f"Could not read egress infra PID: {pid_result.stderr.strip()}")
    return infra_id, int(pid_result.stdout.strip())


def egress_infra_pid(app_name: str) -> int | None:
    """Return the PID of an app's running egress infra container, or None.

    Used by teardown paths to address the app's netns for the privileged
    helper.  Never raises; any error (missing container, timeout) maps to None.
    """
    try:
        result = subprocess.run(
            ["podman", "inspect", "--format", "{{.State.Pid}}", egress_infra_name(app_name)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not read egress infra PID for %s: %s", app_name, e)
        return None
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        return None
    pid = int(result.stdout.strip())
    return pid if pid > 0 else None


def stop_egress_infra(app_name: str) -> None:
    """Stop and remove an app's egress infra container.  Idempotent."""
    subprocess.run(
        ["podman", "rm", "-f", egress_infra_name(app_name)],
        capture_output=True,
        timeout=30,
    )


def run_container(
    app_name: str,
    image_tag: str,
    manifest: AppManifest,
    local_port: int,
    env_vars: dict[str, str],
    data_dir: str,
    temp_data_dir: str,
    archive_dir: str,
    port_mappings: list[PortMapping] | None = None,
    egress_setup: EgressSetup | None = None,
) -> str:
    """Start a detached container for an app.  Returns the container ID."""
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    app_archive_dir = os.path.join(archive_dir, app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    container_name = f"openhost-{app_name}"

    wants_all_app_data = manifest.access_all_app_data
    wants_all_archive = manifest.access_all_archive

    has_app_data = manifest.app_data or manifest.sqlite_dbs or wants_all_app_data
    has_app_temp = manifest.app_temp_data or wants_all_app_data
    has_app_archive = manifest.app_archive or wants_all_archive
    has_vm_data = manifest.access_vm_data or wants_all_app_data

    c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
    c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"
    c_app_archive = f"{CONTAINER_ROOT}/app_archive/{app_name}"
    c_vm_data = f"{CONTAINER_ROOT}/vm_data"

    # Translate host paths in env vars to their in-container equivalents.
    container_env = {}
    for key, value in env_vars.items():
        if key.startswith("OPENHOST_SQLITE_"):
            rel_path = os.path.relpath(value, app_data_dir)
            container_env[key] = os.path.join(c_app_data, rel_path)
        elif key == "OPENHOST_APP_DATA_DIR":
            container_env[key] = c_app_data
        elif key == "OPENHOST_APP_TEMP_DIR":
            container_env[key] = c_app_temp
        elif key == "OPENHOST_APP_ARCHIVE_DIR":
            container_env[key] = c_app_archive
        else:
            container_env[key] = value

    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        container_name,
        "--hostname",
        container_name,
    ]

    if egress_setup is not None:
        # Egress mode: the app joins a private infra netns whose only route is
        # a WireGuard tunnel.  Start that infra container and have the
        # privileged helper inject the tunnel + a host<->netns ingress veth.
        # The app then joins via --network container: and cannot reach the
        # datacenter path at all.
        infra_id, infra_pid = _start_egress_infra(app_name, temp_data_dir)
        try:
            egress_mod.setup_app_egress(
                app_name=app_name,
                infra_pid=infra_pid,
                profile=egress_setup.profile,
                ingress_index=egress_setup.ingress_index,
                resolv_conf_path=egress_setup.resolv_conf_path,
            )
        except Exception:
            # Fail closed: if we can't establish the tunnel, tear the infra
            # container down so we never fall back to leaking out the DC IP.
            stop_egress_infra(app_name)
            raise
        infra_name = egress_infra_name(app_name)
        # NOTE: podman rejects --add-host and --dns when joining another
        # container's netns (`--network container:`).  Both /etc/hosts and DNS
        # come from the shared netns instead: DNS via the bind-mounted
        # resolv.conf below (pointing through the tunnel), and host-gateway
        # aliases are intentionally not provided (an egress app is meant to
        # reach the internet through the tunnel, not host services).
        cmd.extend(
            [
                "--network",
                f"container:{infra_name}",
                "-v",
                _bind_mount_arg(egress_setup.resolv_conf_path, "/etc/resolv.conf", read_only=True),
            ]
        )
    elif manifest.network_host:
        # Host networking: the container shares the host's full network
        # namespace.  Required for apps that do IP forwarding (VPN servers
        # like WireGuard) because pasta can only proxy individual TCP/UDP
        # connections, not forwarded/routed packets from a tunnel interface.
        # Port publishing is unnecessary (the container binds directly on
        # the host), as are --add-host entries (the host's /etc/hosts and
        # DNS are used directly).
        #
        # WARNING: this disables ALL network isolation.  The container can
        # reach any port on the host including other apps' loopback ports.
        logger.warning("App %s uses network_host — all network isolation is disabled", app_name)
        cmd.append("--network=host")
    else:
        cmd.extend(
            [
                "-p",
                f"127.0.0.1:{local_port}:{manifest.container_port}",
                # host.docker.internal kept for compatibility with existing apps.
                "--add-host=host.docker.internal:host-gateway",
                "--add-host=host.containers.internal:host-gateway",
                # Point the container's resolver at the container-facing CoreDNS
                # view bound on the gateway.  That view answers `*.zone_domain`
                # with the gateway IP (where Caddy is reachable) and forwards
                # everything else upstream.  This is what makes "hairpin" access
                # to sibling apps' public HTTPS URLs work: pasta otherwise gives
                # the container the host's public IP as a *local* address, so a
                # connection to `app.zone` (public IP) never leaves the container
                # netns and is refused.  Resolving to the gateway instead sends
                # it to Caddy, which terminates the real zone cert by SNI and
                # applies normal routing/auth.  network_host apps are excluded:
                # they share the host netns and use the host's resolver directly.
                "--dns",
                CONTAINER_GATEWAY_IP,
            ]
        )

    # max-file is not supported by podman's k8s-file driver; archiving is
    # handled by the reload route before run_container is called.
    # max-size=10mb is kept as a safety net against unbounded growth between reloads.
    container_log_file = os.path.join(app_temp_dir, "container.log")
    os.makedirs(app_temp_dir, exist_ok=True)

    cmd.extend(
        [
            f"--memory={manifest.memory_mb}m",
            f"--cpus={manifest.cpu_cores}",
            "--restart=unless-stopped",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--log-driver=k8s-file",
            f"--log-opt=path={container_log_file}",
            "--log-opt=max-size=10mb",
        ]
    )
    if manifest.shm_mb > 0:
        cmd.append(f"--shm-size={manifest.shm_mb}m")
    for cap in sorted(DEFAULT_CAPABILITIES):
        cmd.extend(["--cap-add", cap])

    if wants_all_app_data:
        # Mount the full parent dirs so the app can see all apps' data/temp.
        cmd.extend(["-v", _bind_mount_arg(os.path.join(data_dir, "app_data"), f"{CONTAINER_ROOT}/app_data")])
        cmd.extend(
            [
                "-v",
                _bind_mount_arg(
                    os.path.join(temp_data_dir, "app_temp_data"),
                    f"{CONTAINER_ROOT}/app_temp_data",
                ),
            ]
        )
        # vm_data is rw under wants_all_app_data (admin-level access).
        os.makedirs(vm_data_dir, exist_ok=True)
        cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data)])
    else:
        if has_app_data:
            cmd.extend(["-v", _bind_mount_arg(app_data_dir, c_app_data)])
        if has_app_temp:
            cmd.extend(["-v", _bind_mount_arg(app_temp_dir, c_app_temp)])
        if has_vm_data:
            os.makedirs(vm_data_dir, exist_ok=True)
            cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data, read_only=True)])

    if wants_all_archive:
        # access_all_archive is permissive — skip the archive mount when
        # the tier isn't configured rather than refusing to start.
        if os.path.isdir(archive_dir):
            cmd.extend(["-v", _bind_mount_arg(archive_dir, f"{CONTAINER_ROOT}/app_archive")])
    elif has_app_archive:
        cmd.extend(["-v", _bind_mount_arg(app_archive_dir, c_app_archive)])

    # Structured port mappings: bind TCP+UDP on 0.0.0.0.  host_port
    # values below UNPRIVILEGED_PORT_FLOOR are rejected at manifest
    # parse time so these binds always succeed.
    # With --network=host, port publishing is skipped (container binds
    # directly on the host's interfaces).
    # Egress apps can't publish ports (they join the infra netns, which has no
    # host loopback); structured port mappings are unsupported there and the
    # app's HTTP port is reached over the ingress veth instead.
    if port_mappings and not manifest.network_host and egress_setup is None:
        for pm in port_mappings:
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/tcp"])
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/udp"])

    # Skip caps already in the baseline to keep the argv clean.
    for cap in manifest.capabilities:
        if cap not in DEFAULT_CAPABILITIES:
            cmd.extend(["--cap-add", cap])

    for device in manifest.devices:
        cmd.extend(["--device", device])

    if manifest.network_host:
        # Tell the app which port the router expects it on.  With host
        # networking the container can't use its manifest port if it
        # conflicts with other host services, so it should bind on this
        # allocated port instead.
        container_env["OPENHOST_LOCAL_PORT"] = str(local_port)

    for key, value in container_env.items():
        cmd.extend(["-e", f"{key}={value}"])

    if manifest.container_command:
        cmd.append(image_tag)
        cmd.extend(manifest.container_command.split())
    else:
        cmd.append(image_tag)

    logger.info("Running container: %s", " ".join(cmd))
    _append_log(app_name, temp_data_dir, f"=== Starting container: {container_name} ===\n")

    # Remove any stale container with the same name so podman run doesn't
    # fail with a name conflict.
    subprocess.run(
        ["podman", "rm", "-f", container_name],
        capture_output=True,
        timeout=30,
    )

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        _append_log(app_name, temp_data_dir, f"ERROR: {result.stderr}\n")
        # Fail closed: don't leave a half-configured egress infra behind.
        if egress_setup is not None:
            stop_egress_infra(app_name)
        raise RuntimeError(f"Container start failed:\n{result.stderr}")

    container_id = result.stdout.strip()
    _append_log(app_name, temp_data_dir, f"Container started: {container_id[:12]}\n\n")
    return container_id


def stop_container(container_id: str) -> None:
    """Stop and remove a container by ID or name.  Idempotent.

    Gives the container 10s to exit gracefully (SIGTERM via ``podman stop
    -t 10``).  If that times out or fails, escalates to ``podman kill``
    (SIGKILL) before removing.
    """
    try:
        result = subprocess.run(
            ["podman", "stop", "-t", "10", container_id],
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.warning("podman stop %s exited %d, escalating to kill", container_id, result.returncode)
            subprocess.run(["podman", "kill", container_id], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("podman stop %s timed out, escalating to kill", container_id)
        subprocess.run(["podman", "kill", container_id], capture_output=True, timeout=10)
    subprocess.run(["podman", "rm", "-f", container_id], capture_output=True, timeout=30)


def _row_has_column(row: sqlite3.Row, name: str) -> bool:
    """True if a sqlite3.Row has ``name`` (older rows/tests may predate it)."""
    try:
        return name in row.keys()
    except Exception:
        return False


def stop_app_process(app_row: sqlite3.Row) -> None:
    """Stop the running process for an app.  Does not update the database.

    For egress apps this also tears down the infra container and the
    privileged tunnel/ingress plumbing so nothing is left dangling.
    """
    try:
        if app_row["container_id"]:
            stop_container(app_row["container_id"])
    except Exception as e:
        logger.warning("Error stopping app %s: %s", app_row["name"], e)

    # Egress teardown (best-effort, never fatal).  Guarded so pre-v13 rows or
    # test fixtures without the columns don't raise.
    if _row_has_column(app_row, "egress_profile") and app_row["egress_profile"]:
        app_name = app_row["name"]
        infra_pid = egress_infra_pid(app_name)
        ingress_index = app_row["ingress_index"] if _row_has_column(app_row, "ingress_index") else None
        if ingress_index is not None:
            egress_mod.teardown_app_egress(infra_pid=infra_pid, ingress_index=int(ingress_index))
        stop_egress_infra(app_name)


def remove_image(app_name: str) -> None:
    """Remove the image built for an app.  Idempotent."""
    image_tag = f"openhost-{app_name}:latest"
    subprocess.run(["podman", "rmi", image_tag], capture_output=True, timeout=30)


def container_image_storage_bytes() -> int | None:
    """Total bytes podman uses for image storage (the build cache), from ``podman system df``.

    Returns None when podman is unavailable or the output can't be parsed,
    so status reporting degrades instead of failing the whole endpoint.
    """
    try:
        result = subprocess.run(
            ["podman", "system", "df", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        for row in json.loads(result.stdout):
            if row.get("Type") == "Images":
                return int(row["RawSize"])
        return None
    except Exception as e:
        logger.warning("Could not query podman image storage size: %s", e)
        return None


def drop_docker_build_cache() -> str:
    """Reclaim container-engine disk space via ``podman image prune``.

    Removes every unused image (including stopped-app images, which
    will be rebuilt on next deploy) plus dangling intermediate layers.
    Does not reclaim ``RUN --mount=type=cache`` build mounts.  The
    function and HTTP endpoint path retain "docker" in their names
    for external API compatibility.
    """
    cmd = ["podman", "image", "prune", "--all", "--force"]
    logger.info("Dropping container build cache: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "podman image prune failed")
    return output


def prune_dangling_images() -> str:
    """Remove only dangling (untagged) images via ``podman image prune``.

    Unlike ``drop_docker_build_cache``, this omits ``--all``, so it removes
    only the untagged image layers left behind when an app is rebuilt (each
    rebuild re-tags ``openhost-{app}:latest``, orphaning the previous image).
    Tagged images for stopped apps are kept, so nothing has to be rebuilt on
    next start.  This makes it safe to run unconditionally on a schedule.

    Returns podman's combined output.  Raises ``RuntimeError`` on failure so
    the caller can log it; the periodic thread swallows that so a transient
    podman error never kills the loop.
    """
    cmd = ["podman", "image", "prune", "--force"]
    logger.info("Pruning dangling container images: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "podman image prune failed")
    return output


# An OpenHost app image is always tagged ``openhost-{app_name}:latest`` (see
# ``build_image``).  podman reports the tag fully qualified, e.g.
# ``localhost/openhost-{app_name}:latest``.  This matches that form and pulls
# out the app name.  App names are DNS-label-like (see core.app_id), so the
# capture group is deliberately permissive and validated by the caller against
# the apps DB rather than by a strict name regex here.
_OPENHOST_IMAGE_TAG_RE = re.compile(r"^(?:localhost/)?openhost-(?P<app_name>.+):latest$")


def parse_openhost_image_app_name(tag: str) -> str | None:
    """Return the app name for an ``openhost-{name}:latest`` tag, else None.

    Only OpenHost app images are matched; any other repo tag (base images an
    app FROMs, unrelated tags) returns None so it is never a prune candidate.
    """
    match = _OPENHOST_IMAGE_TAG_RE.match(tag)
    return match.group("app_name") if match else None


@attr.s(auto_attribs=True, frozen=True)
class OpenHostImage:
    """A tagged OpenHost app image as reported by ``podman images``."""

    image_id: str
    app_name: str
    created_epoch: int  # unix seconds


def list_openhost_images() -> list[OpenHostImage]:
    """List tagged OpenHost app images (``openhost-{name}:latest``).

    Parses ``podman images --format json``.  Only images whose tag matches the
    OpenHost app scheme are returned; dangling (untagged) images and unrelated
    repos are skipped.  Returns an empty list when podman is unavailable or its
    output can't be parsed, so a transient error degrades to "prune nothing"
    rather than raising.
    """
    try:
        result = subprocess.run(
            ["podman", "images", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("podman images failed (exit %d): %s", result.returncode, result.stderr.strip())
            return []
        rows = json.loads(result.stdout) or []
    except Exception as e:
        logger.warning("Could not list podman images: %s", e)
        return []

    images: list[OpenHostImage] = []
    for row in rows:
        image_id = row.get("Id")
        # podman may report "Created" as unix seconds (int) and/or "CreatedAt"
        # as a string; prefer the numeric field.
        created = row.get("Created")
        if not isinstance(created, int):
            continue
        # A single image id can carry several tags (Names/RepoTags).  Match any
        # that is an OpenHost app tag; one physical image mapping to two app
        # names is not expected, but handle each tag independently.
        names = row.get("Names") or row.get("RepoTags") or []
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str):
                continue
            app_name = parse_openhost_image_app_name(name)
            if app_name is not None and isinstance(image_id, str):
                images.append(OpenHostImage(image_id=image_id, app_name=app_name, created_epoch=created))
    return images


def remove_image_by_id(image_id: str) -> bool:
    """Force-remove an image by id.  Returns True on success, False otherwise.

    Uses ``podman rmi --force`` by id (not tag) so it targets exactly the image
    we decided to prune, even if the tag moved in the meantime.  ``--force``
    covers the (unexpected) case of a lingering container reference; a failure
    is logged and swallowed so one bad removal can't abort a sweep.
    """
    result = subprocess.run(["podman", "rmi", "--force", image_id], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        logger.warning("Failed to remove image %s: %s", image_id, (result.stdout + result.stderr).strip())
        return False
    return True


def is_container_running(container_id: str) -> bool:
    """Return True iff podman reports the container's ``State.Status`` as ``running``.

    Any error (missing binary, timeout, nonzero exit, unknown container) maps
    to False.  Never raises; unexpected errors are logged at WARNING.
    """
    try:
        result = subprocess.run(
            ["podman", "inspect", "--format", "{{.State.Status}}", container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        logger.warning("podman inspect timed out after 10s for %s", container_id)
        return False
    except OSError as e:
        logger.warning("podman inspect failed for %s with OSError: %s", container_id, e)
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "running"


def get_docker_logs(
    app_name: str,
    temp_data_dir: str,
    container_id: str | None = None,
    tail: int = 10000,
) -> str:
    """Full build log followed by the tail of podman container logs (ANSI-stripped)."""
    parts = []

    log_file = _log_path(app_name, temp_data_dir)
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                parts.append(f.read())
        except OSError as e:
            logger.warning("Could not read build log %s: %s", log_file, e)

    if container_id:
        try:
            result = subprocess.run(
                ["podman", "logs", "--tail", str(tail), container_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            combined = (result.stdout + result.stderr).strip()
            if combined:
                combined = _ANSI_RE.sub("", combined)
                parts.append("=== Container logs ===\n" + combined)
        except subprocess.TimeoutExpired:
            logger.warning("podman logs timed out after 10s for %s", container_id)
        except OSError as e:
            logger.warning("podman logs failed for %s with OSError: %s", container_id, e)

    return "\n".join(parts) if parts else ""
