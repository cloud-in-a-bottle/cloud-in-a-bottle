import os
import re
import tomllib
from typing import Any

import attr
import cattrs
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.logging import logger

_SHORTNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Must match net.ipv4.ip_unprivileged_port_start from ansible/tasks/containers.yml.
# host_port values below this are rejected at parse time.
UNPRIVILEGED_PORT_FLOOR = 25

# Linux capabilities that can be safely granted inside a rootless podman
# user namespace.  Anything outside this set is rejected at parse time
# because it either requires real host privilege (SYS_ADMIN, SYS_MODULE,
# SYS_PTRACE, SYS_RAWIO, SYS_TIME, SYS_BOOT, MAC_ADMIN, MAC_OVERRIDE) or
# effectively requires CAP_SYS_ADMIN to do anything.  Allowlist (not
# denylist) so future kernel caps are denied by default.
SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Networking (VPN-style apps: tailscale, wireguard).
        "NET_ADMIN",
        "NET_RAW",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        # File ownership / permissions within the userns.
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "SETFCAP",
        # Process control within the userns.
        "KILL",
        "SETUID",
        "SETGID",
        "SETPCAP",
        # Device node creation (restricted by rootless anyway).
        "MKNOD",
        "AUDIT_WRITE",
        # mlock (some DBs) + chroot (some init systems).
        "IPC_LOCK",
        "IPC_OWNER",
        "SYS_CHROOT",
    }
)

# Host devices safe to pass through to rootless containers via the
# ``[runtime.container].devices`` list (mapped to ``podman --device``).
# Apps do NOT need to list ``/dev/null``, ``/dev/zero``, ``/dev/random``,
# ``/dev/urandom``, ``/dev/full``, ``/dev/tty`` or ``/dev/console`` here;
# podman (like Docker) mounts a default ``/dev`` with those character
# devices inside every container via the OCI runtime spec.  The allowlist
# below only exists to gate EXTRA host devices the app wants bound in on
# top of that baseline (serial adapters, FUSE, TUN/TAP).  Anything outside
# this set (e.g. ``/dev/mem``, ``/dev/kmem``, raw block devices, ``/dev/kvm``)
# is rejected at parse time.
SAFE_DEVICE_PATHS: frozenset[str] = frozenset(
    {
        "/dev/net/tun",
        "/dev/fuse",
        # First 8 slots of each serial/USB-TTY family; expand if needed.
        *(f"/dev/ttyS{i}" for i in range(8)),
        *(f"/dev/ttyUSB{i}" for i in range(8)),
        *(f"/dev/ttyACM{i}" for i in range(8)),
    }
)


def _normalize_service_url(url: str) -> str:
    url = url.removeprefix("https://").removeprefix("http://")
    return url.rstrip("/")


@attr.s(auto_attribs=True, frozen=True)
class PortMapping:
    """A structured port mapping declared in [[ports]]."""

    label: str
    container_port: int
    host_port: int = 0  # 0 = auto-assign


@attr.s(auto_attribs=True, frozen=True)
class AppLink:
    """A user-facing link declared in [[links]].

    Apps use this to advertise interesting paths on their own URL that
    aren't the bare root — e.g. an admin console at ``/_openhost/admin``.
    The dashboard renders these as clickable links under the app. The
    ``path`` is taken at face value: OpenHost does not validate that it
    exists, that it is reachable, or that it is (or isn't) gated by auth.
    It is simply advertised to the user.
    """

    name: str
    path: str


@attr.s(auto_attribs=True, frozen=True)
class ServiceProvides:
    service: str = attr.ib(converter=_normalize_service_url)
    version: str
    endpoint: str


@attr.s(auto_attribs=True, frozen=True)
class ServiceConsumes:
    service: str = attr.ib(converter=_normalize_service_url)
    shortname: str
    version: str
    # Each grant is either an opaque string (e.g. "read") or a JSON structure
    # (e.g. {"key": "DB_URL"} or a list). The shape is defined by the service,
    # not by us — providers receive the raw grants and decide what they mean.
    grants: list[Grant] = attr.Factory(list)


