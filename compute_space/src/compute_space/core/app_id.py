"""Here to avoid a circular import in migrations if we put this in apps.py"""

import re
import secrets

# App names are DNS-label-like: lowercase alphanumeric, hyphens allowed but not
# at the edges.
_APP_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Bitcoin base58 alphabet: omits 0, O, I, l to avoid visual ambiguity.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_SET = frozenset(_BASE58_ALPHABET)

APP_ID_LENGTH = 12

# The router's own identity, for when it provides or consumes a service itself.  App ids are
# base58 (see the alphabet above), so the underscore cannot collide with a real one.
ROUTER_APP_ID = "_openhost_router"
ROUTER_APP_NAME = "OpenHost Router"


def _encode_base58_padded(n: int) -> str:
    chars = []
    for _ in range(APP_ID_LENGTH):
        n, rem = divmod(n, 58)
        chars.append(_BASE58_ALPHABET[rem])
    return "".join(reversed(chars))


def new_app_id() -> str:
    """Mint a fresh 12-char base58 app id (~70 bits of entropy)."""
    n = int.from_bytes(secrets.token_bytes(9), "big")
    return _encode_base58_padded(n)


def is_valid_app_id(s: str) -> bool:
    """True iff s is exactly APP_ID_LENGTH chars, all from the base58 alphabet."""
    return len(s) == APP_ID_LENGTH and all(c in _BASE58_SET for c in s)


def is_valid_app_name(s: str) -> bool:
    """True iff s is a valid app name (lowercase alphanumeric, interior hyphens)."""
    return bool(_APP_NAME_RE.match(s))
