"""Unit tests for the ``installer`` v2 service core helpers.

The end-to-end install + service-proxy flow is exercised in the e2e
integration tests; here we focus on the small, deterministic units:
``check_install_allowed`` grant matching and the constants the proxy
relies on.
"""

from __future__ import annotations

import pytest

from compute_space.core.installer import GRANT_KEY_CAPABILITY
from compute_space.core.installer import GRANT_KEY_REPO_URL_PREFIX
from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import INSTALL_CAPABILITY
from compute_space.core.installer import InstallError
from compute_space.core.installer import check_install_allowed


class TestCheckInstallAllowed:
    def test_empty_grants_denied(self) -> None:
        assert check_install_allowed("https://github.com/foo/bar", []) is not None

    def test_matching_prefix_allowed(self) -> None:
        grants = [
            {
                GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY,
                GRANT_KEY_REPO_URL_PREFIX: "https://github.com/imbue-openhost/",
            },
        ]
        assert check_install_allowed("https://github.com/imbue-openhost/app-catalog", grants) is None

    def test_non_matching_prefix_denied(self) -> None:
        grants = [
            {
                GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY,
                GRANT_KEY_REPO_URL_PREFIX: "https://github.com/imbue-openhost/",
            },
        ]
        assert check_install_allowed("https://github.com/evil/badapp", grants) is not None

    def test_wildcard_prefix_allows_anything(self) -> None:
        grants = [{GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: "*"}]
        assert check_install_allowed("https://anywhere.invalid/repo", grants) is None

    def test_empty_prefix_allows_anything(self) -> None:
        grants = [{GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: ""}]
        assert check_install_allowed("https://anywhere.invalid/repo", grants) is None

    def test_wrong_capability_ignored(self) -> None:
        grants = [
            {GRANT_KEY_CAPABILITY: "read_logs", GRANT_KEY_REPO_URL_PREFIX: "*"},
        ]
        assert check_install_allowed("https://github.com/foo/bar", grants) is not None

    def test_missing_capability_field_ignored(self) -> None:
        grants = [{GRANT_KEY_REPO_URL_PREFIX: "*"}]
        assert check_install_allowed("https://github.com/foo/bar", grants) is not None

    def test_non_string_prefix_ignored(self) -> None:
        grants = [{GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: 123}]
        # Single bad grant: not matched; deny.
        assert check_install_allowed("https://github.com/foo/bar", grants) is not None

    def test_first_matching_grant_wins(self) -> None:
        grants = [
            {GRANT_KEY_CAPABILITY: "noop", GRANT_KEY_REPO_URL_PREFIX: "*"},
            {
                GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY,
                GRANT_KEY_REPO_URL_PREFIX: "https://github.com/imbue-openhost/",
            },
        ]
        assert check_install_allowed("https://github.com/imbue-openhost/app-catalog", grants) is None
        assert check_install_allowed("https://github.com/other/", grants) is not None

    def test_service_url_constant_is_stable(self) -> None:
        # The catalog manifest pins this string; if it ever needs to
        # change we should bump the service version too.
        assert INSTALLER_SERVICE_URL == "github.com/imbue-openhost/openhost/services/installer"


class TestInstallError:
    def test_default_status_code(self) -> None:
        err = InstallError("oops")
        assert err.message == "oops"
        assert err.status_code == 400

    def test_custom_status_code(self) -> None:
        err = InstallError("nope", status_code=401)
        assert err.status_code == 401

    def test_is_exception(self) -> None:
        with pytest.raises(InstallError):
            raise InstallError("boom")