@attr.s(auto_attribs=True, frozen=True)
class AppManifest:
    # [app]
    name: str
    version: str
    description: str = ""
    authors: list[str] = attr.Factory(list)

    # [runtime]
    runtime_type: str = "serverfull"

    # [runtime.container]
    container_image: str = ""
    container_port: int = 0
    container_command: str | None = None
    port_mappings: list[PortMapping] = attr.Factory(list)
    capabilities: list[str] = attr.Factory(list)
    devices: list[str] = attr.Factory(list)
    # `--shm-size` (in MiB).  0 = use podman's default (64 MiB).
    # Apps doing serious browser work (jibri) need ~2 GiB minimum.
    shm_mb: int = 0
    # Use the host's network namespace instead of pasta.  Required for apps
    # that do IP forwarding (VPN servers like WireGuard).  Pasta proxies
    # individual TCP/UDP connections but cannot forward routed packets from
    # a tunnel interface.
    #
    # WARNING: this disables ALL network isolation.  The container can
    # connect to any port on the host, including other apps' loopback-bound
    # ports, the router, and local services.  Only use for apps that
    # genuinely need raw network access (VPNs, transparent proxies).
    network_host: bool = False

    # Route all of the app's outbound traffic through a named egress profile
    # (a WireGuard tunnel to an operator-configured exit: the owner's home IP,
    # a WG-compatible VPN provider, etc.).  The profile name references a
    # config the instance operator has registered on the host; the app repo
    # never carries tunnel keys.  Empty string = normal datacenter egress.
    #
    # Enforced fail-closed: the app runs inside an infra netns whose ONLY
    # route is the tunnel, so if the tunnel drops the app has no egress at all
    # (no leak to the datacenter IP).  Mutually exclusive with network_host
    # (which shares the host netns and cannot be confined to a tunnel).
    egress: str = ""

    # [routing]
    health_check: str | None = None
    public_paths: list[str] = attr.Factory(list)

    # [[links]]
    links: list[AppLink] = attr.Factory(list)

    # [resources]
    memory_mb: int = 128
    cpu_cores: float = 0.1
    gpu: bool = False

    # [data]
    sqlite_dbs: list[str] = attr.Factory(list)
    app_data: bool = True
    app_temp_data: bool = False
    app_archive: bool = False
    access_vm_data: bool = False
    # Granular cross-app data flags (preferred):
    access_all_app_data: bool = False
    access_all_archive: bool = False
    # Convenience shorthand: equivalent to access_all_app_data + access_all_archive.
    access_all_data: bool = False

    # [services.v2]
    provides_services_v2: list[ServiceProvides] = attr.Factory(list)

    # [[services.v2.consumes]]
    consumes_services_v2: list[ServiceConsumes] = attr.Factory(list)

    # [app] metadata
    hidden: bool = False

    raw_toml: str = ""


def _validate_devices(devices: list[Any]) -> list[str]:
    """Normalise and validate ``[runtime.container].devices`` entries.

    Accepts the ``<host>[:<container>][:rwm]`` form and validates only
    the host path against ``SAFE_DEVICE_PATHS``.
    """
    if not isinstance(devices, list):
        raise ValueError("[runtime.container].devices must be a list of strings")
    validated: list[str] = []
    for entry in devices:
        if not isinstance(entry, str):
            raise ValueError(f"[runtime.container].devices must contain strings, got {type(entry).__name__}")
        host_path = entry.split(":", 1)[0].strip()
        if host_path not in SAFE_DEVICE_PATHS:
            allowed = ", ".join(sorted(SAFE_DEVICE_PATHS))
            raise ValueError(
                f"[runtime.container].devices entry {entry!r} is not in the allowlist.  Allowed host paths: {allowed}."
            )
        validated.append(entry)
    return validated


def _validate_capabilities(caps: list[Any]) -> list[str]:
    """Normalise and validate ``[runtime.container].capabilities``.

    Accepts ``CAP_`` prefix or bare names (podman uses bare).  Rejects
    anything not in ``SAFE_CAPABILITIES``.
    """
    if not isinstance(caps, list):
        raise ValueError("[runtime.container].capabilities must be a list of strings")
    normalised: list[str] = []
    for entry in caps:
        if not isinstance(entry, str):
            raise ValueError(f"[runtime.container].capabilities must contain strings, got {type(entry).__name__}")
        name = entry.strip().upper()
        if name.startswith("CAP_"):
            name = name[len("CAP_") :]
        if name not in SAFE_CAPABILITIES:
            allowed = ", ".join(sorted(SAFE_CAPABILITIES))
            raise ValueError(
                f"[runtime.container].capabilities entry {entry!r} is not safe to grant under "
                f"rootless podman.  Allowed: {allowed}."
            )
        normalised.append(name)
    return normalised


# Egress profile names are used as podman container names, netns handle
# filenames, and WireGuard config filenames, so they must be a conservative
# DNS-label-like token.  Same shape as app shortnames.
_EGRESS_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _validate_egress(egress: Any, network_host: bool) -> str:
    """Normalise and validate ``[runtime.container].egress``.

    Empty/absent means normal datacenter egress.  A non-empty value must be a
    safe profile-name token and is incompatible with ``network_host`` (which
    shares the host netns and so can't be confined to a per-app tunnel).
    """
    if egress is None or egress == "":
        return ""
    if not isinstance(egress, str):
        raise ValueError("[runtime.container].egress must be a string profile name")
    name = egress.strip()
    if not _EGRESS_PROFILE_RE.match(name):
        raise ValueError(
            f"[runtime.container].egress {egress!r} is not a valid profile name "
            "(lowercase letters, digits, '-' and '_', max 32 chars)."
        )
    if network_host:
        raise ValueError(
            "[runtime.container].egress cannot be combined with network_host: "
            "host networking shares the host netns and cannot be confined to a tunnel."
        )
    return name


