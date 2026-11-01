import random
import socket
import sqlite3

import attr

from compute_space.core.manifest import PortMapping
from compute_space.db import get_db


def allocate_port(range_start: int = 9000, range_end: int = 9999) -> int:
    db = get_db()
    used_ports: set[int] = {row["local_port"] for row in db.execute("SELECT local_port FROM apps").fetchall()}
    used_ports |= {row["host_port"] for row in db.execute("SELECT host_port FROM app_port_mappings").fetchall()}

    for port in range(range_start, range_end + 1):
        if port in used_ports:
            continue
        if _port_is_bindable(port):
            return port

    raise RuntimeError(f"No free ports in range {range_start}-{range_end}")


def check_port_available(
    port: int, db: sqlite3.Connection, exclude_app_id: str | None = None
) -> tuple[bool, dict[str, str] | None]:
    """Check if a host port is available for use.

    Tests both TCP and UDP bind on 0.0.0.0, and checks DB for existing allocations.
    Returns (available, used_by) where used_by is a dict with structured fields.

    exclude_app_id: skip DB rows belonging to this app (used during reload/sync to
    avoid conflicting with the app's own existing mappings).
    """
    # Check DB: main app ports
    row = db.execute("SELECT name, app_id FROM apps WHERE local_port = ?", (port,)).fetchone()
    if row and row["app_id"] != exclude_app_id:
        return False, {"app_name": row["name"], "type": "main_port"}

    # Check DB: port mappings
    if exclude_app_id:
        row = db.execute(
            """SELECT a.name AS app_name, m.label
               FROM app_port_mappings m
               JOIN apps a ON a.app_id = m.app_id
               WHERE m.host_port = ? AND m.app_id != ?""",
            (port, exclude_app_id),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT a.name AS app_name, m.label
               FROM app_port_mappings m
               JOIN apps a ON a.app_id = m.app_id
               WHERE m.host_port = ?""",
            (port,),
        ).fetchone()
    if row:
        return False, {"app_name": row["app_name"], "label": row["label"], "type": "port_mapping"}

    # During a reload (exclude_app_id set) the app's own old container may
    # still be listening on the very host ports we are about to re-hand to
    # the new container. run_container removes the old container before
    # binding the new one, so a failed live bind on one of the app's own
    # already-reserved ports is a *self* conflict that will resolve on its
    # own -- not a real conflict. Without this, every reload of a running
    # multi-port app (e.g. rustdesk holding 21115-21119) self-conflicts and
    # lands in `error: Port N ... already in use by host-level service`.
    # This mirrors the DB-row exclusion above for the live-bind test.
    if exclude_app_id and _port_reserved_by_app(port, db, exclude_app_id):
        return True, None

    if not _port_is_bindable(port):
        return False, {"type": "host_service"}

    return True, None


def _port_reserved_by_app(port: int, db: sqlite3.Connection, app_id: str) -> bool:
    """True if ``port`` is one of ``app_id``'s own reserved host ports.

    That is the app's main ``local_port`` or any ``host_port`` in its
    ``app_port_mappings`` rows. Used to skip the live-bind test during
    reload: the app's own old container may still be holding these ports,
    but it is removed before the new container binds them, so a failed bind
    here is a self-conflict, not a real one.
    """
    if db.execute(
        "SELECT 1 FROM apps WHERE app_id = ? AND local_port = ?", (app_id, port)
    ).fetchone():
        return True
    return bool(
        db.execute(
            "SELECT 1 FROM app_port_mappings WHERE app_id = ? AND host_port = ?", (app_id, port)
        ).fetchone()
    )


def _format_used_by(used_by: dict[str, str] | None) -> str:
    """Format a used_by dict into a human-readable string for error messages."""
    if used_by is None:
        return "unknown"
    if used_by["type"] == "main_port":
        return f"app '{used_by['app_name']}' (main port)"
    if used_by["type"] == "port_mapping":
        return f"app '{used_by['app_name']}' (port mapping '{used_by['label']}')"
    return "host-level service"


def resolve_port_mappings(
    mappings: list[PortMapping],
    db: sqlite3.Connection,
    range_start: int = 9000,
    range_end: int = 9999,
    exclude_app_id: str | None = None,
) -> list[PortMapping]:
    """Resolve port mappings: auto-assign host_port=0 entries, validate fixed ones.

    Returns new list of PortMapping with all host_ports resolved to actual values.
    Raises RuntimeError on conflicts or exhaustion.

    exclude_app_id: passed to check_port_available to ignore this app's own DB rows
    (used during reload/sync).
    """
    # Collect ports already claimed within this batch
    claimed: set[int] = set()
    resolved: list[PortMapping] = []

    # First pass: validate explicitly set ports
    for pm in mappings:
        if pm.host_port != 0:
            available, used_by = check_port_available(pm.host_port, db, exclude_app_id=exclude_app_id)
            if not available:
                owner = _format_used_by(used_by)
                raise RuntimeError(f"Port {pm.host_port} for '{pm.label}' is already in use by {owner}")
            if pm.host_port in claimed:
                raise RuntimeError(f"Port {pm.host_port} requested by multiple mappings in this deploy")
            claimed.add(pm.host_port)
            resolved.append(pm)

    # Second pass: auto-assign for host_port=0
    for pm in mappings:
        if pm.host_port == 0:
            assigned = _find_free_host_port(range_start, range_end, db, claimed, exclude_app_id=exclude_app_id)
            claimed.add(assigned)
            resolved.append(attr.evolve(pm, host_port=assigned))

    return resolved


def _find_free_host_port(
    range_start: int, range_end: int, db: sqlite3.Connection, exclude: set[int], exclude_app_id: str | None = None
) -> int:
    """Find a free host port in the given range, excluding already-claimed ports."""
    used_ports: set[int] = {row["local_port"] for row in db.execute("SELECT local_port FROM apps").fetchall()}
    if exclude_app_id:
        used_ports |= {
            row["host_port"]
            for row in db.execute(
                "SELECT host_port FROM app_port_mappings WHERE app_id != ?", (exclude_app_id,)
            ).fetchall()
        }
    else:
        used_ports |= {row["host_port"] for row in db.execute("SELECT host_port FROM app_port_mappings").fetchall()}
    used_ports |= exclude

    candidates = list(range(range_start, range_end + 1))
    random.shuffle(candidates)
    for port in candidates:
        if port in used_ports:
            continue
        if _port_is_bindable(port):
            return port

    raise RuntimeError(f"No free ports in range {range_start}-{range_end} for auto-assignment")


def _port_is_bindable(port: int) -> bool:
    """Check if a port is bindable on 0.0.0.0 for both TCP and UDP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", port))
    except OSError:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True
