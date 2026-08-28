"""OpenHost -> Cloud in a Bottle env-var rename.

Every OPENHOST_* variable is kept for backward compatibility and also
exposed under a BOTTLE_* twin.  These tests pin the alias helper that stamps
the BOTTLE_* twins onto the env passed into app containers.
"""

from __future__ import annotations

from compute_space.core.containers import add_bottle_env_aliases


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
