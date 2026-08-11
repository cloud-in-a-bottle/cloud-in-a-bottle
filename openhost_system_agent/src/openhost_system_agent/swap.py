"""Manage the host swap file.

Small instances run out of RAM under memory pressure and get apps OOM-killed. A
swap file gives the kernel somewhere to spill cold pages, trading disk for
resilience. OpenHost provisions a default-sized swap file on every host (ansible
on fresh hosts, the v9 migration on already-provisioned ones) and lets the owner
resize it from the settings page.

This module is the single source of truth for the swap file's location, its
default size, and the create/resize/inspect logic. It is root-only: only root
can mkswap/swapon and edit /etc/fstab.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from openhost_system_agent.protocol import SwapStatus

#: Where the swap file lives. /swapfile is the conventional location on a
#: single-volume host.
SWAP_PATH = "/swapfile"

#: Default swap size provisioned on every host. Kept in sync with the ansible
#: task's ``swap_size_gb`` default (a test enforces this).
DEFAULT_SWAP_SIZE_GIB = 16

#: Guardrails for owner-chosen sizes. 0 disables swap entirely; the upper bound
#: keeps a fat-fingered value from trying to fill the disk.
MIN_SWAP_SIZE_GIB = 0
MAX_SWAP_SIZE_GIB = 256

_FSTAB_PATH = "/etc/fstab"
# The fstab entry that makes the swap file persist across reboots.
_FSTAB_LINE = f"{SWAP_PATH} none swap sw 0 0"


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("swap operations must be run as root")


def _running_in_container() -> bool:
    """True when we're running inside a container, where swap can't be provisioned.

    Swap is a host-kernel resource: a container shares the host kernel and can't
    ``swapon`` (it's namespaced away and needs real-host CAP_SYS_ADMIN), while a
    multi-GiB backing file would just burn the container's writable layer. Both
    podman and Docker drop a marker file at container start; either means we are
    inside a container and must skip swap provisioning. Real VPS/bare-metal hosts
    — the only place swap actually applies — have neither marker.
    """
    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, raising RuntimeError with the captured stderr on failure.

    We surface stderr rather than letting a bare CalledProcessError bubble up so
    a failed mkswap/swapon is debuggable from the migration log or the CLI's
    JSON error, instead of showing only an exit code.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def _active_swap_bytes() -> int:
    """Bytes of *our* swap file currently swapped on, else 0.

    Reads /proc/swaps and matches the swap file path; other swap devices are
    ignored so the number reflects the file this module manages.
    """
    try:
        text = Path("/proc/swaps").read_text()
    except FileNotFoundError:
        return 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        # /proc/swaps columns: Filename Type Size Used Priority (Size in KiB).
        if len(parts) >= 3 and parts[0] == SWAP_PATH:
            return int(parts[2]) * 1024
    return 0


def get_swap_status() -> SwapStatus:
    """Report the managed swap file's size and whether it is swapped on.

    ``size_bytes`` is the on-disk file size (the configured size, which survives
    a swapoff); ``active`` reflects whether it is currently in use.
    """
    path = Path(SWAP_PATH)
    size_bytes = path.stat().st_size if path.exists() else 0
    return SwapStatus(size_bytes=size_bytes, path=SWAP_PATH, active=_active_swap_bytes() > 0)


def _set_fstab_entry(present: bool) -> None:
    """Ensure the swap fstab line is present or absent. Idempotent.

    Rewrites /etc/fstab dropping any existing entry for our swap path, then
    appends the canonical line when *present*. Only lines whose first field is
    exactly the swap path are removed, so operator entries and comments survive.
    """
    path = Path(_FSTAB_PATH)
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if line.split()[:1] != [SWAP_PATH]]
    if present:
        kept.append(_FSTAB_LINE)
    path.write_text("\n".join(kept) + "\n")


def _create_and_enable(size_gib: int) -> None:
    """(Re)create the swap file at *size_gib* GiB and swap it on.

    fallocate is near-instant but produces a file some filesystems refuse to
    swap on; if mkswap/swapon rejects it we fall back to dd, which writes real
    zeroed blocks. Any prior swap file is swapped off and removed first so this
    is safe to call for both initial creation and resizing.
    """
    _run("swapoff", SWAP_PATH, check=False)
    Path(SWAP_PATH).unlink(missing_ok=True)
    try:
        _run("fallocate", "-l", f"{size_gib}G", SWAP_PATH)
        os.chmod(SWAP_PATH, 0o600)
        _run("mkswap", SWAP_PATH)
        _run("swapon", SWAP_PATH)
        return
    except RuntimeError:
        _run("swapoff", SWAP_PATH, check=False)
        Path(SWAP_PATH).unlink(missing_ok=True)
    _run("dd", "if=/dev/zero", f"of={SWAP_PATH}", "bs=1M", f"count={size_gib * 1024}")
    os.chmod(SWAP_PATH, 0o600)
    _run("mkswap", SWAP_PATH)
    _run("swapon", SWAP_PATH)


def resize_swapfile(size_gib: int) -> SwapStatus:
    """Set the swap file to *size_gib* GiB, enabling it and persisting it in
    /etc/fstab. ``size_gib`` of 0 disables swap and removes the file. Root-only,
    idempotent, safe to call repeatedly.
    """
    _require_root()
    if not MIN_SWAP_SIZE_GIB <= size_gib <= MAX_SWAP_SIZE_GIB:
        raise ValueError(f"swap size must be between {MIN_SWAP_SIZE_GIB} and {MAX_SWAP_SIZE_GIB} GiB, got {size_gib}")
    if size_gib == 0:
        _run("swapoff", SWAP_PATH, check=False)
        Path(SWAP_PATH).unlink(missing_ok=True)
        _set_fstab_entry(present=False)
    else:
        _create_and_enable(size_gib)
        _set_fstab_entry(present=True)
    return get_swap_status()


def ensure_swapfile(size_gib: int = DEFAULT_SWAP_SIZE_GIB, *, create_only: bool = True) -> SwapStatus:
    """Provision the swap file if the host has none. Root-only, idempotent.

    Used by fresh-host provisioning (ansible) and the v9 migration on existing
    hosts. With ``create_only`` (the default) an existing swap file is left at
    its current size so a re-provision or re-run never shrinks an owner-
    customized value; it still re-enables a present-but-swapped-off file and
    re-asserts the fstab entry so the file actually gets used.

    No-op inside a container: swap can't be enabled there and creating the
    backing file would just fill the container's disk, so this reports whatever
    swap the host already exposes without touching anything. This is why running
    the system-agent migrations inside a (test) container doesn't try to
    fallocate a multi-GiB file.
    """
    _require_root()
    if _running_in_container():
        return get_swap_status()
    if create_only and Path(SWAP_PATH).exists():
        _run("swapon", SWAP_PATH, check=False)
        _set_fstab_entry(present=True)
        return get_swap_status()
    return resize_swapfile(size_gib)
