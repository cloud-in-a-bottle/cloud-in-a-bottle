"""Tests for the opaque app_id generator + validator."""

from __future__ import annotations

import string

from hypothesis import given
from hypothesis import strategies as st

from compute_space.core.app_id import APP_ID_LENGTH
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.app_id import is_valid_app_name
from compute_space.core.app_id import new_app_id


def test_new_app_id_shape():
    for _ in range(50):
        app_id = new_app_id()
        assert len(app_id) == APP_ID_LENGTH
        assert is_valid_app_id(app_id)


def test_new_app_id_collision_resistance():
    """~70 bits of entropy — no collisions across a small batch should be the norm."""
    ids = {new_app_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_is_valid_app_id_rejects_obvious_garbage():
    assert is_valid_app_id("") is False
    assert is_valid_app_id("short") is False
    assert is_valid_app_id("a" * (APP_ID_LENGTH + 1)) is False
    # 0, O, I, l are excluded from the bitcoin base58 alphabet.
    assert is_valid_app_id("0" * APP_ID_LENGTH) is False
    assert is_valid_app_id("O" * APP_ID_LENGTH) is False
    assert is_valid_app_id("I" * APP_ID_LENGTH) is False
    assert is_valid_app_id("l" * APP_ID_LENGTH) is False
    # Punctuation, spaces, etc.
    assert is_valid_app_id("a" * (APP_ID_LENGTH - 1) + " ") is False
    assert is_valid_app_id("a" * (APP_ID_LENGTH - 1) + "-") is False


@given(
    name=st.one_of(
        st.text(),
        st.tuples(
            st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1),
            st.sampled_from(["", "\n", "\r", "\t", " ", "_"]),
        ).map("".join),
    )
)
def test_app_name_matches_documented_grammar(name: str) -> None:
    alphanumeric = string.ascii_lowercase + string.digits
    expected = (
        bool(name)
        and name[0] in alphanumeric
        and name[-1] in alphanumeric
        and all(character in alphanumeric + "-" for character in name)
    )
    assert is_valid_app_name(name) == expected
