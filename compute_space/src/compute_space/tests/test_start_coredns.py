from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.web import start as start_mod


def _cfg(tmp_path: Path) -> DefaultConfig:
    return DefaultConfig(data_root_dir=str(tmp_path))


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr

    def getsockname(self) -> tuple[str, int]:
        return ("10.0.0.5", 12345)


def test_dns_bind_ip_uses_default_route_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(start_mod.socket, "socket", lambda *args: fake_socket)

    assert start_mod._dns_bind_ip("203.0.113.10") == "10.0.0.5"
    assert fake_socket.addr == ("8.8.8.8", 80)


def test_dns_bind_ip_falls_back_to_the_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(*args: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(start_mod.socket, "socket", raise_os_error)

    assert start_mod._dns_bind_ip("203.0.113.10") == "203.0.113.10"


def test_ensure_coredns_uses_path_binary_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_mod.shutil, "which", lambda name: "/usr/local/bin/coredns")
    installs: list[object] = []
    monkeypatch.setattr(start_mod, "install_pinned_binary", lambda *a, **k: installs.append(a))

    result = start_mod._ensure_coredns_binary(_cfg(tmp_path))

    assert result == "/usr/local/bin/coredns"
    assert installs == []  # provisioned binary on PATH -> no self-heal download


def test_ensure_coredns_self_heals_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_mod.shutil, "which", lambda name: None)
    installed: dict[str, object] = {}

    def fake_install(binary: object, dest: str) -> None:
        installed["binary"] = binary
        installed["dest"] = dest

    monkeypatch.setattr(start_mod, "install_pinned_binary", fake_install)

    cfg = _cfg(tmp_path)
    result = start_mod._ensure_coredns_binary(cfg)

    expected = str(cfg.openhost_data_path / "coredns")
    assert result == expected
    assert installed["dest"] == expected
    assert installed["binary"] == get_pinned_binary("coredns")
