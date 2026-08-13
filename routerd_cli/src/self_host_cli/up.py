"""``openhost up`` -- start the Cloud in a Bottle router.

--domain: enables TLS via ACME.
--zone-domain: enables host-based app subdomain routing without TLS.
"""

import argparse
import ipaddress
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
from pathlib import Path

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from self_host_cli.config_gen import _DEFAULT_DATA_DIR
from self_host_cli.config_gen import generate_config

# Matches secrets.token_urlsafe output; safe to drop into a URL query string
# verbatim with no escaping.
_URL_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_PID_DIR = os.path.expanduser("~/.openhost/run")
_ROUTER_PID = os.path.join(_PID_DIR, "router.pid")


def _claim_token_path(data_dir: str) -> Path:
    # Mirrors compute_space.config.Config.claim_token_path; duplicated here to
    # avoid importing Config (and triggering its directory side effects) just
    # to compute one path.
    return Path(data_dir) / "persistent_data" / "openhost" / "claim_token"


def _require_url_safe(token: str, source: str) -> None:
    """Reject tokens that would need URL-escaping to appear in the claim URL."""
    if not token or not _URL_SAFE_TOKEN_RE.match(token):
        print(
            f"Error: claim token from {source} is not URL-safe. Allowed characters: letters, digits, '-', '_'.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _provision_claim_token(
    data_dir: str,
    supplied_token: str | None,
    port: int,
) -> None:
    """Ensure a claim-token file exists so first-boot /setup can be claimed.

    DefaultConfig has claim_token_required=True (fail-safe), so /setup rejects
    every caller unless a token file is present and supplied via the URL. Here
    we make sure that file exists:

    - supplied_token: validate and write to disk (overwriting any prior value).
    - else, file already on disk: validate the existing token and reuse it.
    - else: generate a fresh URL-safe random token and write it.

    The resulting claim URL is printed so the operator can hand it to the
    human who will claim the workspace.
    """
    path = _claim_token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if supplied_token is not None:
        _require_url_safe(supplied_token, "--claim-token")
        path.write_text(supplied_token)
        token = supplied_token
    elif path.exists():
        # split(":", 1)[0] matches setup_app's parser, which allows trailing
        # metadata after a colon.
        token = path.read_text().strip().split(":", 1)[0]
        _require_url_safe(token, str(path))
    else:
        token = secrets.token_urlsafe(32)
        path.write_text(token)

    # 600 — token grants ownership of this instance on first /setup.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    print(f"  Claim URL: http://localhost:{port}/setup?claim={token}")


def _is_public_ip(ip: str) -> bool:
    """Return True if *ip* is a globally routable address."""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _detect_public_ip() -> str:
    """Detect the machine's public IP address.

    Checks the PUBLIC_IP environment variable first (trusted as-is).
    Falls back to ``hostname -I`` (Linux) then a UDP socket probe
    (cross-platform), filtering out private/link-local/loopback addresses.
    Returns empty string if no public IP is found.
    """
    env_ip = os.environ.get("PUBLIC_IP", "").strip()
    if env_ip:
        return env_ip
    # Try hostname -I (works on Linux/Ubuntu)
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip().split()[0]
            if _is_public_ip(ip):
                return ip
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pass
    # Fallback: UDP socket probe (works on macOS and Linux)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
            if _is_public_ip(ip):
                return ip
    except OSError:
        pass
    return ""


def _write_pid(path: str, pid: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(pid))


def _resolve_zone_domain(args: argparse.Namespace) -> str:
    """Resolve effective zone domain from CLI args.

    --domain implies TLS mode and sets zone domain.
    --zone-domain sets zone domain only (no TLS).
    """

    domain = (args.domain or "").strip()
    zone_domain = (getattr(args, "zone_domain", "") or "").strip()

    if domain and zone_domain and domain != zone_domain:
        print(
            "Error: --domain and --zone-domain must match when both are set.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return domain or zone_domain


def run_up(args: argparse.Namespace) -> None:
    """Run the router directly on the host."""
    print("Starting Cloud in a Bottle...")
    print()

    zone_domain = _resolve_zone_domain(args)
    email = (getattr(args, "email", "") or "").strip()
    config_path = generate_config(port=args.port, domain=zone_domain, email=email)
    print()

    public_ip = _detect_public_ip()
    if public_ip:
        print(f"  Public IP: {public_ip}")
    if zone_domain:
        print(f"  Domain:    {zone_domain} (host-based app subdomain routing)")
    print(f"  Router:    http://localhost:{args.port}")

    _provision_claim_token(
        data_dir=_DEFAULT_DATA_DIR,
        supplied_token=getattr(args, "claim_token", None),
        port=args.port,
    )

    env = os.environ.copy()
    env["OPENHOST_ROUTER_CONFIG"] = config_path

    cmd = [
        "pixi",
        "run",
        "python",
        "-m",
        "compute_space",
    ]

    if args.foreground:
        print(f"Starting router on http://localhost:{args.port} (Ctrl-C to stop)...")
        proc = subprocess.Popen(cmd, cwd=COMPUTE_SPACE_PACKAGE_DIR, env=env)
        _write_pid(_ROUTER_PID, proc.pid)

        def _handle_signal(signum: int, _frame: object) -> None:
            proc.send_signal(signum)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            rc = proc.wait()
            sys.exit(rc)
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=COMPUTE_SPACE_PACKAGE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_pid(_ROUTER_PID, proc.pid)
        print(f"Router started on http://localhost:{args.port} (pid {proc.pid}).")
        print("Run 'openhost down' to stop.")
