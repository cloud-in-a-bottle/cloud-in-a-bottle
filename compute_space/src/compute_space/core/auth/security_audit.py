"""Runtime security posture checks for the VM.

Checks actual security posture regardless of config settings: TLS active,
SSH password auth disabled, no unexpected ports.

Results are exposed via the authenticated /api/security-audit endpoint.
"""

import shutil
import sqlite3
import subprocess
from typing import TypedDict

from compute_space.core.containers import CONTAINER_GATEWAY_IP


class CheckResult(TypedDict):
    ok: bool
    detail: str


class AuditResult(TypedDict):
    secure: bool
    checks: dict[str, CheckResult]


class ListeningPort(TypedDict):
    """A single TCP listening port, classified for the System page port table."""

    port: int
    address: str  # e.g. "0.0.0.0:443" or "127.0.0.1:9001"
    # One of "secure" (53/80/443/router), "app_range" (9000-9999),
    # "allocated" (explicit DB port mapping), or "unexpected".
    classification: str
    # Human-readable label for the row (e.g. "HTTPS", "App range", app name).
    label: str


# Public-facing ports that should be listening on a healthy VM.
_PUBLIC_SECURE_PORTS: dict[int, str] = {
    22: "SSH",
    53: "CoreDNS",
    80: "ACME HTTP-01",
    443: "HTTPS",
}


def is_sshd_active() -> bool:
    """Check if sshd is currently running (service or socket-activated)."""
    try:
        for unit in ("ssh.service", "sshd.service", "ssh.socket"):
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() == "active":
                return True
        return False
    except Exception:
        return False


def run_audit(db: sqlite3.Connection | None = None) -> AuditResult:
    """Run all security checks. Returns a dict with results.

    {
        "secure": True/False,        # overall pass/fail
        "checks": {
            "check_name": {
                "ok": True/False,
                "detail": "human-readable explanation"
            },
            ...
        }
    }
    """
    checks = {}

    checks["ssh_password_disabled"] = _check_ssh_password_disabled()
    checks["tls_active"] = _check_tls_active()
    checks["no_unexpected_ports"] = _check_no_unexpected_ports(db=db)

    secure = all(c["ok"] for c in checks.values())
    return {"secure": secure, "checks": checks}


# sshd is typically installed in /usr/sbin, which isn't on the systemd unit's
# stripped-down PATH.  Search common system locations explicitly.
_SSHD_SEARCH_PATHS: tuple[str, ...] = (
    "/usr/sbin/sshd",
    "/usr/local/sbin/sshd",
    "/sbin/sshd",
)


def _find_sshd_binary() -> str | None:
    """Return an absolute path to the sshd binary, or None if not found."""
    found = shutil.which("sshd")
    if found:
        return found
    for path in _SSHD_SEARCH_PATHS:
        if shutil.which(path):
            return path
    return None


