"""Render + assert invariants on the ``openhost.service`` systemd unit template.

The unit is a Jinja2 template (``ansible/templates/openhost.service.j2``) with a
single ``host_uid`` variable, installed verbatim by
``ansible/tasks/install_openhost_units.yml``. There is no ansible-side test
harness in this repo, so these tests render the template directly and pin the
behaviors we care about — chiefly the crash-restart policy, which is what keeps
an instance (and its own authoritative DNS) from staying dark after a transient
compute_space crash.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined

# tests/ -> compute_space/ -> src/ -> compute_space/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_TEMPLATE_DIR = _REPO_ROOT / "ansible" / "templates"
_TEMPLATE_NAME = "openhost.service.j2"

_FAKE_HOST_UID = "1001"


def _render_unit() -> str:
    # StrictUndefined so a forgotten variable raises instead of rendering blank —
    # a blank host_uid would silently produce a broken /run/user/ path.
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), undefined=StrictUndefined)
    return env.get_template(_TEMPLATE_NAME).render(host_uid=_FAKE_HOST_UID)


def _parse_unit(text: str) -> configparser.ConfigParser:
    # systemd unit files are INI-ish; configparser reads them well enough for
    # single-valued directives, which is all we assert here. allow_no_value for
    # comment-only lines; strict=False since systemd permits repeated keys.
    parser = configparser.ConfigParser(allow_no_value=True, strict=False, interpolation=None)
    # Preserve case: systemd directive names are case-sensitive (Restart, not restart).
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(text)
    return parser


@pytest.fixture(scope="module")
def unit_text() -> str:
    return _render_unit()


@pytest.fixture(scope="module")
def unit(unit_text: str) -> configparser.ConfigParser:
    return _parse_unit(unit_text)


def test_template_renders_with_host_uid(unit_text: str) -> None:
    # The one template variable must be substituted everywhere it appears.
    assert "{{" not in unit_text and "{%" not in unit_text
    assert f"user@{_FAKE_HOST_UID}.service" in unit_text
    assert f"/run/user/{_FAKE_HOST_UID}" in unit_text


def test_restart_policy_is_on_failure(unit: configparser.ConfigParser) -> None:
    # The crux of this change: a transient crash must auto-recover rather than
    # leave the instance (and its authoritative DNS) dark until a human reboots.
    assert unit.get("Service", "Restart") == "on-failure"


def test_update_flow_exit_42_still_restarts_cleanly(unit: configparser.ConfigParser) -> None:
    # The self-update flow exits 42 on purpose (updates.py RESTART_EXIT_CODE).
    # It must be both a forced-restart status (so Restart=on-failure honors it)
    # and a success status (so it never counts toward the crash burst limit).
    assert unit.get("Service", "RestartForceExitStatus") == "42"
    assert unit.get("Service", "SuccessExitStatus") == "42"


def test_crash_limiter_is_bounded(unit: configparser.ConfigParser) -> None:
    # StartLimit* must live in [Unit] (systemd ignores them in [Service]) and be
    # finite so a persistent cert-acquisition crash can hit an ACME endpoint at
    # most StartLimitBurst times per interval.
    burst = int(unit.get("Unit", "StartLimitBurst"))
    interval = int(unit.get("Unit", "StartLimitIntervalSec"))
    assert burst > 0
    assert interval > 0
    # Sanity bound: keep the worst-case ACME attempt rate low.
    assert burst <= 10


def test_restart_is_delayed(unit: configparser.ConfigParser) -> None:
    # A non-trivial RestartSec keeps a fast crash loop from spinning the CPU and
    # from racing ACME orders back-to-back within the burst window.
    assert int(unit.get("Service", "RestartSec")) >= 5


def test_stop_reaps_child_processes(unit: configparser.ConfigParser) -> None:
    # CoreDNS + Caddy run as in-process children; a bounded TimeoutStopSec ensures
    # systemd kills the whole cgroup on stop so they don't linger on :53/:80/:443.
    assert int(unit.get("Service", "TimeoutStopSec")) > 0
