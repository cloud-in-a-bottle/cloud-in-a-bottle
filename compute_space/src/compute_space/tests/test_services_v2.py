from __future__ import annotations

import types
from typing import Any

import pytest

from compute_space.core.app_id import new_app_id
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.core.proxy_target import LocalPort
from compute_space.core.service_interface.headers import approve_grant_url
from compute_space.core.service_interface.resolve import resolve_provider
from compute_space.core.service_interface.services import default_provider_id_for_service
from compute_space.core.service_interface.services import lookup_service_by_manifest_shortname
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY
from compute_space.web.routes.api.apps import _oauth_return_host

SVC_SECRETS = "github.com/org/repo/services/secrets"
SVC_OAUTH = "github.com/org/repo/services/oauth"


def _add_provider(
    db, service_url, app_name, version, endpoint, port=9000, status="running", default=True, installed_at="2020-01-01"
) -> str:
    """Insert a provider app row + service_providers_v2 entry. Returns the minted app_id.

    ``installed_at`` sets ``apps.created_at``, which is what decides between providers the owner
    has not chosen between; it defaults to a shared timestamp so callers that don't care about
    install order get a tie.
    """
    app_id = new_app_id()
    db.execute(
        """INSERT OR REPLACE INTO apps (app_id, name, version, repo_path, local_port, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (app_id, app_name, "0.0.0", f"/tmp/{app_name}", port, status, installed_at),
    )
    db.execute(
        "INSERT INTO service_providers_v2 (service_url, app_id, service_version, endpoint) VALUES (?, ?, ?, ?)",
        (service_url, app_id, version, endpoint),
    )
    if default:
        db.execute(
            "INSERT OR REPLACE INTO service_defaults (service_url, app_id) VALUES (?, ?)",
            (service_url, app_id),
        )
    db.commit()
    return app_id


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


class TestVersionResolution:
    def test_compatible_version_resolves(self, db):
        provider_id = _add_provider(db, SVC_SECRETS, "secrets", "0.2.0", "/_svc/")
        provider = resolve_provider(SVC_SECRETS, ">=0.1.0", db)
        assert provider.app_id == provider_id
        assert provider.service_version == "0.2.0"
        assert provider.endpoint == "/_svc/"
        assert provider.target == LocalPort(9000)

    def test_exact_version(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "1.0.0", "/_svc/")
        assert resolve_provider(SVC_SECRETS, "==1.0.0", db).service_version == "1.0.0"

    def test_a_provider_serves_when_no_default_is_set(self, db):
        # The default row goes away with the app it named (ON DELETE CASCADE), and the owner can
        # clear it outright — neither should leave the service dead while a provider exists.
        a_id = _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001, default=False)
        assert default_provider_id_for_service(SVC_SECRETS, db) == a_id
        assert resolve_provider(SVC_SECRETS, ">=0.1.0", db).app_id == a_id

    def test_installing_a_second_provider_does_not_take_the_service_off_the_first(self, db):
        # Providers of a service hold their own data, so a newer or higher-versioned one appearing
        # alongside the incumbent must not silently redirect consumers at its empty store.
        a_id = _add_provider(
            db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001, default=False, installed_at="2020-01-01"
        )
        _add_provider(
            db, SVC_SECRETS, "secrets-b", "9.0.0", "/_b/", port=9002, default=False, installed_at="2020-06-01"
        )
        assert default_provider_id_for_service(SVC_SECRETS, db) == a_id

    def test_version_breaks_a_tie_between_providers_installed_at_the_same_time(self, db):
        # created_at is second-granular, so two providers can tie; the answer still has to be
        # stable rather than whatever the query happens to return first.
        _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001, default=False)
        b_id = _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)
        assert default_provider_id_for_service(SVC_SECRETS, db) == b_id

    def test_a_default_naming_a_non_provider_is_reported_not_silently_replaced(self, db):
        # An app that drops a service on update loses its service_providers_v2 row, but the
        # default row survives (its foreign key is on apps).  Say so rather than quietly routing
        # to whoever else happens to provide the service.
        b_id = _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)
        gone_id = new_app_id()
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, 'lapsed', '0.0.0', '/tmp/lapsed', 9003, 'running')""",
            (gone_id,),
        )
        db.execute("INSERT INTO service_defaults (service_url, app_id) VALUES (?, ?)", (SVC_SECRETS, gone_id))
        db.commit()

        assert default_provider_id_for_service(SVC_SECRETS, db) == gone_id
        assert default_provider_id_for_service(SVC_SECRETS, db) != b_id
        with pytest.raises(RuntimeError, match="not found"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db)

    def test_an_explicit_default_beats_a_higher_version(self, db):
        a_id = _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001)
        _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)
        assert default_provider_id_for_service(SVC_SECRETS, db) == a_id

    def test_no_provider_raises(self, db):
        with pytest.raises(RuntimeError, match="No provider"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db)

    def test_version_mismatch_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(RuntimeError, match="does not match"):
            resolve_provider(SVC_SECRETS, ">=99.0.0", db)

    def test_not_running_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/", status="stopped")
        with pytest.raises(RuntimeError, match="not running"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db)

    def test_explicit_provider_app(self, db):
        _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001)
        b_id = _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)

        provider = resolve_provider(SVC_SECRETS, ">=0.1.0", db, provider_app_id=b_id)
        assert provider.app_id == b_id
        assert provider.endpoint == "/_b/"

    def test_explicit_provider_app_not_found(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(RuntimeError, match="not found"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db, provider_app_id=new_app_id())

    def test_explicit_provider_app_version_mismatch(self, db):
        provider_id = _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(RuntimeError, match="does not match"):
            resolve_provider(SVC_SECRETS, ">=99.0.0", db, provider_app_id=provider_id)

    def test_uses_default_provider(self, db):
        a_id = _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001)
        _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)

        assert resolve_provider(SVC_SECRETS, ">=0.1.0", db).app_id == a_id


