"""OpenHost -> Cloud in a Bottle env-var rename.

Every OPENHOST_* variable is kept for backward compatibility and also
exposed under a BOTTLE_* twin.  These tests pin the alias helper and the
daemon config loader's dual-prefix precedence.
"""

from __future__ import annotations

import pytest

from compute_space.config import load_config
from compute_space.core.data import add_bottle_env_aliases


def test_alias_adds_bottle_twin_for_each_openhost_var() -> None:
    aliased = add_bottle_env_aliases({"OPENHOST_APP_NAME": "myapp", "OPENHOST_APP_ID": "abc"})
    # Legacy names preserved for compatibility.
    assert aliased["OPENHOST_APP_NAME"] == "myapp"
    assert aliased["OPENHOST_APP_ID"] == "abc"
    # New names carry the same values.
    assert aliased["BOTTLE_APP_NAME"] == "myapp"
    assert aliased["BOTTLE_APP_ID"] == "abc"


def test_alias_leaves_non_openhost_vars_untouched() -> None:
    aliased = add_bottle_env_aliases({"PATH": "/usr/bin", "OPENHOST_APP_NAME": "myapp"})
    assert aliased["PATH"] == "/usr/bin"
    assert "BOTTLE_PATH" not in aliased


def test_alias_does_not_clobber_existing_bottle_value() -> None:
    # An explicit new-style value wins over the auto-generated alias.
    aliased = add_bottle_env_aliases({"OPENHOST_APP_NAME": "legacy", "BOTTLE_APP_NAME": "explicit"})
    assert aliased["BOTTLE_APP_NAME"] == "explicit"
    assert aliased["OPENHOST_APP_NAME"] == "legacy"


def test_alias_is_pure() -> None:
    original = {"OPENHOST_APP_NAME": "myapp"}
    add_bottle_env_aliases(original)
    assert original == {"OPENHOST_APP_NAME": "myapp"}


def test_load_config_prefers_bottle_over_openhost(monkeypatch: pytest.MonkeyPatch) -> None:
    # zone_domain is required and has no default; supply it via the legacy name.
    monkeypatch.setenv("OPENHOST_ZONE_DOMAIN", "example.com")

    monkeypatch.setenv("OPENHOST_PORT", "9001")
    assert load_config().port == 9001, "legacy OPENHOST_ override still works"

    monkeypatch.setenv("BOTTLE_PORT", "9002")
    assert load_config().port == 9002, "BOTTLE_ wins when both are set"


def test_load_config_reads_required_field_from_bottle_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTLE_ZONE_DOMAIN", "bottle.example")
    assert load_config().zone_domain == "bottle.example"
