import functools
import json
import os
import re
import tomllib
from typing import Annotated
from typing import Any
from typing import get_args
from typing import get_origin
from typing import get_type_hints

import attr
import cattrs
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.auth.permissions_v2 import PermissionRecord
from compute_space.core.logging import logger

# App manifest filenames, in the order they are looked for inside a repo.
# ``cloudinabottle.toml`` is the canonical name; ``openhost.toml`` is the legacy name
# and is still accepted as a silent fallback so existing app repos keep
# working without changes.
MANIFEST_FILENAMES: tuple[str, ...] = ("cloudinabottle.toml", "openhost.toml")

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
    ``path`` is taken at face value: Cloud in a Bottle does not validate that it
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
class SettingLabel:
    """Marks an :class:`AppManifest` field as shown in the update review diff,
    under ``group``, displayed as ``text``. See :func:`manifest_setting_labels`."""

    group: str
    text: str


@attr.s(auto_attribs=True, frozen=True)
class AppManifest:
    # [app]
    name: str
    version: Annotated[str, SettingLabel("App", "Version")]
    description: Annotated[str, SettingLabel("App", "Description")] = ""
    authors: Annotated[list[str], SettingLabel("App", "Authors")] = attr.Factory(list)

    # [runtime]
    runtime_type: Annotated[str, SettingLabel("App", "Runtime type")] = "serverfull"

    # [runtime.container]
    container_image: Annotated[str, SettingLabel("Container", "Image")] = ""
    container_port: Annotated[int, SettingLabel("Container", "Container port")] = 0
    container_command: Annotated[str | None, SettingLabel("Container", "Command")] = None
    port_mappings: Annotated[list[PortMapping], SettingLabel("Ports", "Port mappings")] = attr.Factory(list)
    capabilities: Annotated[list[str], SettingLabel("Container", "Linux capabilities")] = attr.Factory(list)
    devices: Annotated[list[str], SettingLabel("Container", "Devices")] = attr.Factory(list)
    # `--shm-size` (in MiB).  0 = use podman's default (64 MiB).
    # Apps doing serious browser work (jibri) need ~2 GiB minimum.
    shm_mb: Annotated[int, SettingLabel("Container", "Shared memory (MB)")] = 0
    # Use the host's network namespace instead of pasta.  Required for apps
    # that do IP forwarding (VPN servers like WireGuard).  Pasta proxies
    # individual TCP/UDP connections but cannot forward routed packets from
    # a tunnel interface.
    #
    # WARNING: this disables ALL network isolation.  The container can
    # connect to any port on the host, including other apps' loopback-bound
    # ports, the router, and local services.  Only use for apps that
    # genuinely need raw network access (VPNs, transparent proxies).
    network_host: Annotated[bool, SettingLabel("Container", "Host networking")] = False

    # [routing]
    health_check: Annotated[str | None, SettingLabel("Routing", "Health check")] = None
    public_paths: Annotated[list[str], SettingLabel("Routing", "Public paths")] = attr.Factory(list)

    # [[links]]
    links: Annotated[list[AppLink], SettingLabel("Routing", "Links")] = attr.Factory(list)

    # [resources]
    memory_mb: Annotated[int, SettingLabel("Resources", "Memory (MB)")] = 128
    # Memory limit for the image *build* step (podman build --memory).
    # None falls back to the app's runtime memory_mb: an app that declares a
    # small memory footprint must not be able to consume far more during its
    # build and trigger the OOM killer against other apps. A build that
    # genuinely needs more memory must declare it here up front.
    build_memory_mb: Annotated[int | None, SettingLabel("Resources", "Build memory (MB)")] = None
    cpu_cores: Annotated[float, SettingLabel("Resources", "CPU cores")] = 0.1
    gpu: Annotated[bool, SettingLabel("Resources", "GPU")] = False

    # [data]
    sqlite_dbs: Annotated[list[str], SettingLabel("Data", "SQLite databases")] = attr.Factory(list)
    app_data: Annotated[bool, SettingLabel("Data", "Permanent data")] = True
    app_temp_data: Annotated[bool, SettingLabel("Data", "Temporary data")] = False
    app_archive: Annotated[bool, SettingLabel("Data", "Archive data")] = False
    access_all_app_data: Annotated[bool, SettingLabel("Data", "Access all app data")] = False

    # [services.v2]
    provides_services_v2: Annotated[list[ServiceProvides], SettingLabel("Services", "Services provided")] = (
        attr.Factory(list)
    )

    # [[services.v2.consumes]] — diffed separately by manifest_newly_declared_permissions_v2,
    # not part of the review label set.
    consumes_services_v2: list[ServiceConsumes] = attr.Factory(list)

    # [app] metadata
    hidden: Annotated[bool, SettingLabel("App", "Hidden")] = False

    raw_toml: str = ""

    @property
    def effective_build_memory_mb(self) -> int:
        """Memory limit to apply to the image build.

        Falls back to the runtime ``memory_mb`` when ``build_memory_mb`` is
        unset, so a build can never exceed the app's declared memory footprint
        — otherwise a "small" app could grab far more at build time and trip
        the OOM killer against other apps. A build that genuinely needs more
        must raise it explicitly via ``build_memory_mb``.
        """
        return self.build_memory_mb if self.build_memory_mb is not None else self.memory_mb