def _parse_ports(ports_list: list[Any]) -> list[PortMapping]:
    """Parse and validate [[ports]] entries from manifest data."""
    seen_labels: set[str] = set()
    seen_container_ports: set[int] = set()
    seen_host_ports: set[int] = set()
    result: list[PortMapping] = []
    for entry in ports_list:
        if not isinstance(entry, dict):
            raise ValueError("Each [[ports]] entry must be a table")
        label = entry.get("label")
        if not label or not isinstance(label, str):
            raise ValueError("Each [[ports]] entry requires a string 'label'")
        if label in seen_labels:
            raise ValueError(f"Duplicate port label: '{label}'")
        seen_labels.add(label)
        cport = entry.get("container_port")
        if cport is None or not isinstance(cport, int) or cport < 0:
            raise ValueError(f"[[ports]] '{label}' requires a non-negative integer 'container_port'")
        if cport in seen_container_ports:
            raise ValueError(f"Duplicate container_port {cport} in [[ports]]")
        seen_container_ports.add(cport)
        hport = entry.get("host_port", 0)
        if not isinstance(hport, int) or hport < 0:
            raise ValueError(f"[[ports]] '{label}' host_port must be a non-negative integer")
        if hport != 0 and hport < UNPRIVILEGED_PORT_FLOOR:
            raise ValueError(
                f"[[ports]] '{label}' host_port {hport} is below the unprivileged port floor "
                f"({UNPRIVILEGED_PORT_FLOOR}); rootless podman cannot bind to it. "
                f"Use a port >= {UNPRIVILEGED_PORT_FLOOR} or route through the openhost proxy."
            )
        if hport != 0 and hport in seen_host_ports:
            raise ValueError(f"Duplicate host_port {hport} in [[ports]]")
        if hport != 0:
            seen_host_ports.add(hport)
        result.append(PortMapping(label=label, container_port=cport, host_port=hport))
    return result


def _parse_links(links_list: Any) -> list[AppLink]:
    """Parse [[links]] entries from manifest data.

    Each entry needs a non-empty ``name`` and ``path``. The path is not
    validated beyond being a non-empty string — OpenHost trusts the app
    to advertise its own paths and only displays them to the user.
    """
    if not isinstance(links_list, list):
        raise ValueError("[[links]] must be a list of tables")
    result: list[AppLink] = []
    for entry in links_list:
        if not isinstance(entry, dict):
            raise ValueError("Each [[links]] entry must be a table")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("Each [[links]] entry requires a non-empty string 'name'")
        path = entry.get("path")
        if not path or not isinstance(path, str):
            raise ValueError(f"[[links]] '{name}' requires a non-empty string 'path'")
        result.append(AppLink(name=name, path=path))
    return result


def _parse_cpu_cores(resources: dict[str, Any], app_name: str) -> float:
    """Read [resources].cpu_cores, falling back to the deprecated cpu_millicores key.

    cpu_millicores (1000 = 1 core) was the old unit; cpu_cores expresses the
    same allocation as fractional cores. When only the old key is present we
    convert it (millicores / 1000) and warn.
    """
    if "cpu_cores" in resources:
        return float(resources["cpu_cores"])
    if "cpu_millicores" in resources:
        logger.warning(
            "App '%s' uses deprecated 'cpu_millicores' in [resources]. Use 'cpu_cores' (fractional cores) instead.",
            app_name,
        )
        return float(resources["cpu_millicores"]) / 1000.0
    return 0.1


