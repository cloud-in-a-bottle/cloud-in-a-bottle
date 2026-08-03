"""API-token scopes: the granular permission vocabulary for bearer tokens.

A token carries a set of scope strings (stored as a JSON array in
``api_tokens.scopes``).  A request guarded by ``require_scope(X)`` passes for
a token iff its scope set contains ``X`` or the ``OWNER`` super-scope.

Session-owner (human) auth is never scope-restricted — scopes only ever
*narrow* what a token can do.  See docs/api_token_scopes_ai_generated.md.

``SCOPE_CATALOG`` is the single source of truth: name, human description, and
whether the scope is owner-equivalent (privilege-escalating).  Every other
view — the derived sets below, the ``GET /api/token_scopes`` endpoint, the CLI
``oh tokens scopes`` listing, and the web UI's checkboxes — is derived from it
so they can't drift out of sync.
"""

from __future__ import annotations

import json

import attr

# ── The super-scope ─────────────────────────────────────────────────────────
# "owner" means "all scopes".  It is the migration/back-compat default for
# tokens that predate scopes, and lets a human mint a full-access token.
OWNER = "owner"

# ── Concrete scope names ─────────────────────────────────────────────────────
# Kept as module constants so route guards can reference them by symbol
# (require_scope(APPS_READ)).  The catalog below is built from these.
APPS_READ = "apps:read"
APPS_LOGS = "apps:logs"
APPS_MANAGE = "apps:manage"
APPS_DELETE = "apps:delete"
SYSTEM_READ = "system:read"
SETTINGS_READ = "settings:read"
SYSTEM_ADMIN = "system:admin"
SETTINGS_WRITE = "settings:write"
STORAGE_ADMIN = "storage:admin"
TOKENS_MANAGE = "tokens:manage"
PERMISSIONS_MANAGE = "permissions:manage"
IDENTITY_APPROVE = "identity:approve"


@attr.s(auto_attribs=True, frozen=True)
class ScopeDef:
    """One entry in the scope vocabulary.

    ``owner_equivalent`` marks scopes that can escalate privilege or exfiltrate
    secrets (so any one of them is effectively equal to ``owner``); the token
    UIs surface a warning when one is selected.
    """

    name: str
    description: str
    owner_equivalent: bool


# The single source of truth.  Order is the display order in the UIs.
SCOPE_CATALOG: tuple[ScopeDef, ...] = (
    ScopeDef(OWNER, "Full access (all scopes)", owner_equivalent=True),
    ScopeDef(APPS_READ, "List apps, status, diagnostics", owner_equivalent=False),
    ScopeDef(APPS_LOGS, "Read app logs (may contain user data)", owner_equivalent=False),
    ScopeDef(APPS_MANAGE, "Deploy, reload, stop, start, rename apps", owner_equivalent=False),
    ScopeDef(APPS_DELETE, "Remove apps", owner_equivalent=False),
    ScopeDef(SYSTEM_READ, "Version, ports, storage/ssh status, logs", owner_equivalent=False),
    ScopeDef(SETTINGS_READ, "Read settings, owner username, remote", owner_equivalent=False),
    ScopeDef(SYSTEM_ADMIN, "Toggle SSH, restart router, drop cache", owner_equivalent=True),
    ScopeDef(SETTINGS_WRITE, "Update settings, change owner password", owner_equivalent=True),
    ScopeDef(STORAGE_ADMIN, "Configure archive backend / S3 creds", owner_equivalent=True),
    ScopeDef(TOKENS_MANAGE, "Create/list/delete API tokens", owner_equivalent=True),
    ScopeDef(PERMISSIONS_MANAGE, "Grant/revoke app service permissions (all secrets)", owner_equivalent=True),
    ScopeDef(IDENTITY_APPROVE, "Sign federated identity tokens as owner", owner_equivalent=True),
)

# ── Derived views (do not hand-maintain — all computed from SCOPE_CATALOG) ────

# Every valid value a token may hold, including the super-scope.
VALID_TOKEN_SCOPES: frozenset[str] = frozenset(s.name for s in SCOPE_CATALOG)

# The concrete grantable scopes (everything except the OWNER super-scope).
ALL_SCOPES: frozenset[str] = VALID_TOKEN_SCOPES - {OWNER}

# Concrete scopes that are effectively owner-equivalent.  Selecting any should
# trigger a privilege-escalation warning in the token-creation UIs.  Excludes
# the OWNER super-scope itself (which is owner by definition, not a concrete
# escalation scope) so this stays a subset of ALL_SCOPES.
OWNER_EQUIVALENT_SCOPES: frozenset[str] = frozenset(
    s.name for s in SCOPE_CATALOG if s.owner_equivalent and s.name != OWNER
)


def token_has_scope(granted: frozenset[str], required: str) -> bool:
    """True if a token holding ``granted`` scopes satisfies ``required``.

    The ``OWNER`` super-scope satisfies any required scope.
    """
    return OWNER in granted or required in granted


def parse_scopes(raw: str) -> frozenset[str]:
    """Parse the JSON ``api_tokens.scopes`` column into a scope set.

    Raises ``ValueError`` if the stored value isn't a JSON array of strings.  A
    corrupt scopes value is a data-integrity bug and must surface as an *error*
    — not be silently coerced into "no access", which would masquerade as an
    ordinary permission denial and be far harder to trace back to a
    formatting/parsing problem.

    Unknown-but-well-formed scope names are returned as-is (not rejected here):
    name validation is the write path's job (``validate_requested_scopes``), and
    tolerating an unrecognized name keeps a token forward-compatible with a
    newer scope it was granted — ``token_has_scope`` simply never matches it.
    """
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"api_tokens.scopes is not valid JSON: {raw!r}") from e
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"api_tokens.scopes must be a JSON array of strings, got: {value!r}")
    return frozenset(value)


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
