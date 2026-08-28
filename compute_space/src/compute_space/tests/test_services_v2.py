import pytest

from compute_space.core.app_id import new_app_id
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.core.proxy_target import LocalPort
from compute_space.core.service_interface.registry import lookup_shortname
from compute_space.core.service_interface.resolve import resolve_provider

SVC_SECRETS = "github.com/org/repo/services/secrets"
SVC_OAUTH = "github.com/org/repo/services/oauth"


def _add_provider(db, service_url, app_name, version, endpoint, port=9000, status="running", default=True) -> str:
    """Insert a provider app row + service_providers_v2 entry. Returns the minted app_id."""
    app_id = new_app_id()
    db.execute(
        """INSERT OR REPLACE INTO apps (app_id, name, version, repo_path, local_port, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (app_id, app_name, "0.0.0", f"/tmp/{app_name}", port, status),
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
        assert provider.version == "0.2.0"
        assert provider.endpoint == "/_svc/"
        assert provider.target == LocalPort(9000)

    def test_exact_version(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "1.0.0", "/_svc/")
        assert resolve_provider(SVC_SECRETS, "==1.0.0", db).version == "1.0.0"

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
        service_url, version = lookup_shortname(consumer_id, "oauth", db)
        assert service_url == SVC_OAUTH
        assert version == ">=0.1.0"

    def test_unknown_shortname_raises(self, db):
        consumer_id = _install_consumer(
            db,
            "consumer",
            f'\n[[services.v2.consumes]]\nservice = "{SVC_OAUTH}"\nshortname = "oauth"\nversion = ">=0.1.0"\ngrants = []\n',
        )
        with pytest.raises(LookupError, match="not declared"):
            lookup_shortname(consumer_id, "missing", db)

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
            lookup_shortname(bare_id, "anything", db)

    def test_unknown_consumer_raises(self, db):
        with pytest.raises(LookupError, match="No manifest"):
            lookup_shortname(new_app_id(), "oauth", db)

    def test_picks_correct_entry_among_many(self, db):
        perms = (
            f'\n[[services.v2.consumes]]\nservice = "{SVC_SECRETS}"\nshortname = "secrets"\nversion = ">=0.1.0"\ngrants = []\n'
            f'\n[[services.v2.consumes]]\nservice = "{SVC_OAUTH}"\nshortname = "oauth"\nversion = "==1.0.0"\ngrants = []\n'
        )
        multi_id = _install_consumer(db, "multi", perms)
        assert lookup_shortname(multi_id, "secrets", db) == (SVC_SECRETS, ">=0.1.0")
        assert lookup_shortname(multi_id, "oauth", db) == (SVC_OAUTH, "==1.0.0")


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
