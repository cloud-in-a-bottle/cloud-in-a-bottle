"""Unit tests for the platform-service grant model (core/platform_service.py).

Pure functions, no DB: deploy-prefix matching, manage-scope resolution, and the
non-escalating delegation rule.  All checks must fail closed.
"""

from __future__ import annotations

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.auth.permissions_v2 import GrantedPermission
from compute_space.core.platform_service import CAP_DELEGATE_PERMISSIONS
from compute_space.core.platform_service import CAP_SYSTEM_READ
from compute_space.core.platform_service import check_delegation_allowed
from compute_space.core.platform_service import check_deploy_allowed
from compute_space.core.platform_service import has_capability
from compute_space.core.platform_service import resolve_manage_scope

# ── deploy ───────────────────────────────────────────────────────────────────


def test_deploy_no_grants_denied() -> None:
    assert check_deploy_allowed("https://github.com/a/b", []) is not None


def test_deploy_wildcard_prefix_allows_any() -> None:
    g: list[Grant] = [{"capability": "deploy", "repo_url_prefix": "*"}]
    assert check_deploy_allowed("https://github.com/anyone/thing", g) is None


def test_deploy_empty_prefix_allows_any() -> None:
    g: list[Grant] = [{"capability": "deploy", "repo_url_prefix": ""}]
    assert check_deploy_allowed("https://x/y", g) is None


def test_deploy_matching_prefix_allows() -> None:
    g: list[Grant] = [{"capability": "deploy", "repo_url_prefix": "https://github.com/acme/"}]
    assert check_deploy_allowed("https://github.com/acme/widget", g) is None


def test_deploy_non_matching_prefix_denied() -> None:
    g: list[Grant] = [{"capability": "deploy", "repo_url_prefix": "https://github.com/acme/"}]
    assert check_deploy_allowed("https://github.com/evil/x", g) is not None


def test_deploy_ignores_other_capabilities_and_non_dicts() -> None:
    g: list[Grant] = ["some-string", {"capability": "manage_apps", "target": "all"}, ["list"]]
    # No deploy grant present among these -> denied.
    assert check_deploy_allowed("https://github.com/acme/x", g) is not None


# ── manage scope ───────────────────────────────────────────────────────────


def test_manage_scope_own_only() -> None:
    scope = resolve_manage_scope([{"capability": "manage_apps", "target": "own"}])
    assert scope.own_apps and not scope.all_apps
    assert scope.allows(app_id="abc", installed_by="caller", caller_app_id="caller")
    assert not scope.allows(app_id="abc", installed_by="someone-else", caller_app_id="caller")


def test_manage_scope_all() -> None:
    scope = resolve_manage_scope([{"capability": "manage_apps", "target": "all"}])
    assert scope.all_apps
    assert scope.allows(app_id="abc", installed_by=None, caller_app_id="caller")
    assert scope.allows(app_id="xyz", installed_by="other", caller_app_id="caller")


def test_manage_scope_specific_app_id() -> None:
    scope = resolve_manage_scope([{"capability": "manage_apps", "target": "app_00000001"}])
    assert not scope.all_apps and not scope.own_apps
    assert scope.allows(app_id="app_00000001", installed_by="whoever", caller_app_id="caller")
    assert not scope.allows(app_id="app_00000002", installed_by="whoever", caller_app_id="caller")


def test_manage_scope_combines_own_and_specific() -> None:
    scope = resolve_manage_scope(
        [
            {"capability": "manage_apps", "target": "own"},
            {"capability": "manage_apps", "target": "app_ZZ"},
        ]
    )
    assert scope.own_apps and "app_ZZ" in scope.app_ids
    assert scope.allows(app_id="mine", installed_by="caller", caller_app_id="caller")
    assert scope.allows(app_id="app_ZZ", installed_by="other", caller_app_id="caller")
    assert not scope.allows(app_id="app_QQ", installed_by="other", caller_app_id="caller")