@functools.cache
def manifest_setting_labels() -> dict[str, SettingLabel]:
    """``{field_name: SettingLabel}`` for every :class:`AppManifest` field annotated
    with :class:`SettingLabel`, in declaration order. Drives the update review diff
    (see :func:`manifest_settings_changes`) so it stays in sync with the manifest
    schema without a separately maintained field list. Cached: pure function of a
    fixed class, computed once regardless of how many manifests are diffed."""
    hints = get_type_hints(AppManifest, include_extras=True)
    labels: dict[str, SettingLabel] = {}
    for field in attr.fields(AppManifest):
        hint = hints[field.name]
        if get_origin(hint) is not Annotated:
            continue
        for extra in get_args(hint)[1:]:
            if isinstance(extra, SettingLabel):
                labels[field.name] = extra
    return labels


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
                f"Use a port >= {UNPRIVILEGED_PORT_FLOOR} or route through the Cloud in a Bottle proxy."
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
    validated beyond being a non-empty string — Cloud in a Bottle trusts the app
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
            "App '{}' uses deprecated 'cpu_millicores' in [resources]. Use 'cpu_cores' (fractional cores) instead.",
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
    """Parse an app manifest (``cloudinabottle.toml``) from its string content."""
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
            "App '{}' uses deprecated 'extra_ports' in [runtime.container]. Migrate to [[ports]] tables instead.",
            app_name,
        )

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
        shm_mb=shm_mb,
        health_check=routing.get("health_check"),
        public_paths=routing.get("public_paths", []),
        links=_parse_links(data.get("links", [])),
        memory_mb=resources.get("memory_mb", 128),
        build_memory_mb=resources.get("build_memory_mb"),
        cpu_cores=_parse_cpu_cores(resources, app_name),
        gpu=resources.get("gpu", False),
        sqlite_dbs=data_section.get("sqlite", []),
        app_data=data_section.get("app_data", True),
        app_temp_data=data_section.get("app_temp_data", False),
        app_archive=data_section.get("app_archive", False),
        access_all_app_data=data_section.get("access_all_app_data", False),
        provides_services_v2=_parse_services_v2(data),
        consumes_services_v2=_parse_services_v2_consumes(data),
        raw_toml=raw_text,
    )


def find_manifest_path(repo_path: str) -> str | None:
    """Return the path to the app manifest inside ``repo_path``, or ``None``.

    Looks for the canonical ``cloudinabottle.toml`` first and falls back to the legacy
    ``openhost.toml`` (see ``MANIFEST_FILENAMES``). When both exist,
    ``cloudinabottle.toml`` wins.
    """
    for name in MANIFEST_FILENAMES:
        candidate = os.path.join(repo_path, name)
        if os.path.exists(candidate):
            return candidate
    return None


def parse_manifest(repo_path: str) -> AppManifest:
    manifest_path = find_manifest_path(repo_path)
    if manifest_path is None:
        raise ValueError(f"No {MANIFEST_FILENAMES[0]} found at {os.path.join(repo_path, MANIFEST_FILENAMES[0])}")

    with open(manifest_path, "rb") as f:
        raw_bytes = f.read()

    return parse_manifest_from_string(raw_bytes.decode("utf-8"))


@attr.s(auto_attribs=True, frozen=True)
class PermissionGrant:
    """A single permission grant: which service and what payload."""

    service_url: str
    grant: Grant


def all_manifest_permissions_v2(manifest: AppManifest) -> list[PermissionGrant]:
    """Build a permissions_v2_grants list that approves every permission declared in the manifest."""
    grants: list[PermissionGrant] = []
    for perm in manifest.consumes_services_v2:
        for grant_payload in perm.grants:
            grants.append(PermissionGrant(service_url=perm.service, grant=grant_payload))
    return grants


