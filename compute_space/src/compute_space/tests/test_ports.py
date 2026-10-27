"""Unit tests for port allocation and availability checking."""

import errno
import socket
import sqlite3
import sys
from collections.abc import Iterator
from unittest import mock

import pytest

from compute_space.core.app_id import new_app_id
from compute_space.core.manifest import PortMapping
from compute_space.core.ports import check_port_available
from compute_space.core.ports import resolve_port_mappings


@pytest.fixture
def db():
    """In-memory SQLite DB with the required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE apps (
            app_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            local_port INTEGER NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """CREATE TABLE app_port_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            label TEXT NOT NULL,
            container_port INTEGER NOT NULL,
            host_port INTEGER NOT NULL,
            UNIQUE(app_id, label)
        )"""
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def unused_port() -> int:
    """Ask the OS for a port unused by both TCP and UDP."""
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("0.0.0.0", 0))
            port = tcp.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("0.0.0.0", port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    continue
            return port
    pytest.fail("Could not obtain an unused TCP/UDP port")


@pytest.fixture
def time_wait_port(unused_port: int) -> int:
    if sys.platform != "linux":
        pytest.skip("Exercises Linux TIME_WAIT rebinding semantics")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(5)
        listener.bind(("0.0.0.0", unused_port))
        listener.listen(1)
        with socket.create_connection(("127.0.0.1", unused_port), timeout=5) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(5)
                # The server sends FIN first. Observe both FINs before closing so
                # the server's port enters TIME_WAIT without a timing-dependent sleep.
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert accepted.recv(1) == b""
    return unused_port


def _seed(db, name: str, local_port: int) -> str:
    """Insert an app row and return its app_id."""
    app_id = new_app_id()
    db.execute("INSERT INTO apps (app_id, name, local_port) VALUES (?, ?, ?)", (app_id, name, local_port))
    db.commit()
    return app_id


class TestCheckPortAvailable:
    def test_free_port_is_available(self, db, unused_port):
        assert check_port_available(unused_port, db) == (True, None)

    def test_time_wait_is_available(self, db, time_wait_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as exclusive:
            with pytest.raises(OSError) as exc_info:
                exclusive.bind(("0.0.0.0", time_wait_port))
            assert exc_info.value.errno == errno.EADDRINUSE

        # A real replacement service can immediately bind AND listen.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind(("0.0.0.0", time_wait_port))
            replacement.listen(1)

        assert check_port_available(time_wait_port, db) == (True, None)

    @pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1"])
    @pytest.mark.parametrize("reuse", [False, True])
    def test_active_tcp_listener_is_unavailable(self, db, unused_port, address, reuse):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse)
            listener.bind((address, unused_port))
            listener.listen(1)
            assert check_port_available(unused_port, db) == (False, {"type": "host_service"})
            assert check_port_available(unused_port, db, exclude_app_id=new_app_id()) == (
                False,
                {"type": "host_service"},
            )
        assert check_port_available(unused_port, db) == (True, None)

    @pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1"])
    @pytest.mark.parametrize("reuse", [False, True])
    def test_active_udp_socket_is_unavailable(self, db, unused_port, address, reuse):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse)
            udp.bind((address, unused_port))
            # Establish that only UDP is occupied, so this exercises the UDP probe.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
                tcp.bind(("0.0.0.0", unused_port))
            assert check_port_available(unused_port, db) == (False, {"type": "host_service"})
        assert check_port_available(unused_port, db) == (True, None)

    @pytest.mark.parametrize("owner_type", ["main_port", "port_mapping"])
    def test_time_wait_preserves_db_ownership(self, db, time_wait_port, unused_tcp_port, owner_type):
        main_port = time_wait_port if owner_type == "main_port" else unused_tcp_port
        app_id = _seed(db, "myapp", main_port)
        expected_owner = {"app_name": "myapp", "type": owner_type}
        mapping = PortMapping(label="gemini", container_port=time_wait_port, host_port=time_wait_port)
        if owner_type == "port_mapping":
            db.execute(
                "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES (?, ?, ?, ?)",
                (app_id, mapping.label, mapping.container_port, mapping.host_port),
            )
            db.commit()
            expected_owner["label"] = mapping.label

        assert check_port_available(time_wait_port, db) == (False, expected_owner)
        assert check_port_available(time_wait_port, db, exclude_app_id=new_app_id()) == (False, expected_owner)
        assert check_port_available(time_wait_port, db, exclude_app_id=app_id) == (True, None)
        if owner_type == "port_mapping":
            assert resolve_port_mappings([mapping], db, exclude_app_id=app_id) == [mapping]

    def test_port_used_by_app_main(self, db):
        _seed(db, "myapp", 9500)
        available, used_by = check_port_available(9500, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["type"] == "main_port"

    def test_port_used_by_mapping(self, db):
        app_id = _seed(db, "myapp", 9500)
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) "
            "VALUES (?, 'metrics', 9090, 9600)",
            (app_id,),
        )
        db.commit()
        available, used_by = check_port_available(9600, db)
        assert available is False
        assert used_by["app_name"] == "myapp"
        assert used_by["label"] == "metrics"
        assert used_by["type"] == "port_mapping"

    def test_exclude_app_skips_own_main_port(self, db):
        app_id = _seed(db, "myapp", 9500)
        available, used_by = check_port_available(9500, db, exclude_app_id=app_id)
        assert available is True

    def test_exclude_app_skips_own_mapping(self, db):
        app_id = _seed(db, "myapp", 9500)
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) "
            "VALUES (?, 'metrics', 9090, 9600)",
            (app_id,),
        )
        db.commit()
        available, used_by = check_port_available(9600, db, exclude_app_id=app_id)
        assert available is True

    def test_exclude_app_still_blocks_other_app(self, db):
        _seed(db, "other", 9500)
        available, used_by = check_port_available(9500, db, exclude_app_id=new_app_id())
        assert available is False


@pytest.fixture
def _always_bindable() -> Iterator[None]:
    """Pretend every port is OS-bindable so resolve_port_mappings tests
    don't depend on what's actually bound on the CI runner.

    Without this mock, resolve_port_mappings tests that hardcode
    high ports (e.g., 59200) can fail intermittently on shared
    CI runners where some unrelated process — kernel-allocated
    ephemeral source ports, runner-installed services, etc. —
    happens to occupy the chosen number.  The tests want to
    exercise resolve_port_mappings' DB-bookkeeping logic, not
    its OS-side bindability check, so mocking _port_is_bindable
    out is the right scope of fix.
    """
    with mock.patch("compute_space.core.ports._port_is_bindable", return_value=True):
        yield


class TestResolvePortMappings:
    @pytest.fixture(autouse=True)
    def _bindable(self, _always_bindable: None) -> None:
        """Apply the bindable-port mock to every test in this class."""
        return None

    def test_fixed_ports_pass_through(self, db):
        mappings = [PortMapping(label="web", container_port=80, host_port=59200)]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 1
        assert resolved[0].host_port == 59200

    def test_auto_assign_picks_free_port(self, db):
        mappings = [PortMapping(label="auto", container_port=3000, host_port=0)]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 1
        assert 59200 <= resolved[0].host_port <= 59300

    def test_conflict_with_db_raises(self, db):
        _seed(db, "other", 9500)
        mappings = [PortMapping(label="conflict", container_port=80, host_port=9500)]
        with pytest.raises(RuntimeError, match="already in use"):
            resolve_port_mappings(mappings, db)

    def test_duplicate_port_in_batch_raises(self, db):
        mappings = [
            PortMapping(label="a", container_port=80, host_port=59200),
            PortMapping(label="b", container_port=81, host_port=59200),
        ]
        with pytest.raises(RuntimeError, match="multiple mappings"):
            resolve_port_mappings(mappings, db, 59200, 59300)

    def test_mixed_fixed_and_auto(self, db):
        mappings = [
            PortMapping(label="fixed", container_port=80, host_port=59200),
            PortMapping(label="auto", container_port=3000, host_port=0),
        ]
        resolved = resolve_port_mappings(mappings, db, 59200, 59300)
        assert len(resolved) == 2
        ports = {r.label: r.host_port for r in resolved}
        assert ports["fixed"] == 59200
        assert ports["auto"] != 59200  # should pick different port
        assert 59200 <= ports["auto"] <= 59300
