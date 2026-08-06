"""The OpenHost *platform* service: platform operations exposed to apps over the
same v2 service interface they use to call other apps, but dispatched in-process
by the router (like the ``installer`` service) rather than proxied to a provider
app.

Design:

- Apps consume this service via a normal ``[[services.v2.consumes]]`` block and
  call ``/api/services/v2/call/<shortname>/...``.  Auth is the app's
  ``$OPENHOST_APP_TOKEN``; permissions are ordinary ``permissions_v2`` grants
  keyed on ``(consumer_app_id, PLATFORM_SERVICE_URL)``.
- The vocabulary of capabilities is roughly "what the ``oh`` CLI can do", gated
  per-capability by grant payloads.
- The headline capability is *propagating / non-escalating delegation*: an app
  may deploy new apps, have full control over the apps **it** deployed, and
  grant those apps any permission it **already holds** — so it can make copies
  of (a subset of) its own privileges but never escalate.

Grant payloads (JSON objects stored in ``permissions_v2.grant_payload``):

    {"capability": "deploy", "repo_url_prefix": "https://github.com/acme/"}
        Deploy new apps whose repo_url starts with the prefix ("" or "*" = any).
        A token-/app-initiated deploy stamps ``apps.installed_by`` with the
        caller, which is what "apps I deployed" below keys on.

    {"capability": "manage_apps", "target": "own"}
    {"capability": "manage_apps", "target": "all"}
    {"capability": "manage_apps", "target": "<app_id>"}
        Manage existing apps: view status/logs, stop/start, remove.
        ``own``  = only apps this caller deployed (installed_by == caller).
        ``all``  = every app on the instance.
        ``<id>`` = one specific app.
        Managing apps does NOT include granting them permissions — that is a
        separate capability (``delegate_permissions``).

    {"capability": "system_read"}
        Read-only platform/system info (version, disk, memory, ports, logs).

    {"capability": "delegate_permissions"}
        Allow the caller to grant a deployed app any permission the caller
        itself already holds (bounded intersection — see
        ``check_delegation_allowed``).  This is the escalation-sensitive one;
        it never lets the caller hand out a grant it does not itself possess.

Only dict-shaped grants with a recognized ``capability`` are honored; anything
else is ignored (fail-closed).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import attr

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.auth.permissions_v2 import GrantAtom
from compute_space.core.auth.permissions_v2 import GrantedPermission

# ── Service identity ─────────────────────────────────────────────────────────
PLATFORM_SERVICE_URL = "github.com/imbue-openhost/openhost/services/platform"
PLATFORM_SERVICE_VERSION = "0.1.0"

# ── Grant payload keys / capability names ────────────────────────────────────
GRANT_KEY_CAPABILITY = "capability"
GRANT_KEY_REPO_URL_PREFIX = "repo_url_prefix"
GRANT_KEY_TARGET = "target"

CAP_DEPLOY = "deploy"
CAP_MANAGE_APPS = "manage_apps"
CAP_SYSTEM_READ = "system_read"
CAP_DELEGATE_PERMISSIONS = "delegate_permissions"

ALL_CAPABILITIES: frozenset[str] = frozenset({CAP_DEPLOY, CAP_MANAGE_APPS, CAP_SYSTEM_READ, CAP_DELEGATE_PERMISSIONS})

# ``manage_apps`` target sentinels (anything else is treated as a concrete app_id).
TARGET_OWN = "own"
TARGET_ALL = "all"


@attr.s(auto_attribs=True, frozen=True)
class ManageScope:
    """Resolved reach of a caller's ``manage_apps`` grants.

    ``all`` wins over everything.  Otherwise the caller may manage apps it
    deployed (``own``) and/or an explicit set of app_ids.
    """

    all_apps: bool = False
    own_apps: bool = False
    app_ids: frozenset[str] = attr.ib(factory=frozenset)

    def allows(self, *, app_id: str, installed_by: str | None, caller_app_id: str) -> bool:
        if self.all_apps:
            return True
        if self.own_apps and installed_by == caller_app_id:
            return True
        return app_id in self.app_ids


def is_known_capability(capability: object) -> bool:
    """True iff ``capability`` is one of the platform service's capabilities."""
    return isinstance(capability, str) and capability in ALL_CAPABILITIES


def _dict_grants_with_capability(grants: list[Grant], capability: str) -> list[Mapping[str, GrantAtom]]:
    """The dict-shaped grants whose ``capability`` equals ``capability``.

    Non-dict grants (strings, lists), grants for other capabilities, and grants
    naming an unknown capability are all skipped, so an unrecognized/mixed grant
    set never accidentally widens access.  (``capability`` itself is always a
    known ``CAP_*`` value at every call site, so the ``is_known_capability``
    guard only ever filters the stored grant, never the requested capability.)
    """
    out: list[Mapping[str, GrantAtom]] = []
    for g in grants:
        if not isinstance(g, dict):
            continue
        cap = g.get(GRANT_KEY_CAPABILITY)
        if not is_known_capability(cap):
            continue
        if cap == capability:
            out.append(g)
    return out


