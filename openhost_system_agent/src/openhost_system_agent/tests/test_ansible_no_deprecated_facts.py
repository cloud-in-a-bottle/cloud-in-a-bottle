"""Guard against re-introducing top-level injected ansible facts.

Ansible's ``INJECT_FACTS_AS_VARS`` default (``True``) auto-injects every
gathered fact as a top-level ``ansible_<fact>`` variable (e.g.
``ansible_architecture``, ``ansible_date_time``).  That default is deprecated
and is being removed in ansible-core 2.24, after which those references
silently resolve to *undefined*.  The supported spelling is the accessor form,
``ansible_facts['<fact>']``.

We don't pin ansible-core in this repo (it's installed via ``apt`` in
scripts/provision.sh and ``pipx`` in e2e, both unversioned), so we can't rely
on a fixed toolchain to warn us.  A test that actually ran a playbook and
scraped stderr for ``[DEPRECATION WARNING]`` would be worse than useless here:
these warnings only fire at task runtime against a real host (not in
``--syntax-check``), and any future ansible release could emit a brand-new,
unrelated deprecation that breaks the suite with no change on our side.

So instead this test statically enforces the one rule behind this class of
warning: no bare ``ansible_<fact>`` references anywhere under ``ansible/`` --
use ``ansible_facts[...]``.  It is deterministic, needs no ansible install or
host, and is immune to ansible version drift.
"""

from __future__ import annotations

import re
from pathlib import Path

# ``ansible_*`` variables that are connection/behavioral/runtime "magic" vars
# rather than gathered facts.  These are NOT injected by INJECT_FACTS_AS_VARS
# and are always safe as top-level references.  If a legitimate new one trips
# this test, add it here (and confirm it's genuinely a magic var, not a fact --
# facts appear under ``ansible_facts`` in ``ansible -m setup``).
_ALLOWED_MAGIC_VARS = frozenset(
    {
        # connection
        "ansible_connection",
        "ansible_host",
        "ansible_port",
        "ansible_user",
        "ansible_password",
        "ansible_ssh_host",
        "ansible_ssh_port",
        "ansible_ssh_user",
        "ansible_ssh_pass",
        "ansible_ssh_private_key_file",
        "ansible_ssh_common_args",
        "ansible_ssh_extra_args",
        "ansible_ssh_pipelining",
        "ansible_ssh_executable",
        "ansible_ssh_transfer_method",
        "ansible_sftp_extra_args",
        "ansible_scp_extra_args",
        "ansible_paramiko_pty",
        "ansible_pipelining",
        # privilege escalation
        "ansible_become",
        "ansible_become_method",
        "ansible_become_user",
        "ansible_become_pass",
        "ansible_become_password",
        "ansible_become_exe",
        "ansible_become_flags",
        # interpreter / shell
        "ansible_python_interpreter",
        "ansible_shell_type",
        "ansible_shell_executable",
        # runtime / play introspection
        "ansible_check_mode",
        "ansible_diff_mode",
        "ansible_verbosity",
        "ansible_forks",
        "ansible_version",
        "ansible_managed",
        "ansible_config_file",
        "ansible_playbook_python",
        "ansible_play_hosts",
        "ansible_play_hosts_all",
        "ansible_play_batch",
        "ansible_play_name",
        "ansible_play_role_names",
        "ansible_role_names",
        "ansible_role_name",
        "ansible_dependent_role_names",
        "ansible_parent_role_names",
        "ansible_parent_role_paths",
        "ansible_collection_name",
        "ansible_loop",
        "ansible_loop_var",
        "ansible_index_var",
        "ansible_search_path",
        "ansible_inventory_sources",
        "ansible_limit",
        "ansible_run_tags",
        "ansible_skip_tags",
        "ansible_module_name",
    }
)

# Matches ``ansible_foo`` / ``ansible_foo_bar`` tokens.  ``ansible_facts`` (the
# blessed accessor) is filtered out below rather than via the regex so the
# failure message can point specifically at the offending fact name.
_ANSIBLE_VAR_RE = re.compile(r"\bansible_[a-z0-9]+(?:_[a-z0-9]+)*\b")


def _repo_root() -> Path:
    """Walk up until we find the checkout root (the dir holding ``ansible/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ansible").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate repo root (no ansible/ + pyproject.toml above this test)")


def _ansible_source_files() -> list[Path]:
    root = _repo_root() / "ansible"
    # Playbooks/tasks (``.yml``/``.yaml``) and Jinja templates (``.j2``); the
    # ``template`` module injects facts the same way ``set_fact``/inline
    # templating does, so both surfaces are subject to the deprecation.
    files = [p for ext in ("*.yml", "*.yaml", "*.j2") for p in root.rglob(ext)]
    assert files, f"no ansible source files found under {root}"
    return files


def test_no_top_level_injected_facts() -> None:
    root = _repo_root()
    offenders: list[str] = []

    for path in _ansible_source_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for match in _ANSIBLE_VAR_RE.finditer(line):
                var = match.group(0)
                if var == "ansible_facts" or var in _ALLOWED_MAGIC_VARS:
                    continue
                rel = path.relative_to(root)
                offenders.append(f"  {rel}:{lineno}: {var}")

    assert not offenders, (
        "Top-level ansible fact references found. These break once "
        "INJECT_FACTS_AS_VARS defaults to False (ansible-core 2.24). "
        "Use ansible_facts['<name>'] instead:\n" + "\n".join(offenders)
    )
