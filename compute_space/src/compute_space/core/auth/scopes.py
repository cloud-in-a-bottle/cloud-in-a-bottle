"""API-token scopes: the granular permission vocabulary for bearer tokens.

A token carries a set of scope strings (stored as a JSON array in
``api_tokens.scopes``).  A request guarded by ``require_scope(X)`` passes for
a token iff its scope set contains ``X`` or the ``OWNER`` super-scope.

Session-owner (human) auth is never scope-restricted — scopes only ever
*narrow* what a token can do.  See docs/api_token_scopes_ai_generated.md.
"""

from __future__ import annotations

import json

# ── The super-scope ─────────────────────────────────────────────────────────
# "owner" means "all scopes".  It is the migration/back-compat default for
# tokens that predate scopes, and lets a human mint a full-access token.
OWNER = "owner"

# ── Read tier ────────────────────────────────────────────────────────────────
APPS_READ = "apps:read"  # list apps, status, diagnostics
APPS_LOGS = "apps:logs"  # read app logs  (⚠ may contain user data — kept separate)
SYSTEM_READ = "system:read"  # version, ports, storage/ssh status, platform logs
SETTINGS_READ = "settings:read"  # read settings, owner username, remote

# ── Write tier ───────────────────────────────────────────────────────────────
APPS_MANAGE = "apps:manage"  # deploy, clone, reload, stop, start, rename, set-remote
APPS_DELETE = "apps:delete"  # remove apps (destructive — separate opt-in)

# ── Owner-equivalent tier ────────────────────────────────────────────────────
# Each of these lets the holder escalate its own privilege or exfiltrate
# secrets, so any one of them is effectively equivalent to `owner`.  They are
# allowed as scopes, but the creation UI must warn when any is selected.
SYSTEM_ADMIN = "system:admin"  # toggle SSH, restart router, drop cache, storage-guard
SETTINGS_WRITE = "settings:write"  # update settings, change owner password
STORAGE_ADMIN = "storage:admin"  # configure archive backend / S3 credentials
TOKENS_MANAGE = "tokens:manage"  # create/list/delete API tokens (can mint full-access)
PERMISSIONS_MANAGE = "permissions:manage"  # grant/revoke app service perms (≈ all secrets)
IDENTITY_APPROVE = "identity:approve"  # sign federated identity tokens as the owner

# The set of concrete scopes a token may be granted (excluding the OWNER
# super-scope, which is handled specially).
ALL_SCOPES: frozenset[str] = frozenset(
    {
        APPS_READ,
        APPS_LOGS,
        SYSTEM_READ,
        SETTINGS_READ,
        APPS_MANAGE,
        APPS_DELETE,
        SYSTEM_ADMIN,
        SETTINGS_WRITE,
        STORAGE_ADMIN,
        TOKENS_MANAGE,
        PERMISSIONS_MANAGE,
        IDENTITY_APPROVE,
    }
)

# Scopes that are effectively owner-equivalent.  Selecting any should trigger a
# privilege-escalation warning in the token-creation UI.
OWNER_EQUIVALENT_SCOPES: frozenset[str] = frozenset(
    {
        SYSTEM_ADMIN,
        SETTINGS_WRITE,
        STORAGE_ADMIN,
        TOKENS_MANAGE,
        PERMISSIONS_MANAGE,
        IDENTITY_APPROVE,
    }
)

# Every valid value a token may hold, including the super-scope.
VALID_TOKEN_SCOPES: frozenset[str] = ALL_SCOPES | {OWNER}


def token_has_scope(granted: frozenset[str], required: str) -> bool:
    """True if a token holding ``granted`` scopes satisfies ``required``.

    The ``OWNER`` super-scope satisfies any required scope.
    """
    return OWNER in granted or required in granted


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Parse the JSON ``api_tokens.scopes`` column into a scope set.

    Fails safe: anything malformed, empty, or not a JSON array of strings
    yields the empty set (no access) rather than raising — a corrupt scopes
    value must never accidentally grant access.  ``None``/empty is treated as
    no scopes (NOT as owner); the DB default supplies ``["owner"]`` for
    back-compat rows, so an empty value here reflects a token deliberately
    stripped of all access.
    """
    if not raw:
        return frozenset()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return frozenset()
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item in VALID_TOKEN_SCOPES)


def dump_scopes(scopes: frozenset[str] | set[str] | list[str]) -> str:
    """Serialize a scope set to the JSON string stored in ``api_tokens.scopes``.

    Sorted for stable, diff-friendly storage.
    """
    return json.dumps(sorted(set(scopes)))


def validate_requested_scopes(scopes: list[str]) -> str | None:
    """Return None if every requested scope is valid, else an error message.

    Used by the token-creation/update API to reject unknown scope strings.
    """
    unknown = sorted(s for s in scopes if s not in VALID_TOKEN_SCOPES)
    if unknown:
        return f"Unknown scope(s): {', '.join(unknown)}"
    if not scopes:
        return "At least one scope is required."
    return None
