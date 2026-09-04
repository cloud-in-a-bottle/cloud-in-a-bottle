from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.pinned_binary import get_pinned_binary
from compute_space.web import start as start_mod


def _cfg(tmp_path: Path) -> DefaultConfig:
    return DefaultConfig(data_root_dir=str(tmp_path))


def test_hairpin_gateway_is_none_when_the_interface_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # openhost0 only exists on provisioned hosts; asking CoreDNS to bind an address that isn't
    # there stops it starting at all, so the view has to be dropped instead.
    monkeypatch.setattr(start_mod, "is_bindable", lambda ip: False)

    assert start_mod._hairpin_gateway_ip() is None


def test_hairpin_gateway_is_the_container_gateway_when_bindable(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[str] = []
    monkeypatch.setattr(start_mod, "is_bindable", lambda ip: probed.append(ip) or True)

    assert start_mod._hairpin_gateway_ip() == CONTAINER_GATEWAY_IP
    assert probed == [CONTAINER_GATEWAY_IP]


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
