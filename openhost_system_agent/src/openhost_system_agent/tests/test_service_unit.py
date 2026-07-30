from __future__ import annotations

from pathlib import Path

from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_EXEC_START_PRE
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class TestOpenhostServiceUnit:
    def test_reclaim_execstartpre_runs_script_as_root_best_effort(self) -> None:
        unit = build_openhost_service_unit(1001)
        # Runs the standalone script (no inline $VAR for systemd to expand) as
        # root (`+`) and best-effort (`-`, so a chown failure can't block the
        # service the failsafe protects).
        assert f"ExecStartPre=-+{RECLAIM_SCRIPT_PATH}\n" in unit
        # Must not embed a shell $VAR, which systemd would substitute from the
        # unit environment before /bin/sh sees it.
        assert "$" not in RECLAIM_EXEC_START_PRE

    def test_reclaim_runs_before_execstart(self) -> None:
        unit = build_openhost_service_unit(1001)
        assert unit.index("ExecStartPre=-+") < unit.index("ExecStart=/home/host/.pixi/bin/pixi run")

    def test_uses_the_shared_reclaim_constant(self) -> None:
        # The exact ExecStartPre line is a module constant so migrations that
        # rewrite the unit stay byte-identical with the baseline.
        assert RECLAIM_EXEC_START_PRE in build_openhost_service_unit(1234)

    def test_host_uid_is_substituted(self) -> None:
        unit = build_openhost_service_unit(4242)
        assert "XDG_RUNTIME_DIR=/run/user/4242" in unit
        assert "user@4242.service" in unit


class TestReclaimScript:
    def test_script_chowns_host_trees_to_host(self) -> None:
        assert "chown -Rh host:host" in RECLAIM_SCRIPT
        # The repo tree (covers its .pixi env, .git, working tree) and the
        # standalone pixi tree (binary + caches).
        assert "/home/host/openhost" in RECLAIM_SCRIPT
        assert "/home/host/.pixi" in RECLAIM_SCRIPT
        assert RECLAIM_SCRIPT.startswith("#!/bin/sh")

    def test_matches_ansible_copy_byte_for_byte(self) -> None:
        # The migration-written script and the ansible-copied script must be
        # identical so a host looks the same however it was set up.
        repo_root = Path(__file__).resolve().parents[4]
        ansible_copy = repo_root / "ansible" / "files" / "openhost-reclaim-pixi"
        assert ansible_copy.read_text() == RECLAIM_SCRIPT
