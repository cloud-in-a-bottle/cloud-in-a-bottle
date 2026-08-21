"""Ansible restates the `.local` rule in Jinja (group_vars/all.yml) because it renders the claim URL
before any router code runs; this pins that expression to `is_local_name`, the source of truth."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment
from jinja2 import StrictUndefined

from compute_space.core.domains import is_local_name

_ALL_YML = Path(__file__).parents[4] / "ansible" / "group_vars" / "all.yml"

_NAMES = (
    "myhost.local",
    "MyHost.LOCAL",
    "myhost.local:8080",
    "local",
    "host.example.com",
    "notlocal",
    "local.example.com",
    "",
)


def _ansible_expr(var: str) -> str:
    content = _ALL_YML.read_text()
    match = re.search(rf'^{var}: "(.+)"$', content, re.MULTILINE)
    assert match is not None, f"{var} not found in {_ALL_YML}"
    return match.group(1)


def test_ansible_mdns_mode_matches_is_local_name() -> None:
    env = Environment(undefined=StrictUndefined)
    domain_host_expr = _ansible_expr("domain_host")
    mdns_mode_expr = _ansible_expr("mdns_mode")
    for name in _NAMES:
        domain_host = env.from_string(domain_host_expr).render(domain=name)
        rendered = env.from_string(mdns_mode_expr).render(domain_host=domain_host)
        assert (rendered == "True") == is_local_name(name), name