def _structure_list(data: list[Any], cls: type[Any], label: str) -> list[Any]:
    try:
        return [cattrs.structure(entry, cls) for entry in data]
    except (cattrs.ClassValidationError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid [[{label}]]: {exc}") from exc


def _parse_services_v2(data: dict[str, Any]) -> list[ServiceProvides]:
    entries = data.get("services", {}).get("v2", {}).get("provides", [])
    return _structure_list(entries, ServiceProvides, "services.v2.provides")


def _parse_services_v2_consumes(data: dict[str, Any]) -> list[ServiceConsumes]:
    raw_entries = data.get("services", {}).get("v2", {}).get("consumes", [])
    perms: list[ServiceConsumes] = []
    seen_shortnames: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid [[services.v2.consumes]] entry: must be a table, got {type(entry).__name__}")
        for required in ("service", "shortname", "version"):
            if required not in entry:
                raise ValueError(f"Invalid [[services.v2.consumes]] entry: missing required field {required!r}")
        grants_raw = entry.get("grants", [])
        if not isinstance(grants_raw, list):
            raise ValueError("[[services.v2.consumes]] grants must be a list")
        grants: list[Grant] = []
        for g in grants_raw:
            if not isinstance(g, (str, dict, list)):
                raise ValueError(
                    f"[[services.v2.consumes]] grant entry must be a string, table, or array, got {type(g).__name__}"
                )
            grants.append(g)
        p = ServiceConsumes(
            service=entry["service"],
            shortname=entry["shortname"],
            version=entry["version"],
            grants=grants,
        )
        if not _SHORTNAME_RE.match(p.shortname):
            raise ValueError(
                f"Invalid [[services.v2.consumes]] shortname {p.shortname!r}: must match {_SHORTNAME_RE.pattern}"
            )
        if p.shortname in seen_shortnames:
            raise ValueError(f"Duplicate [[services.v2.consumes]] shortname {p.shortname!r}")
        seen_shortnames.add(p.shortname)
        try:
            SpecifierSet(p.version)
        except InvalidSpecifier as e:
            raise ValueError(
                f"Invalid [[services.v2.consumes]] version specifier {p.version!r} for shortname {p.shortname!r}: {e}"
            ) from e
        perms.append(p)
    return perms


def parse_manifest_from_string(raw_text: str) -> AppManifest:
    """Parse an openhost.toml manifest from its string content."""
    data = tomllib.loads(raw_text)

    app_section = data.get("app", {})
    if not app_section.get("name"):
        raise ValueError("Manifest missing required [app].name")
    if not app_section.get("version"):
        raise ValueError("Manifest missing required [app].version")

    runtime = data.get("runtime", {})
    runtime_type = runtime.get("type", "serverfull")
    if runtime_type not in ("serverless", "serverfull"):
        raise ValueError(f"Invalid runtime type: {runtime_type}")

    container = runtime.get("container", {})
    if not container.get("image"):
        raise ValueError("[runtime.container].image is required")
    if not container.get("port"):
        raise ValueError("[runtime.container].port is required")

    shm_mb = container.get("shm_mb", 0)
    if not isinstance(shm_mb, int) or shm_mb < 0:
        raise ValueError("[runtime.container].shm_mb must be a non-negative integer")

    routing = data.get("routing", {})
    resources = data.get("resources", {})
    data_section = data.get("data", {})

    app_name = app_section["name"]

    # Deprecated: extra_ports (raw Docker -p strings)
    if container.get("extra_ports"):
        logger.warning(
            "App '%s' uses deprecated 'extra_ports' in [runtime.container]. Migrate to [[ports]] tables instead.",
            app_name,
        )

    _compat_all_data = data_section.get("access_all_data", False)
    return AppManifest(
        name=app_name,
        version=app_section["version"],
        description=app_section.get("description", ""),
        authors=app_section.get("authors", []),
        hidden=app_section.get("hidden", False),
        runtime_type=runtime_type,
        container_image=container["image"],
        container_port=container["port"],
        container_command=container.get("command"),
        port_mappings=_parse_ports(data.get("ports", [])),
        capabilities=_validate_capabilities(container.get("capabilities", [])),
        devices=_validate_devices(container.get("devices", [])),
        network_host=container.get("network_host", False),
        egress=_validate_egress(container.get("egress", ""), container.get("network_host", False)),
        shm_mb=shm_mb,
        health_check=routing.get("health_check"),
        public_paths=routing.get("public_paths", []),
        links=_parse_links(data.get("links", [])),
        memory_mb=resources.get("memory_mb", 128),
        cpu_cores=_parse_cpu_cores(resources, app_name),
        gpu=resources.get("gpu", False),
        sqlite_dbs=data_section.get("sqlite", []),
        app_data=data_section.get("app_data", True),
        app_temp_data=data_section.get("app_temp_data", False),
        app_archive=data_section.get("app_archive", False),
        access_vm_data=data_section.get("access_vm_data", False),
        access_all_data=_compat_all_data,
        access_all_app_data=data_section.get("access_all_app_data", False) or _compat_all_data,
        access_all_archive=data_section.get("access_all_archive", False) or _compat_all_data,
        provides_services_v2=_parse_services_v2(data),
        consumes_services_v2=_parse_services_v2_consumes(data),
        raw_toml=raw_text,
    )


def parse_manifest(repo_path: str) -> AppManifest:
    manifest_path = os.path.join(repo_path, "openhost.toml")
    if not os.path.exists(manifest_path):
        raise ValueError(f"No openhost.toml found at {manifest_path}")

    with open(manifest_path, "rb") as f:
        raw_bytes = f.read()

    return parse_manifest_from_string(raw_bytes.decode("utf-8"))