def test_manage_scope_default_target_is_own() -> None:
    # A manage_apps grant with no explicit target defaults to "own".
    scope = resolve_manage_scope([{"capability": "manage_apps"}])
    assert scope.own_apps and not scope.all_apps


def test_manage_scope_empty_when_no_manage_grants() -> None:
    scope = resolve_manage_scope([{"capability": "deploy", "repo_url_prefix": "*"}])
    assert not scope.all_apps and not scope.own_apps and not scope.app_ids


# ── system_read / has_capability ─────────────────────────────────────────────


def test_has_capability_system_read() -> None:
    assert has_capability([{"capability": "system_read"}], CAP_SYSTEM_READ)
    assert not has_capability([{"capability": "deploy", "repo_url_prefix": "*"}], CAP_SYSTEM_READ)
    assert not has_capability(["system_read"], CAP_SYSTEM_READ)  # string, not dict grant


# ── delegation (anti-escalation) ─────────────────────────────────────────────


def _gp(grant: object, scope: str = "global", provider: str | None = None) -> GrantedPermission:
    return GrantedPermission(grant=grant, scope=scope, provider_app_id=provider)  # type: ignore[arg-type]


def test_delegation_denied_without_capability() -> None:
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": "deploy", "repo_url_prefix": "*"}],
        caller_grants_for_target_service=[_gp({"key": "DB_URL"})],
        grant_to_delegate={"key": "DB_URL"},
    )
    assert d.reason is not None and "delegate_permissions" in d.reason
    assert d.granted is None


def test_delegation_denied_when_caller_lacks_the_grant() -> None:
    # Caller has the capability but does NOT itself hold the grant it wants to
    # hand out -> escalation attempt, denied.
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[_gp({"key": "PUBLIC"})],
        grant_to_delegate={"key": "SECRET"},
    )
    assert d.reason is not None and "does not itself hold" in d.reason


def test_delegation_allowed_when_caller_holds_grant_and_capability() -> None:
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[_gp({"key": "SECRET"})],
        grant_to_delegate={"key": "SECRET"},
    )
    assert d.reason is None
    assert d.granted is not None and d.granted.scope == "global"


def test_delegation_grant_identity_is_key_order_insensitive() -> None:
    # {"a":1,"b":2} held; delegating {"b":2,"a":1} must be recognized as the same.
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[_gp({"a": 1, "b": 2})],
        grant_to_delegate={"b": 2, "a": 1},
    )
    assert d.reason is None


def test_delegation_string_grant_matches() -> None:
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[_gp("FULL_ACCESS")],
        grant_to_delegate="FULL_ACCESS",
    )
    assert d.reason is None


def test_delegation_preserves_app_scope_no_widening_to_global() -> None:
    # Caller holds the grant ONLY app-scoped to provider X.  Delegation must copy
    # that exact (app, X) scope to the child — never widen it to global, which
    # would let the child use it against any provider (privilege escalation).
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[_gp({"key": "DB_URL"}, scope="app", provider="ProviderX")],
        grant_to_delegate={"key": "DB_URL"},
    )
    assert d.reason is None
    assert d.granted is not None
    assert d.granted.scope == "app"
    assert d.granted.provider_app_id == "ProviderX"


def test_delegation_prefers_app_scoped_match_when_both_exist() -> None:
    # If the caller holds the same payload both app-scoped and global, the
    # narrower (app-scoped) authority is what gets delegated.
    d = check_delegation_allowed(
        caller_platform_grants=[{"capability": CAP_DELEGATE_PERMISSIONS}],
        caller_grants_for_target_service=[
            _gp({"key": "DB_URL"}, scope="global"),
            _gp({"key": "DB_URL"}, scope="app", provider="ProviderX"),
        ],
        grant_to_delegate={"key": "DB_URL"},
    )
    assert d.reason is None
    assert d.granted is not None and d.granted.scope == "app"
