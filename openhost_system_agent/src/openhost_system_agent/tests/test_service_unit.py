from __future__ import annotations

from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined

from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_EXEC_START_PRE
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT
from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_SCRIPT_PATH
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit
from openhost_system_agent.migrations.versions.v0010_journal_read_for_oom import JOURNAL_READ_DROPIN


def _effective_directives(unit_text: str) -> list[str]:
    """Non-comment, non-blank lines — the directives systemd actually acts on."""
    out: list[str] = []
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def test_ansible_template_and_builder_agree_on_directives() -> None:
    """The ansible template (fresh provisioning) and build_openhost_service_unit
    (migrations) must produce the same systemd directives so a host looks the
    same however it was set up. Comments may differ; directives may not."""
    repo_root = Path(__file__).resolve().parents[4]
    env = Environment(
        loader=FileSystemLoader(str(repo_root / "ansible" / "templates")),
        undefined=StrictUndefined,
    )
    rendered_j2 = env.get_template("openhost.service.j2").render(host_uid="1001")
    assert _effective_directives(rendered_j2) == _effective_directives(build_openhost_service_unit(1001))


def test_journal_read_dropin_matches_ansible_copy_byte_for_byte() -> None:
    """The migration-written drop-in and the ansible-installed drop-in must be
    identical so a host grants journal access the same way however it was set up.
    The group lives in this additive drop-in, not the main unit, so no existing
    migration (or the shared builder) has to change to add it."""
    repo_root = Path(__file__).resolve().parents[4]
    ansible_copy = repo_root / "ansible" / "files" / "openhost.service.d" / "10-journal-read.conf"
    assert ansible_copy.read_text() == JOURNAL_READ_DROPIN
    assert "SupplementaryGroups=systemd-journal\n" in JOURNAL_READ_DROPIN


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

    def test_sets_both_router_config_env_names(self) -> None:
        # OpenHost -> Cloud in a Bottle rename: the unit must set the config path
        # under both the new BOTTLE_ name and the legacy OPENHOST_ name so the
        # router resolves it whichever it reads (works across a version-skewed
        # self-update). The ansible template carries the same pair (enforced by
        # test_ansible_template_and_builder_agree_on_directives).
        unit = build_openhost_service_unit(1001)
        cfg = "/home/host/.openhost/local_compute_space/config.toml"
        assert f"Environment=BOTTLE_ROUTER_CONFIG={cfg}\n" in unit
        assert f"Environment=OPENHOST_ROUTER_CONFIG={cfg}\n" in unit

    def test_host_uid_is_substituted(self) -> None:
        unit = build_openhost_service_unit(4242)
        assert "XDG_RUNTIME_DIR=/run/user/4242" in unit
        assert "user@4242.service" in unit

    def test_restarts_on_failure_with_a_bounded_crash_limiter(self) -> None:
        unit = build_openhost_service_unit(1001)
        # Auto-restart on crash so a transient compute_space failure doesn't take
        # the instance (and its own authoritative DNS) dark until a human reboots.
        assert "Restart=on-failure\n" in unit
        assert "Restart=no" not in unit
        # StartLimit* MUST live in [Unit]; systemd ignores them in [Service].
        assert unit.index("StartLimitBurst=5") < unit.index("[Service]")
        assert "StartLimitIntervalSec=1800\n" in unit
        # A non-trivial backoff so a crash loop can't spin the CPU or race ACME.
        assert "RestartSec=30\n" in unit

    def test_self_update_exit_42_is_a_restarting_success(self) -> None:
        unit = build_openhost_service_unit(1001)
        # 42 (updates.py RESTART_EXIT_CODE) must force a restart *and* count as a
        # success, so update restarts work and don't consume the crash burst.
        assert "RestartForceExitStatus=42\n" in unit
        assert "SuccessExitStatus=42\n" in unit


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