# ---------------------------------------------------------------------------
# Permissions V2
# ---------------------------------------------------------------------------


class TestPermissionsV2:
    def test_grant_and_query(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "DB_URL"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1
        assert grants[0].grant == {"key": "DB_URL"}

    def test_grant_is_idempotent(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1

    def test_revoke(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})
        assert revoke_permission_v2("test-app", SVC_SECRETS, {"key": "X"}) is True

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 0

    def test_revoke_nonexistent_returns_false(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        assert revoke_permission_v2("test-app", SVC_SECRETS, {"key": "NOPE"}) is False

    def test_revoke_requires_matching_scope_and_provider(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2(
            "test-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email"},
            scope="app",
            provider_app_id="secrets",
        )
        # Wrong scope
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
            )
            is False
        )
        # Wrong provider_app_id
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
                scope="app",
                provider_app_id="other",
            )
            is False
        )
        # Correct full key
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
                scope="app",
                provider_app_id="secrets",
            )
            is True
        )
        assert len(get_granted_permissions_v2("test-app", SVC_OAUTH)) == 0

    def test_permissions_scoped_per_service(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "DB_URL"})
        grant_permission_v2("test-app", SVC_OAUTH, {"provider": "google", "scope": "email"})

        secrets_grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        oauth_grants = get_granted_permissions_v2("test-app", SVC_OAUTH)
        assert len(secrets_grants) == 1
        assert secrets_grants[0].grant == {"key": "DB_URL"}
        assert len(oauth_grants) == 1
        assert oauth_grants[0].grant == {"provider": "google", "scope": "email"}

    def test_get_all_permissions(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("app-a", SVC_SECRETS, {"key": "X"})
        grant_permission_v2("app-a", SVC_OAUTH, {"provider": "google", "scope": "email"})
        grant_permission_v2("app-b", SVC_SECRETS, {"key": "Y"})

        all_perms = get_all_permissions_v2()
        assert len(all_perms) == 3

        app_a_perms = get_all_permissions_v2(consumer_app_id="app-a")
        assert len(app_a_perms) == 2
        assert {p.service_url for p in app_a_perms} == {SVC_SECRETS, SVC_OAUTH}

    def test_multiple_grants_same_service(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_A"})
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_B"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 2
        granted_keys = {g.grant["key"] for g in grants}
        assert granted_keys == {"SECRET_A", "SECRET_B"}

        revoke_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_A"})
        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1
        assert grants[0].grant["key"] == "SECRET_B"


# ---------------------------------------------------------------------------
# Version resolution — edge cases
# ---------------------------------------------------------------------------


class TestVersionResolutionEdgeCases:
    def test_invalid_specifier_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(RuntimeError, match="Invalid version specifier"):
            resolve_provider(SVC_SECRETS, "not_a_version!!", db)


# ---------------------------------------------------------------------------
# Shortname lookup (consumer manifest → service URL + version)
# ---------------------------------------------------------------------------


_MINIMAL_MANIFEST = """
[app]
name = "{name}"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080
"""


def _install_consumer(db, name: str, perms_toml: str = "") -> str:
    """Insert a consumer app row whose manifest_raw contains the given [[services.v2.consumes]] entries.
    Returns the minted app_id."""
    raw = _MINIMAL_MANIFEST.format(name=name) + perms_toml
    app_id = new_app_id()
    db.execute(
        """INSERT OR REPLACE INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (app_id, name, "0.1.0", f"/tmp/{name}", 9100, "running", raw),
    )
    db.commit()
    return app_id


class TestShortnameLookup:
    def test_resolves_declared_shortname(self, db):
        consumer_id = _install_consumer(
            db,
            "consumer",
            f'\n[[services.v2.consumes]]\nservice = "{SVC_OAUTH}"\nshortname = "oauth"\nversion = ">=0.1.0"\ngrants = []\n',
        )
        service_url, version = lookup_service_by_manifest_shortname(consumer_id, "oauth", db)
        assert service_url == SVC_OAUTH
        assert version == ">=0.1.0"

    def test_unknown_shortname_raises(self, db):
        consumer_id = _install_consumer(
            db,
            "consumer",
            f'\n[[services.v2.consumes]]\nservice = "{SVC_OAUTH}"\nshortname = "oauth"\nversion = ">=0.1.0"\ngrants = []\n',
        )
        with pytest.raises(LookupError, match="not declared"):
            lookup_service_by_manifest_shortname(consumer_id, "missing", db)

    def test_no_manifest_raises(self, db):
        # Consumer row without manifest_raw at all.
        bare_id = new_app_id()
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (bare_id, "bare", "0.1.0", "/tmp/bare", 9101, "running"),
        )
        db.commit()
        with pytest.raises(LookupError, match="No manifest"):
            lookup_service_by_manifest_shortname(bare_id, "anything", db)

    def test_unknown_consumer_raises(self, db):
        with pytest.raises(LookupError, match="No manifest"):
            lookup_service_by_manifest_shortname(new_app_id(), "oauth", db)

    def test_picks_correct_entry_among_many(self, db):
        perms = (
            f'\n[[services.v2.consumes]]\nservice = "{SVC_SECRETS}"\nshortname = "secrets"\nversion = ">=0.1.0"\ngrants = []\n'
            f'\n[[services.v2.consumes]]\nservice = "{SVC_OAUTH}"\nshortname = "oauth"\nversion = "==1.0.0"\ngrants = []\n'
        )
        multi_id = _install_consumer(db, "multi", perms)
        assert lookup_service_by_manifest_shortname(multi_id, "secrets", db) == (SVC_SECRETS, ">=0.1.0")
        assert lookup_service_by_manifest_shortname(multi_id, "oauth", db) == (SVC_OAUTH, "==1.0.0")


# ---------------------------------------------------------------------------
# Provider authorization check (grant_app_scoped)
# ---------------------------------------------------------------------------


class TestProviderAuthCheck:
    """Verify that only registered providers can grant app-scoped permissions."""

    def test_registered_provider_can_grant(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.auth.permissions_v2.get_db", lambda: db)
        provider_id = _add_provider(db, SVC_OAUTH, "oauth-provider", "0.1.0", "/oauth/")
        consumer_id = _install_consumer(db, "consumer")

        # Provider is registered, so a direct grant should work.
        grant_permission_v2(
            consumer_app_id=consumer_id,
            service_url=SVC_OAUTH,
            grant_payload={"provider": "google", "scopes": ["email"]},
            scope="app",
            provider_app_id=provider_id,
        )
        grants = get_granted_permissions_v2(consumer_id, SVC_OAUTH)
        assert len(grants) == 1
        assert grants[0].scope == "app"

    def test_non_provider_is_rejected_by_db_check(self, db):
        """An app that is not a registered provider for a service
        should fail the DB lookup that the grant_app_scoped endpoint
        performs before calling grant_permission_v2."""
        non_provider_id = new_app_id()
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (non_provider_id, "non-provider", "0.1.0", "/tmp/np", 9200, "running"),
        )
        db.commit()

        # Simulate the check the endpoint does.
        row = db.execute(
            "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?",
            (SVC_OAUTH, non_provider_id),
        ).fetchone()
        assert row is None, "non-provider should not be in service_providers_v2"


class TestAccessedDomainUrls:
    """Owner-facing URLs the router hands back (permission-approval, OAuth returns) are
    built on the domain *and* access port the owner is actually browsing, so a
    tunnelled/NAT'd instance (reached on e.g. :8088) keeps working."""

    def _seed_primary(self, db) -> None:
        seed_domains(db, Domain(name="lvh.me", tls=False), [])

    def test_grant_url_carries_browsing_domain_and_port(self, db):
        # The browsing authority comes from the consumer app's Origin header.
        self._seed_primary(db)
        url = approve_grant_url("consumer-id", SVC_SECRETS, {"x": 1}, db, "consumer.lvh.me:8088")
        assert url.startswith("http://lvh.me:8088/approve-permissions-v2?")

    def test_grant_url_falls_back_to_primary_without_origin(self, db):
        # A server-side call has no Origin: stay on the primary, port-less (prior behavior).
        self._seed_primary(db)
        url = approve_grant_url("consumer-id", SVC_SECRETS, {"x": 1}, db, None)
        assert url.startswith("http://lvh.me/approve-permissions-v2?")

    def test_grant_url_is_relative_when_no_domain_known(self, db):
        # No configured domains at all → a relative path rather than a broken absolute URL.
        url = approve_grant_url("consumer-id", SVC_SECRETS, {"x": 1}, db, "consumer.lvh.me:8088")
        assert url.startswith("/approve-permissions-v2?")

    def _request(self, netloc: str, zone: Domain | None) -> Any:
        scope = {ZONE_SCOPE_KEY: zone} if zone is not None else {}
        return types.SimpleNamespace(scope=scope, url=types.SimpleNamespace(netloc=netloc))

    def test_oauth_return_host_uses_browsing_origin(self, db):
        self._seed_primary(db)
        request = self._request("lvh.me:8088", Domain(name="lvh.me", tls=False))
        assert _oauth_return_host(db, request) == "lvh.me:8088"

    def test_oauth_return_host_falls_back_to_primary(self, db):
        self._seed_primary(db)
        request = self._request("lvh.me", None)  # middleware stashed no zone
        assert _oauth_return_host(db, request) == "lvh.me"
