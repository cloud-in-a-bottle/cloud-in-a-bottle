"""Per-app egress routing through operator-configured WireGuard tunnels.

An app may declare ``[runtime.container].egress = "<profile>"`` in its
manifest.  When it does, ALL of the app's outbound traffic is forced through
a WireGuard tunnel to an operator-configured exit (the owner's home IP, a
WG-compatible VPN provider, Tor via a WG-fronted relay, …) instead of leaving
via the datacenter IP.

Why this shape (validated end-to-end on a real rootless-podman host):

  * Rootless podman CANNOT ``setns()`` into a root-owned netns, so the app
    cannot simply "join" a host-side tunnel namespace.  Instead we start a
    tiny **infra container** (rootless, owned by the same ``host`` user) with
    ``--network none`` — a private netns with *no* egress at all — and a
    privileged host helper injects a WireGuard interface into it as the SOLE
    default route.  The app container then joins that netns via
    ``--network container:<infra>``.

  * ``--network none`` (not pasta) is mandatory: pasta would install the
    host's own default route in the netns and the app's traffic would leak
    around the tunnel out the datacenter IP.  With ``none`` + a single
    wg-only default route, a dropped tunnel means the app has NO route out —
    a fail-closed kill-switch, proven by test.

  * Ingress is preserved by a veth pair from the host into the infra netns:
    the router still reaches the app at ``<netns_ip>:<local_port>`` and that
    path is a directly-connected route, so it does not traverse (and is not
    killed by) the tunnel.

  * ``--dns`` is rejected by podman together with both ``--network none`` and
    ``--network container:``.  DNS is therefore delivered by bind-mounting a
    generated ``resolv.conf`` (pointing at a resolver reachable *through* the
    tunnel) into the app container, so name resolution can't leak either.

The actual privileged operations (creating veths, moving a wg device into the
infra netns, configuring wg, setting routes) are performed by a small root
helper shipped separately (see the ansible-installed ``openhost-egress``
helper).  This module orchestrates it and owns the podman-side lifecycle.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess

import attr

from compute_space.core.logging import logger

# Link-local /30 used for the host<->infra-netns veth carrying ingress (the
# router reaching the app's port).  Per-app: the third octet is derived from
# a per-app index so concurrent egress apps don't collide.  RFC-5737-ish
# private space unlikely to clash with app/tunnel addressing.
INGRESS_SUBNET_BASE = "10.199"

# Where the root helper lives (installed by ansible).  Overridable for tests.
EGRESS_HELPER = os.environ.get("OPENHOST_EGRESS_HELPER", "/usr/local/sbin/openhost-egress")


class EgressProfileError(Exception):
    """A referenced egress profile is missing or malformed."""


@attr.s(auto_attribs=True, frozen=True)
class EgressProfile:
    """A parsed WireGuard egress profile.

    Holds only the fields the deploy path needs (interface address + DNS); the
    private key stays in the on-disk ``.conf`` and is read directly by the
    privileged helper at injection time.  Note the router process does read the
    whole config file (to parse address/DNS) and the profiles directory is
    owned by the ``host`` user, so the key is not cryptographically isolated
    from the router -- the router user is the trust boundary here, not the key.
    """

    name: str
    config_path: str
    # The address WireGuard assigns the tunnel interface inside the netns,
    # e.g. "10.77.0.2/24".  Parsed from the [Interface] Address line.
    interface_address: str
    # A DNS server reachable THROUGH the tunnel.  From the [Interface] DNS
    # line if present, else a sensible default (1.1.1.1) so resolution does
    # not leak out the datacenter path.
    dns_server: str


def _profile_path(profiles_dir: str, name: str) -> str:
    return os.path.join(profiles_dir, f"{name}.conf")


def profile_exists(profiles_dir: str, name: str) -> bool:
    return os.path.isfile(_profile_path(profiles_dir, name))


def _parse_wg_conf(text: str) -> tuple[str, str | None]:
    """Extract (Address, DNS) from a WireGuard [Interface] section.

    WireGuard .conf is INI-ish but not tomllib-parseable (repeated keys,
    no quoting).  We scan the [Interface] section line by line.  Returns the
    first Address CIDR and the first DNS server (or None).
    """
    address: str | None = None
    dns: str | None = None
    in_interface = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_interface = line.lower() == "[interface]"
            continue
        if not in_interface or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "address" and address is None:
            # Address may be comma-separated (v4,v6); take the first v4.
            address = value.split(",")[0].strip()
        elif key == "dns" and dns is None:
            dns = value.split(",")[0].strip()
    if address is None:
        raise EgressProfileError("WireGuard profile has no [Interface] Address")
    return address, dns


def load_profile(profiles_dir: str, name: str) -> EgressProfile:
    """Load and validate an egress profile by name.

    Raises EgressProfileError if the profile is missing or malformed.
    """
    path = _profile_path(profiles_dir, name)
    if not os.path.isfile(path):
        raise EgressProfileError(
            f"Egress profile {name!r} not found (expected {path}). "
            "The instance operator must register it before an app can use it."
        )
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        raise EgressProfileError(f"Could not read egress profile {name!r}: {e}") from e

    address, dns = _parse_wg_conf(text)
    # Validate the address parses as a CIDR so we fail early, not mid-inject.
    try:
        ipaddress.ip_interface(address)
    except ValueError as e:
        raise EgressProfileError(f"Egress profile {name!r} has invalid Address {address!r}: {e}") from e

    return EgressProfile(
        name=name,
        config_path=path,
        interface_address=address,
        dns_server=dns or "1.1.1.1",
    )


def ingress_ips_for_index(index: int) -> tuple[str, str]:
    """Return (host_side_ip, netns_side_ip) for an app's ingress veth /30.

    ``index`` is a small stable per-app number.  Each app gets a distinct /30
    so multiple egress apps can run concurrently without address collisions.
    Host side is the .1, netns side the .2 of ``10.199.<index>.0/30``.
    """
    if not 0 <= index <= 255:
        raise ValueError(f"egress ingress index out of range: {index}")
    return (f"{INGRESS_SUBNET_BASE}.{index}.1", f"{INGRESS_SUBNET_BASE}.{index}.2")


def helper_available() -> bool:
    """True if the privileged egress helper is installed and executable."""
    return os.path.isfile(EGRESS_HELPER) and os.access(EGRESS_HELPER, os.X_OK)


def _run_helper(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Invoke the privileged egress helper via sudo (NOPASSWD, ansible-scoped).

    The router runs unprivileged; the helper is the only thing that touches
    root-only network state.  We always go through ``sudo`` with the exact
    helper path so the sudoers rule can be tightly scoped.
    """
    cmd = ["sudo", "-n", EGRESS_HELPER, *args]
    logger.info("egress helper: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def setup_app_egress(
    *,
    app_name: str,
    infra_pid: int,
    profile: EgressProfile,
    ingress_index: int,
    resolv_conf_path: str,
) -> None:
    """Wire an app's infra netns for egress through ``profile``.

    Delegates all privileged work to the root helper:
      1. inject a WireGuard device configured from ``profile.config_path`` into
         the infra container's netns (identified by ``infra_pid``), set it as
         the sole default route (fail-closed kill-switch);
      2. add a host<->netns veth /30 (``ingress_index``) so the router can
         reach the app's port without that path crossing the tunnel.

    Also writes ``resolv_conf_path`` (unprivileged; router-owned) pointing at
    the profile's through-tunnel DNS, for the app container to bind-mount.

    Raises RuntimeError on helper failure so the caller fails the deploy
    fail-closed rather than starting an app that would leak.
    """
    host_ip, netns_ip = ingress_ips_for_index(ingress_index)
    result = _run_helper(
        [
            "up",
            "--pid",
            str(infra_pid),
            "--wg-config",
            profile.config_path,
            "--wg-address",
            profile.interface_address,
            "--ingress-host-ip",
            host_ip,
            "--ingress-netns-ip",
            netns_ip,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"egress helper failed to set up tunnel for {app_name!r} "
            f"(profile {profile.name!r}): {(result.stdout + result.stderr).strip()}"
        )

    # DNS through the tunnel (router-owned file; bind-mounted into the app).
    os.makedirs(os.path.dirname(resolv_conf_path), exist_ok=True)
    with open(resolv_conf_path, "w") as f:
        f.write(f"nameserver {profile.dns_server}\n")
    # World-readable so the rootless container user can bind-mount it.
    os.chmod(resolv_conf_path, 0o644)


def teardown_app_egress(*, infra_pid: int | None, ingress_index: int) -> None:
    """Best-effort teardown of an app's egress plumbing.

    Idempotent: safe to call even if setup never ran or the infra container is
    already gone.  Never raises — teardown failures are logged, not fatal, so
    app removal always completes.
    """
    host_ip, netns_ip = ingress_ips_for_index(ingress_index)
    args = ["down", "--ingress-host-ip", host_ip, "--ingress-netns-ip", netns_ip]
    if infra_pid is not None:
        args += ["--pid", str(infra_pid)]
    try:
        result = _run_helper(args)
        if result.returncode != 0:
            logger.warning(
                "egress helper teardown (index=%d) exited %d: %s",
                ingress_index,
                result.returncode,
                (result.stdout + result.stderr).strip(),
            )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("egress helper teardown (index=%d) failed: %s", ingress_index, e)


def wireguard_available() -> bool:
    """True if wireguard-tools is installed on the host (needed by the helper)."""
    return shutil.which("wg") is not None