def _check_ssh_password_disabled() -> CheckResult:
    """SSH password authentication is disabled."""
    sshd = _find_sshd_binary()
    if sshd is None:
        # No sshd installed at all → cannot authenticate over SSH at all,
        # so password auth is effectively disabled.
        return {"ok": True, "detail": "sshd is not installed"}
    try:
        result = subprocess.run(
            [sshd, "-T"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("passwordauthentication "):
                if "no" in line:
                    return {"ok": True, "detail": "PasswordAuthentication no"}
                return {"ok": False, "detail": f"sshd config: {line}"}
        return {
            "ok": True,
            "detail": "PasswordAuthentication not found in sshd config (default: no on Ubuntu 24.04)",
        }
    except Exception as e:
        return {"ok": True, "detail": f"Could not query sshd config: {e}"}


def _check_tls_active() -> CheckResult:
    """TLS is active (HTTPS listening on port 443)."""
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if ":443 " in line or ":443\t" in line:
                return {"ok": True, "detail": "Listening on port 443"}
        return {"ok": False, "detail": "Not listening on port 443"}
    except Exception as e:
        return {"ok": False, "detail": f"Could not check: {e}"}


def _secure_ports() -> dict[int, str]:
    """Return the static-secure-port → label map, including the configured router port.

    The router's HTTP listener (``Config.port``, default 8080) binds to
    ``127.0.0.1`` and ``10.200.0.1`` (container gateway) — it should
    be reported as expected, not flagged.
    """
    secure: dict[int, str] = dict(_PUBLIC_SECURE_PORTS)
    try:
        from compute_space.config import get_config  # noqa: PLC0415 — avoid import cycle at module load

        secure[get_config().port] = "Router (compute_space)"
    except Exception:
        # If the config isn't available (e.g. unit-testing the helper directly),
        # fall back to the public-only set.
        pass
    # The router's outbound-email SMTP submission listener (email v2 service) binds
    # 127.0.0.1 + 10.200.0.1 only (never public), and only when email is enabled;
    # mark it expected so the audit doesn't flag it. Static import (no heavy deps).
    from compute_space.core.email.service import ROUTER_SMTP_PORT  # noqa: PLC0415 — avoid import cycle

    secure[ROUTER_SMTP_PORT] = "Router email service (SMTP)"
    return secure


def is_loopback_address(addr: str) -> bool:
    host = addr.rsplit(":", 1)[0]
    host = host.strip("[]")
    return host in ("127.0.0.1", "::1") or host.startswith("127.")


def list_listening_ports(db: sqlite3.Connection | None = None) -> list[ListeningPort]:
    """Return every TCP port the VM is listening on, classified.

    Used by the audit's ``no_unexpected_ports`` check and by the System page's
    listening-ports table.

    Each entry is unique by ``(port, address)`` and is sorted by port. If
    ``ss`` cannot be invoked, returns an empty list — callers should handle
    this case (the audit check below treats it as a failure).
    """
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    # Build dynamic whitelist from DB port mappings, mapped to app names so
    # the System page can show which app reserved a port.
    app_by_port: dict[int, str] = {}
    if db is not None:
        rows = db.execute(
            """SELECT m.host_port, a.name AS app_name
               FROM app_port_mappings m
               JOIN apps a ON a.app_id = m.app_id"""
        ).fetchall()
        app_by_port = {row["host_port"]: row["app_name"] for row in rows}

    secure_ports = _secure_ports()

    seen: set[tuple[int, str]] = set()
    ports: list[ListeningPort] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        port_str = addr.rsplit(":", 1)[-1]
        try:
            port = int(port_str)
        except ValueError:
            continue
        key = (port, addr)
        if key in seen:
            continue
        seen.add(key)

        if port in secure_ports:
            classification, label = "secure", secure_ports[port]
        elif port in app_by_port:
            classification, label = "allocated", f"App: {app_by_port[port]}"
        elif 9000 <= port <= 9999:
            classification, label = "app_range", "App range (9000-9999)"
        elif is_loopback_address(addr) and 6060 <= port <= 6099:
            classification, label = "secure", "JuiceFS pprof agent (loopback)"
        else:
            classification, label = "unexpected", "Unexpected"

        ports.append(
            {"port": port, "address": addr, "classification": classification, "label": label},
        )

    ports.sort(key=lambda p: (p["port"], p["address"]))
    return ports


def external_ports(ports: list[ListeningPort]) -> list[ListeningPort]:
    """Filter to ports reachable from outside the VM.

    Loopback and the internal container-gateway interface (``openhost0``,
    10.200.0.1) are excluded; wildcard and specific external binds are kept.
    """

    def _internal(addr: str) -> bool:
        host = addr.rsplit(":", 1)[0].strip("[]")
        return is_loopback_address(addr) or host == CONTAINER_GATEWAY_IP

    return [p for p in ports if not _internal(p["address"])]


def _check_no_unexpected_ports(db: sqlite3.Connection | None = None) -> CheckResult:
    """Only expected ports are listening."""
    ports = list_listening_ports(db=db)
    if not ports:
        # ``ss`` failed or returned nothing; surface that instead of silently passing.
        return {"ok": False, "detail": "Could not enumerate listening ports"}

    unexpected = sorted({p["port"] for p in ports if p["classification"] == "unexpected"})
    if unexpected:
        return {
            "ok": False,
            "detail": f"Unexpected listening ports: {unexpected}",
        }
    all_ports = sorted({p["port"] for p in ports})
    return {
        "ok": True,
        "detail": f"Only expected ports listening: {all_ports}",
    }
