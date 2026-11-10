import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from openhost_system_agent.migrations import helpers


def test_standalone_migrations_use_router_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[openhost]\ndata_root_dir = "/data/custom root"\n')
    monkeypatch.delenv("OPENHOST_DATA_DIR", raising=False)
    monkeypatch.delenv("OPENHOST_DATA_ROOT_DIR", raising=False)
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config))
    helpers.configure_migration_data_dir()
    assert os.environ["OPENHOST_DATA_DIR"] == "/data/custom root/persistent_data/openhost"


def test_explicit_migration_directory_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text("not valid TOML")
    monkeypatch.setenv("OPENHOST_DATA_DIR", "/data/explicit")
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config))
    helpers.configure_migration_data_dir()
    assert os.environ["OPENHOST_DATA_DIR"] == "/data/explicit"


def test_explicit_missing_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHOST_DATA_DIR", raising=False)
    monkeypatch.delenv("OPENHOST_DATA_ROOT_DIR", raising=False)
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        helpers.configure_migration_data_dir()
    assert "OPENHOST_DATA_DIR" not in os.environ


def test_data_root_environment_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[openhost]\ndata_root_dir = "/data/from-file"\n')
    monkeypatch.delenv("OPENHOST_DATA_DIR", raising=False)
    monkeypatch.setenv("OPENHOST_DATA_ROOT_DIR", "/data/from-env")
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config))
    helpers.configure_migration_data_dir()
    assert os.environ["OPENHOST_DATA_DIR"] == "/data/from-env/persistent_data/openhost"


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_router_stopped_accepts_quiescent_states(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, state + "\n"))
    monkeypatch.setattr(subprocess, "run", run)
    helpers.require_router_stopped()
    assert run.call_args.kwargs["check"] is True


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading", "", "unknown"])
def test_router_stopped_rejects_other_states(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, state)))
    with pytest.raises(RuntimeError, match="Stop openhost.service"):
        helpers.require_router_stopped()


def test_router_stopped_does_not_treat_failed_query_as_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(side_effect=subprocess.CalledProcessError(1, "systemctl", output="inactive"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        helpers.require_router_stopped()


@pytest.mark.parametrize("recorded_ids", [[], ["recorded", "unrecorded"]])
def test_stop_includes_unrecorded_managed_containers(monkeypatch: pytest.MonkeyPatch, recorded_ids: list[str]) -> None:
    monkeypatch.setattr(helpers, "get_host_uid", lambda: 4242)
    run = Mock(side_effect=[subprocess.CompletedProcess([], 0, "unrecorded\n"), subprocess.CompletedProcess([], 0)])
    monkeypatch.setattr(subprocess, "run", run)
    helpers.stop_app_containers(recorded_ids)
    listing, stopping = run.call_args_list
    assert listing.args[0][-6:] == ["ps", "--all", "--filter", "name=^openhost-", "--format", "{{.ID}}"]
    command = stopping.args[0]
    assert command[command.index("--time") :] == ["--time", "10", "--", *dict.fromkeys([*recorded_ids, "unrecorded"])]
    assert stopping.kwargs["check"] is True
    assert "XDG_RUNTIME_DIR=/run/user/4242" in command
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/4242/bus" in command


def test_stop_does_not_issue_empty_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helpers, "get_host_uid", lambda: 4242)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, ""))
    monkeypatch.setattr(subprocess, "run", run)
    helpers.stop_app_containers([])
    run.assert_called_once()


@pytest.mark.parametrize("failed_call", ["list", "stop"])
def test_container_query_and_stop_failures_propagate(monkeypatch: pytest.MonkeyPatch, failed_call: str) -> None:
    monkeypatch.setattr(helpers, "get_host_uid", lambda: 4242)
    error = subprocess.CalledProcessError(1, "podman")
    results = [error] if failed_call == "list" else [subprocess.CompletedProcess([], 0, "container"), error]
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=results))
    with pytest.raises(subprocess.CalledProcessError):
        helpers.stop_app_containers([])
