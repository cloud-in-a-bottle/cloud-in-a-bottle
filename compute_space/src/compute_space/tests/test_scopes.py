"""Unit tests for the API-token scope vocabulary (core/auth/scopes.py).

These are pure-function tests — parsing, serializing, membership, and request
validation. The guard-level enforcement (that a scoped token actually gets 401
on an out-of-scope route) lives in test_token_scope_enforcement.py.
"""

from __future__ import annotations

from compute_space.core.auth.scopes import ALL_SCOPES
from compute_space.core.auth.scopes import APPS_MANAGE
from compute_space.core.auth.scopes import APPS_READ
from compute_space.core.auth.scopes import OWNER
from compute_space.core.auth.scopes import OWNER_EQUIVALENT_SCOPES
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


def test_parse_fails_closed() -> None:
    # None / empty / malformed / wrong-shape all yield NO access, never owner.
    assert parse_scopes(None) == frozenset()
    assert parse_scopes("") == frozenset()
    assert parse_scopes("not json") == frozenset()
    assert parse_scopes('{"not": "a list"}') == frozenset()
    assert parse_scopes("123") == frozenset()


def test_parse_drops_unknown_scopes() -> None:
    # Unknown scope strings are silently dropped (fail closed), known ones kept.
    assert parse_scopes('["apps:read", "bogus:scope", 42]') == frozenset({APPS_READ})


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