def _permission_key(service_url: str, grant_payload: Grant) -> tuple[str, str]:
    """Normalized identity for a (service, grant) pair.

    Uses the same ``json.dumps(..., sort_keys=True)`` serialization that
    :func:`grant_permission_v2` stores, so a declared grant and a stored grant
    compare equal regardless of dict key order.
    """
    return (service_url, json.dumps(grant_payload, sort_keys=True))


def manifest_ungranted_permissions_v2(
    manifest: AppManifest,
    granted: list[PermissionRecord],
) -> list[PermissionGrant]:
    """The permissions a manifest declares that are NOT already granted.

    Single source of truth for the "which manifest permissions still need owner
    approval" diff, shared by the app-detail page (post-install display) and the
    update/reload gate (so an app update can't silently pick up newly declared
    permissions). ``granted`` is the app's current ``permissions_v2`` rows
    (e.g. from :func:`get_all_permissions_v2`).

    Duplicate manifest declarations collapse to a single entry, and each new
    permission is returned only once even if declared multiple times.
    """
    granted_keys = {_permission_key(rec.service_url, rec.grant) for rec in granted}
    ungranted: list[PermissionGrant] = []
    seen: set[tuple[str, str]] = set(granted_keys)
    for perm in manifest.consumes_services_v2:
        for grant_payload in perm.grants:
            key = _permission_key(perm.service, grant_payload)
            if key in seen:
                continue
            seen.add(key)
            ungranted.append(PermissionGrant(service_url=perm.service, grant=grant_payload))
    return ungranted


def manifest_newly_declared_permissions_v2(
    manifest: AppManifest,
    granted: list[PermissionRecord],
    previous_manifest_raw: str | None,
) -> list[PermissionGrant]:
    """The permissions an update *newly* declares: in the new manifest, not already
    granted, and not declared by the previously-deployed manifest.

    This is the update gate's delta. Gating on it — rather than on grant state
    (see :func:`manifest_ungranted_permissions_v2`) — means a permission the owner
    deliberately revoked is NOT re-surfaced for approval on an unrelated update:
    it was declared by the previous manifest too, so it isn't part of the delta.
    Only grants the app *author* actually added are gated.

    Scope is permissions only; other manifest changes (ports, resources, image,
    …) always apply on update regardless of this diff.

    ``previous_manifest_raw`` is the currently-deployed manifest text (``apps.manifest_raw``).
    When it is absent or unparseable, this falls back to the full grant-state diff
    (equivalent to :func:`manifest_ungranted_permissions_v2`) — the safe default,
    since it never silently grants something the owner hasn't seen.
    """
    ungranted = manifest_ungranted_permissions_v2(manifest, granted)
    if not previous_manifest_raw:
        return ungranted
    try:
        previous_manifest = parse_manifest_from_string(previous_manifest_raw)
    except ValueError:
        return ungranted
    previously_declared = {
        _permission_key(pg.service_url, pg.grant) for pg in all_manifest_permissions_v2(previous_manifest)
    }
    return [pg for pg in ungranted if _permission_key(pg.service_url, pg.grant) not in previously_declared]


@attr.s(auto_attribs=True, frozen=True)
class SettingChange:
    """A single manifest setting whose value changed between two deployments."""

    group: str
    label: str
    old: str
    new: str


def _render_setting_value(value: Any) -> str:
    """One-line human rendering of a manifest field value for the update diff."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_render_setting_value(v) for v in value) if value else "(none)"
    if attr.has(type(value)):
        return json.dumps(attr.asdict(value), sort_keys=True)
    return str(value)


def manifest_settings_changes(manifest: AppManifest, previous_manifest_raw: str | None) -> list[SettingChange]:
    """Grouped diff of :class:`SettingLabel`-annotated manifest settings changed vs
    the previously-deployed manifest. Empty when there's no parseable previous
    manifest; permissions are excluded (diffed separately, see
    :func:`manifest_newly_declared_permissions_v2`)."""
    if not previous_manifest_raw:
        return []
    try:
        previous = parse_manifest_from_string(previous_manifest_raw)
    except ValueError:
        return []
    changes: list[SettingChange] = []
    for field, setting_label in manifest_setting_labels().items():
        old_val = getattr(previous, field)
        new_val = getattr(manifest, field)
        if old_val != new_val:
            changes.append(
                SettingChange(
                    group=setting_label.group,
                    label=setting_label.text,
                    old=_render_setting_value(old_val),
                    new=_render_setting_value(new_val),
                )
            )
    return changes
