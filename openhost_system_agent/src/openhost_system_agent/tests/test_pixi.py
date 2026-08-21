from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from openhost_system_agent import pixi

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestEnsurePixiVersion:
    def test_runs_self_update_as_host_when_root(self) -> None:
        with (
            patch("os.geteuid", return_value=0),
            patch("subprocess.run", return_value=_ok()) as mock_run,
        ):
            pixi.ensure_pixi_version()

        cmd = mock_run.call_args.args[0]
        # Must drop to the host user so self-update never leaves root-owned
        # files under /home/host/.pixi.
        assert cmd[:4] == ["sudo", "-u", pixi.HOST_USER, "-H"]
        assert cmd[4:] == [pixi.PIXI_BIN, "self-update", "--version", pixi.PIXI_VERSION]

    def test_runs_pixi_directly_when_not_root(self) -> None:
        with (
            patch("os.geteuid", return_value=1000),
            patch("subprocess.run", return_value=_ok()) as mock_run,
        ):
            pixi.ensure_pixi_version()

        cmd = mock_run.call_args.args[0]
        assert cmd == [pixi.PIXI_BIN, "self-update", "--version", pixi.PIXI_VERSION]

    def test_raises_on_failure(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with (
            patch("os.geteuid", return_value=0),
            patch("subprocess.run", return_value=failed),
        ):
            with pytest.raises(RuntimeError, match="self-update"):
                pixi.ensure_pixi_version()


def test_pinned_pixi_version_is_consistent_everywhere() -> None:
    """Every place that installs or requires pixi must name ``PIXI_VERSION``: a lock resolved by one
    version is rejected by another under ``--locked``, so drift breaks CI or deploy far from its cause."""
    pinned = pixi.PIXI_VERSION
    found: dict[str, set[str]] = {}

    # `curl | PIXI_VERSION=vX bash` (host provisioning) and setup-pixi's `pixi-version: vX` (CI).
    for relative in ("ansible/tasks/pixi.yml", ".github/workflows/ci.yml", ".github/workflows/e2e.yml"):
        text = (_REPO_ROOT / relative).read_text()
        found[relative] = set(re.findall(r"(?:PIXI_VERSION=|pixi-version:\s*)v([\d.]+)", text))

    # The manifest guard makes any other pixi fail loudly instead of silently resolving a bad lock.
    manifest = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    requires = manifest["tool"]["pixi"]["workspace"]["requires-pixi"]
    found["pyproject.toml"] = {requires.lstrip("=")}

    assert all(versions for versions in found.values()), f"no pixi version found in {found}"
    assert {v for versions in found.values() for v in versions} == {pinned}, (
        f"pixi version drifted from PIXI_VERSION={pinned}: {found}"
    )
