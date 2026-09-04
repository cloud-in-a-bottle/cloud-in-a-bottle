from __future__ import annotations

import socket

import pytest

import compute_space.core.ip as ip_mod
from compute_space.core.ip import default_outbound_interface_ipv4
from compute_space.core.ip import infer_inbound_ipv4
from compute_space.core.ip import is_bindable
from compute_space.core.ip import source_ip_for


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr

    def getsockname(self) -> tuple[str, int]:
        return ("10.0.0.5", 12345)


def _raise_os_error(*args: object) -> object:
    raise OSError("network unreachable")


def test_source_ip_for_reads_the_route_source_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *args: fake_socket)

    assert source_ip_for("192.168.1.1") == "10.0.0.5"
    assert fake_socket.addr == ("192.168.1.1", 9)  # discard port; the connect is only a route lookup


def test_source_ip_for_is_none_when_there_is_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _raise_os_error)

    assert source_ip_for("192.168.1.1") is None


def test_default_outbound_interface_ipv4_probes_an_off_link_address(monkeypatch: pytest.MonkeyPatch) -> None:
    # The destination stands in for "the internet" -- any off-link address routes the same way.
    fake_socket = _FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *args: fake_socket)

    assert default_outbound_interface_ipv4() == "10.0.0.5"
    assert fake_socket.addr[0] == "8.8.8.8"


def test_infer_inbound_ipv4_binds_the_public_ip_when_it_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bare metal: the address is on a NIC, so queries arrive on it directly.
    monkeypatch.setattr(ip_mod, "is_bindable", lambda ip: ip == "203.0.113.10")

    assert infer_inbound_ipv4("203.0.113.10") == "203.0.113.10"


def test_infer_inbound_ipv4_falls_back_to_the_outbound_interface_when_natted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AWS/GCP or a home router: the public address isn't ours, so the best guess is that inbound
    # arrives on the same interface outbound leaves from.
    monkeypatch.setattr(ip_mod, "is_bindable", lambda ip: False)
    monkeypatch.setattr(ip_mod, "default_outbound_interface_ipv4", lambda: "10.0.1.23")

    assert infer_inbound_ipv4("203.0.113.10") == "10.0.1.23"


def test_infer_inbound_ipv4_is_none_when_nothing_can_be_determined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ip_mod, "is_bindable", lambda ip: False)
    monkeypatch.setattr(socket, "socket", _raise_os_error)

    assert infer_inbound_ipv4("203.0.113.10") is None


def test_is_bindable_distinguishes_our_addresses_from_everything_else() -> None:
    # The real syscall: TEST-NET-3 is never assigned locally, loopback always is.
    assert is_bindable("203.0.113.10") is False
    assert is_bindable("127.0.0.1") is True
