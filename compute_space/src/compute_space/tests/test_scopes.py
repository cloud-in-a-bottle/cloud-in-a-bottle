"""Unit tests for the API-token scope vocabulary (core/auth/scopes.py).

These are pure-function tests — parsing, serializing, membership, and request
validation. The guard-level enforcement (that a scoped token actually gets 401
on an out-of-scope route) lives in test_token_scope_enforcement.py.
"""

from __future__ import annotations

import pytest

from compute_space.core.auth.scopes import ALL_SCOPES
from compute_space.core.auth.scopes import APPS_MANAGE
from compute_space.core.auth.scopes import APPS_READ
from compute_space.core.auth.scopes import OWNER
from compute_space.core.auth.scopes import OWNER_EQUIVALENT_SCOPES
from compute_space.core.auth.scopes import SCOPE_CATALOG
from compute_space.core.auth.scopes import TOKENS_MANAGE
from compute_space.core.auth.scopes import VALID_TOKEN_SCOPES
from compute_space.core.auth.scopes import dump_scopes
from compute_space.core.auth.scopes import parse_scopes
from compute_space.core.auth.scopes import token_has_scope
from compute_space.core.auth.scopes import validate_requested_scopes


def test_owner_satisfies_any_scope() -> None:
    granted = frozenset({OWNER})
    assert token_has_scope(granted, APPS_READ)
    assert token_has_scope(granted, TOKENS_MANAGE)
    # even a scope that isn't in ALL_SCOPES: owner is a blanket grant
    assert token_has_scope(granted, "anything:at:all")


def test_specific_scope_only_satisfies_itself() -> None:
    granted = frozenset({APPS_READ})
    assert token_has_scope(granted, APPS_READ)
    assert not token_has_scope(granted, APPS_MANAGE)
    assert not token_has_scope(granted, TOKENS_MANAGE)


def test_empty_grant_satisfies_nothing() -> None:
    assert not token_has_scope(frozenset(), APPS_READ)
    assert not token_has_scope(frozenset(), OWNER)


def test_parse_dump_round_trip() -> None:
    scopes = {APPS_READ, APPS_MANAGE}
    assert parse_scopes(dump_scopes(scopes)) == frozenset(scopes)
    # dump is sorted + stable
    assert dump_scopes({APPS_MANAGE, APPS_READ}) == dump_scopes({APPS_READ, APPS_MANAGE})


def test_parse_raises_on_malformed() -> None:
    # A corrupt scopes value must surface as an error, not silently become "no
    # access" (which would masquerade as a permission denial and be hard to
    # trace back to a formatting problem).
    for bad in ["", "not json", '{"not": "a list"}', "123", '["ok", 42]']:
        with pytest.raises(ValueError):
            parse_scopes(bad)


def test_parse_keeps_wellformed_unknown_scopes() -> None:
    # A valid JSON array of strings parses even if a name is unrecognized;
    # token_has_scope simply never matches the unknown name (forward-compat).
    parsed = parse_scopes('["apps:read", "future:scope"]')
    assert parsed == frozenset({APPS_READ, "future:scope"})
    assert not token_has_scope(parsed, "some:other")


def test_validate_requested_scopes() -> None:
    assert validate_requested_scopes([APPS_READ, APPS_MANAGE]) is None
    assert validate_requested_scopes([OWNER]) is None
    # empty is rejected — a token must carry at least one scope
    assert validate_requested_scopes([]) is not None
    # unknown scopes are rejected with a message naming them
    err = validate_requested_scopes([APPS_READ, "bogus:scope"])
    assert err is not None and "bogus:scope" in err


def test_owner_is_valid_but_not_in_all_scopes() -> None:
    # OWNER is a valid token value but is the super-scope, not a concrete one.
    assert OWNER in VALID_TOKEN_SCOPES
    assert OWNER not in ALL_SCOPES


def test_owner_equivalent_subset_of_all_scopes() -> None:
    assert OWNER_EQUIVALENT_SCOPES <= ALL_SCOPES
    assert TOKENS_MANAGE in OWNER_EQUIVALENT_SCOPES
    assert APPS_READ not in OWNER_EQUIVALENT_SCOPES


def test_derived_sets_match_catalog() -> None:
    # The derived sets are exactly the catalog — the single source of truth —
    # so the CLI/UI (which render from the catalog via /api/token_scopes) stay
    # 1-1 with what the guards enforce.
    catalog_names = {s.name for s in SCOPE_CATALOG}
    assert VALID_TOKEN_SCOPES == catalog_names
    assert ALL_SCOPES == catalog_names - {OWNER}
    assert OWNER_EQUIVALENT_SCOPES == {s.name for s in SCOPE_CATALOG if s.owner_equivalent and s.name != OWNER}
    # Every scope has a non-empty human description for the UIs.
    assert all(s.description for s in SCOPE_CATALOG)
    # No duplicate names.
    assert len(catalog_names) == len(SCOPE_CATALOG)
