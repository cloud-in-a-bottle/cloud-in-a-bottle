"""Tests for parse_repo_url handling of PAT/credential URLs.

parse_repo_url must correctly handle URLs with embedded credentials
(e.g. oauth2:TOKEN@gitlab.com/user/repo) by recognizing that 'oauth2'
is not a real URL scheme and prepending https://.
"""

import pytest

from compute_space.core.apps import parse_repo_url
from compute_space.core.git_ops import is_github_repo_url
from compute_space.core.git_ops import is_ssh_url


class TestParseRepoUrlCredentialUrls:
    """URLs with credentials like oauth2:TOKEN@host should get https:// prepended."""

    def test_gitlab_pat_without_scheme(self):
        """oauth2:TOKEN@gitlab.com/user/repo.git should become https:// URL."""
        url, ref = parse_repo_url("oauth2:glpat-xxxx@gitlab.com/user/repo.git")
        assert ref is None
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"

    def test_gitlab_pat_with_https_scheme(self):
        """https://oauth2:TOKEN@gitlab.com/... should be left alone."""
        url, ref = parse_repo_url("https://oauth2:glpat-xxxx@gitlab.com/user/repo.git")
        assert ref is None
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"

    def test_generic_user_pass_without_scheme(self):
        """user:pass@host/repo should get https:// prepended."""
        url, ref = parse_repo_url("user:pass@github.com/user/repo.git")
        assert ref is None
        assert url == "https://user:pass@github.com/user/repo.git"


class TestParseRepoUrlKnownSchemes:
    """Supported schemes (http, https, git, file) should be preserved."""

    def test_https_url(self):
        url, ref = parse_repo_url("https://github.com/user/repo.git")
        assert url == "https://github.com/user/repo.git"
        assert ref is None

    def test_http_url(self):
        url, ref = parse_repo_url("http://github.com/user/repo.git")
        assert url == "http://github.com/user/repo.git"
        assert ref is None

    def test_git_url(self):
        url, ref = parse_repo_url("git://github.com/user/repo.git")
        assert url == "git://github.com/user/repo.git"
        assert ref is None

    def test_file_url(self):
        url, ref = parse_repo_url("file:///home/user/repo")
        assert url == "file:///home/user/repo"
        assert ref is None


class TestParseRepoUrlSshRejected:
    """SSH URLs are unsupported; parse_repo_url rejects them with a clear error."""

    @pytest.mark.parametrize(
        "ssh_url",
        [
            "git@github.com:user/repo.git",
            "git@github.com:user/repo.git@main",
            "git@gitlab.com:group/subgroup/repo.git",
            "ssh://git@github.com/user/repo.git",
            "ssh://git@github.com:22/user/repo.git",
        ],
    )
    def test_ssh_urls_raise(self, ssh_url):
        with pytest.raises(ValueError):
            parse_repo_url(ssh_url)

    def test_error_message_points_to_https(self):
        with pytest.raises(ValueError) as excinfo:
            parse_repo_url("git@github.com:user/repo.git")
        assert "HTTPS" in str(excinfo.value)


class TestIsSshUrl:
    """is_ssh_url distinguishes SSH transport from HTTPS/credential URLs."""

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:user/repo.git",
            "git@github.com:user/repo.git@main",
            "git@gitlab.com:group/subgroup/repo.git",
            "ssh://git@github.com/user/repo.git",
            "ssh://git@github.com:22/user/repo.git",
        ],
    )
    def test_ssh(self, url):
        assert is_ssh_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/user/repo.git",
            "http://github.com/user/repo.git",
            "https://git@github.com/user/repo.git",
            "git://github.com/user/repo.git",
            "file:///home/user/repo",
            "github.com/user/repo.git",
            # Credential-bearing URLs: the '@' follows the colon, not an scp host.
            "oauth2:glpat-xxxx@gitlab.com/user/repo.git",
            "user:pass@github.com/user/repo.git",
        ],
    )
    def test_not_ssh(self, url):
        assert is_ssh_url(url) is False


class TestIsGithubRepoUrl:
    """is_github_repo_url matches on the parsed host, not a substring, so
    look-alike hosts don't gate the GitHub OAuth fallback."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/user/repo.git",
            "https://github.com/user/repo.git@main",
            "http://github.com/user/repo",
            "https://x-access-token:TOKEN@github.com/user/repo.git",
            "github.com/user/repo.git",  # bare host normalised to https
            "https://api.github.com/repos/user/repo",  # subdomain
        ],
    )
    def test_github(self, url):
        assert is_github_repo_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com.evil.example/user/repo.git",
            "https://notgithub.com/user/repo.git",
            "https://github.community/user/repo.git",
            "https://gitlab.com/user/repo.git",
            "https://evil.example/?github.com",
        ],
    )
    def test_not_github(self, url):
        assert is_github_repo_url(url) is False


class TestParseRepoUrlBareHostname:
    """Bare hostnames without scheme should get https:// prepended."""

    def test_bare_hostname(self):
        url, ref = parse_repo_url("github.com/user/repo.git")
        assert url == "https://github.com/user/repo.git"
        assert ref is None


class TestParseRepoUrlRefSuffix:
    """@ref suffix in the path should still be parsed correctly."""

    def test_https_with_ref(self):
        url, ref = parse_repo_url("https://github.com/user/repo.git@main")
        assert url == "https://github.com/user/repo.git"
        assert ref == "main"

    def test_pat_url_with_ref(self):
        """PAT URL with @ref at the end — ref should be parsed from path, not from credentials."""
        url, ref = parse_repo_url("https://oauth2:glpat-xxxx@gitlab.com/user/repo.git@v2.0")
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"
        assert ref == "v2.0"
