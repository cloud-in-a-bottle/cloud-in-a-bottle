import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from openhost_system_agent.migrations.helpers import run_apt_get
from openhost_system_agent.migrations.versions import v0002_baseline as baseline


def test_baseline_package_commands_are_unattended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apt = tmp_path / "apt-get"
    apt.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert os.environ['DEBIAN_FRONTEND'] == 'noninteractive'\n"
        "assert os.environ['UCF_FORCE_CONFFOLD'] == '1'\n"
        "assert 'UCF_FORCE_CONFFNEW' not in os.environ\n"
        "assert 'Dpkg::Options::=--force-confdef' in sys.argv\n"
        "assert 'Dpkg::Options::=--force-confold' in sys.argv\n"
        "assert sys.stdin.read() == ''\n"
        "with open(os.environ['APT_CALLS'], 'a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    apt.chmod(0o755)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("APT_CALLS", str(calls))
    monkeypatch.setenv("DEBIAN_FRONTEND", "dialog")
    monkeypatch.setenv("UCF_FORCE_CONFFNEW", "1")
    # Keep the parent's input pipe open, as with a remote deployment command.
    # Package operations must use /dev/null instead of waiting on this pipe.
    read_fd, write_fd = os.pipe()
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from openhost_system_agent.migrations.versions.v0002_baseline import Migration0002Baseline; Migration0002Baseline()._apt_packages()",
            ],
            stdin=read_fd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    commands = calls.read_text().splitlines()
    assert len(commands) == 3
    assert "update -qq" in commands[0]
    assert "install -y -qq git curl wget openssh-server" in commands[1]
    assert "remove -y -qq podman crun" in commands[2]
    assert os.environ["DEBIAN_FRONTEND"] == "dialog"


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(100, "apt-get"), subprocess.TimeoutExpired("apt-get", 300)]
)
def test_package_failures_are_reported(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    run = Mock(side_effect=error)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(type(error)):
        run_apt_get("install", "-y", "openssh-server")
    assert run.call_args.kwargs["timeout"] == 300
    assert run.call_args.kwargs["check"] is True


def test_ssh_hardening_handles_an_inactive_service(monkeypatch: pytest.MonkeyPatch) -> None:
    set_option = Mock()
    run = Mock()
    monkeypatch.setattr(baseline, "set_sshd_option", set_option)
    monkeypatch.setattr(baseline, "run", run)
    baseline.Migration0002Baseline()._harden_ssh()
    assert [call.args for call in set_option.call_args_list] == [
        ("PasswordAuthentication", "no"),
        ("KbdInteractiveAuthentication", "no"),
        ("PermitRootLogin", "prohibit-password"),
    ]
    run.assert_called_once_with("systemctl", "reload-or-restart", "ssh")