def check_deploy_allowed(repo_url: str, grants: list[Grant]) -> str | None:
    """Return None if some ``deploy`` grant permits installing ``repo_url``,
    else a human-readable reason for the 403.

    ``repo_url_prefix`` of ``""`` or ``"*"`` matches any URL; otherwise the
    requested URL must start with the prefix.
    """
    deploy_grants = _dict_grants_with_capability(grants, CAP_DEPLOY)
    if not deploy_grants:
        return "no deploy grant present"
    for g in deploy_grants:
        prefix = g.get(GRANT_KEY_REPO_URL_PREFIX, "")
        if not isinstance(prefix, str):
            continue
        if prefix in ("", "*") or repo_url.startswith(prefix):
            return None
    return "no deploy grant matches the requested repo_url"


def resolve_manage_scope(grants: list[Grant]) -> ManageScope:
    """Fold a caller's ``manage_apps`` grants into a single :class:`ManageScope`."""
    all_apps = False
    own_apps = False
    app_ids: set[str] = set()
    for g in _dict_grants_with_capability(grants, CAP_MANAGE_APPS):
        target = g.get(GRANT_KEY_TARGET, TARGET_OWN)
        if not isinstance(target, str) or not target:
            continue
        if target == TARGET_ALL:
            all_apps = True
        elif target == TARGET_OWN:
            own_apps = True
        else:
            app_ids.add(target)
    return ManageScope(all_apps=all_apps, own_apps=own_apps, app_ids=frozenset(app_ids))


def has_capability(grants: list[Grant], capability: str) -> bool:
    """True if the caller holds at least one grant for ``capability``.

    Used for capabilities that take no further parameters (``system_read``,
    ``delegate_permissions``).
    """
    return bool(_dict_grants_with_capability(grants, capability))


def _grant_identity(grant: Grant) -> str:
    """Stable identity for a grant payload (order-insensitive JSON), so two
    logically-equal grants compare equal regardless of key order."""
    return json.dumps(grant, sort_keys=True)


def matching_held_grant(
    caller_grants_for_service: list[GrantedPermission],
    grant: Grant,
) -> GrantedPermission | None:
    """Return the caller's own :class:`GrantedPermission` whose payload equals
    ``grant`` for the target service, or ``None`` if the caller holds no such
    grant.

    Payloads are compared by canonical JSON so key order doesn't matter.  The
    full ``GrantedPermission`` (including its ``scope`` and ``provider_app_id``)
    is returned so the delegation writer can copy the caller's *exact* authority
    to the child — never a broader one.  If the caller holds the same payload at
    more than one scope, the narrowest (app-scoped) match is preferred so
    delegation can't accidentally widen to global.
    """
    wanted = _grant_identity(grant)
    matches = [gp for gp in caller_grants_for_service if _grant_identity(gp.grant) == wanted]
    if not matches:
        return None
    # Prefer an app-scoped match (narrower) over a global one.
    app_scoped = [gp for gp in matches if gp.scope == "app"]
    return app_scoped[0] if app_scoped else matches[0]


@attr.s(auto_attribs=True, frozen=True)
class DelegationDecision:
    """Result of :func:`check_delegation_allowed`.

    ``reason`` is non-None on denial.  On approval, ``granted`` carries the
    caller's own matching grant (scope + provider_app_id) that the writer must
    copy verbatim to the child — copying the caller's exact authority is what
    keeps delegation non-escalating.
    """

    reason: str | None
    granted: GrantedPermission | None = None


def check_delegation_allowed(
    *,
    caller_platform_grants: list[Grant],
    caller_grants_for_target_service: list[GrantedPermission],
    grant_to_delegate: Grant,
) -> DelegationDecision:
    """Decide whether the caller may delegate ``grant_to_delegate`` (for some
    target service) to an app it deployed.

    Two independent conditions must both hold (fail-closed):

    1. The caller must hold the ``delegate_permissions`` platform capability.
    2. The caller must **already possess** a grant with the same payload for
       that service (anti-escalation: you can only pass on privileges you have,
       never mint new ones).

    On approval the returned :class:`DelegationDecision` carries the caller's
    own matching grant, whose ``scope``/``provider_app_id`` the writer copies to
    the child so the delegated authority is never broader than the caller's
    (e.g. an app-scoped grant is delegated app-scoped to the same provider, not
    widened to global).
    """
    if not has_capability(caller_platform_grants, CAP_DELEGATE_PERMISSIONS):
        return DelegationDecision(reason="caller lacks the delegate_permissions capability")
    held = matching_held_grant(caller_grants_for_target_service, grant_to_delegate)
    if held is None:
        return DelegationDecision(reason="caller does not itself hold the grant it is trying to delegate")
    return DelegationDecision(reason=None, granted=held)
