import re
import urllib.parse
from pathlib import Path

import git

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.core.util import async_wrap


class RemoteNotSetError(Exception):
    pass


class NoRemoteBranchForLocalBranch(Exception):
    pass


class UnsupportedRepoUrlError(ValueError):
    """A repo URL whose transport we recognise but deliberately don't support.

    Currently only SSH (``ssh://`` and SCP-style ``git@host:path``) — clones
    over SSH would need a deploy key, known_hosts, and an SSH agent that the
    compute_space process doesn't have, so we reject them up front with a clear
    message rather than letting ``git clone`` fail cryptically.
    """


_KNOWN_SCHEMES = {"http", "https", "ssh", "git", "file"}

# SCP-style SSH shorthand: ``[user@]host:path`` with no URL scheme, e.g.
# ``git@github.com:user/repo.git``. We require a ``user@`` and a ``host:``
# before the first slash so we don't misfire on credential URLs like
# ``oauth2:TOKEN@host/path`` (where the ``@`` follows the colon).
_SCP_STYLE_SSH_RE = re.compile(r"^[^/@]+@[^/:]+:")

_SSH_URL_ERROR = (
    "SSH git URLs are not supported (e.g. 'git@github.com:user/repo.git' or "
    "'ssh://git@github.com/user/repo.git'). Please use the HTTPS clone URL "
    "instead, e.g. 'https://github.com/user/repo.git'."
)


def is_ssh_url(repo_url: str) -> bool:
    """True if ``repo_url`` uses the SSH transport.

    Matches both the ``ssh://`` scheme and git's SCP-style shorthand
    (``git@host:path``). Credential-bearing HTTPS-ish URLs such as
    ``oauth2:TOKEN@host/path`` are not SSH and return False.
    """
    if urllib.parse.urlparse(repo_url).scheme == "ssh":
        return True
    return bool(_SCP_STYLE_SSH_RE.match(repo_url))


def _repo_url_hostname(repo_url: str) -> str:
    """Lowercased hostname of ``repo_url``, applying the same bare-hostname
    normalisation as :func:`parse_repo_url` (a scheme-less URL is treated as
    https). Returns "" when no host can be parsed."""
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.scheme not in _KNOWN_SCHEMES:
        parsed = urllib.parse.urlparse("https://" + repo_url)
    return (parsed.hostname or "").lower()


def is_github_repo_url(repo_url: str) -> bool:
    """True if ``repo_url``'s host is github.com (or a subdomain of it).

    Matches on the parsed hostname rather than a substring so a look-alike
    host like ``github.com.evil.example`` or ``notgithub.com`` doesn't gate
    the GitHub OAuth clone fallback (which would otherwise attach a GitHub
    token to a request bound for the wrong host).
    """
    host = _repo_url_hostname(repo_url)
    return host == "github.com" or host.endswith(".github.com")


def parse_repo_url(repo_url: str) -> tuple[str, str | None]:
    """Parse a repo URL with optional @ref suffix (pip-style).

    Returns (base_url, ref) where ref is a branch, tag, or commit hash, or None.

    Raises:
        UnsupportedRepoUrlError: if the URL uses the SSH transport.
    """
    # Reject SSH URLs before the bare-hostname fallback below: an SCP-style
    # URL like "git@github.com:user/repo.git" has no scheme, so it would
    # otherwise be rewritten to a malformed "https://git@github.com:user/..."
    # (git reads "user" as a port) and fail cryptically.
    if is_ssh_url(repo_url):
        raise UnsupportedRepoUrlError(_SSH_URL_ERROR)
    # Allow bare hostnames like "github.com/user/repo" without a scheme.
    # urlparse misidentifies credentials (e.g. "oauth2:TOKEN@host") as a scheme,
    # so we only trust schemes we actually recognise.
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.scheme not in _KNOWN_SCHEMES:
        repo_url = "https://" + repo_url
        parsed = urllib.parse.urlparse(repo_url)
    path = parsed.path
    if "@" in path:
        base_path, ref = path.rsplit("@", 1)
        base_url = parsed._replace(path=base_path).geturl()
        return base_url, ref
    return repo_url, None


def _get_remote(repo: git.Repo) -> git.Remote:
    try:
        return repo.remote("origin")
    except (AttributeError, ValueError) as e:
        raise RemoteNotSetError("remote 'origin' is not set") from e


@async_wrap
def validate_repo(repo_path: Path) -> None:
    """Check if the given path is a valid git repository.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
    """
    git.Repo(repo_path)


@async_wrap
def get_current_ref(repo_path: Path) -> str:
    """Return the current branch name, or the short commit hash if in detached HEAD state."""
    repo = git.Repo(repo_path)
    try:
        return repo.active_branch.name
    except TypeError:
        return repo.head.commit.hexsha[:8]


@async_wrap
def get_head_sha(repo_path: Path) -> str:
    """Return the full HEAD commit SHA."""
    return git.Repo(repo_path).head.commit.hexsha


@async_wrap
def reset_hard(repo_path: Path, sha: str) -> None:
    """Hard-reset the working tree back to ``sha``.

    Used to undo a git pull whose resulting update was refused (e.g. the owner
    has not approved new permissions the pulled manifest declares), so the
    on-disk repo keeps matching the version the app is actually running.
    """
    git.Repo(repo_path).git.reset("--hard", sha)


@async_wrap
def get_branch_name(repo_path: Path) -> str | None:
    """Return the current branch name, or None if HEAD is detached."""
    repo = git.Repo(repo_path)
    try:
        return repo.active_branch.name
    except TypeError:
        return None


def _strip_credentials(url: str) -> str:
    """Remove userinfo (OAuth tokens, passwords) from a URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        host_port = parsed.hostname or ""
        if parsed.port:
            host_port = f"{host_port}:{parsed.port}"
        return parsed._replace(netloc=host_port).geturl()
    return url


@async_wrap
def get_remote_url(repo_path: Path) -> str | None:
    """Returns the remote URL with any credentials stripped.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote
    """
    repo = git.Repo(repo_path)
    url = _get_remote(repo).url
    return _strip_credentials(url) if url else None


@async_wrap
def is_dirty(repo_path: Path) -> bool:
    """
    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
    """
    return git.Repo(repo_path).is_dirty(untracked_files=True)


@async_wrap
def fetch(repo_path: Path) -> None:
    """
    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote
    """
    repo = git.Repo(repo_path)
    _get_remote(repo).fetch()


@async_wrap
def count_commits_vs_remote(repo_path: Path) -> tuple[int, int]:
    """Returns (ahead, behind) commit counts compared to the tracking branch.

    Raises:
        git.InvalidGitRepositoryError: if the path is not a git repository
        git.NoSuchPathError: if the path does not exist
        RemoteNotSetError: if the repository has no 'origin' remote or no tracking branch is set
    """
    repo = git.Repo(repo_path)
    try:
        branch = repo.active_branch
    except TypeError:
        # detached head; no new commits
        return 0, 0
    tracking = branch.tracking_branch()

    if tracking is None:
        raise NoRemoteBranchForLocalBranch(f"{branch.name} has no tracking branch set")

    behind = int(repo.git.rev_list("--count", f"{branch}..{tracking}"))
    ahead = int(repo.git.rev_list("--count", f"{tracking}..{branch}"))
    return ahead, behind


@async_wrap
def init_repo_if_nonexistent(repo_path: Path) -> None:
    """Initialise a git repo if one doesn't already exist."""
    try:
        git.Repo(repo_path)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        git.Repo.init(repo_path, initial_branch="main")


@async_wrap
def set_remote_url(repo_path: Path, url: str) -> None:
    """Set or create the 'origin' remote to the given URL."""
    repo = git.Repo(repo_path)
    try:
        with _get_remote(repo).config_writer as cw:
            cw.set("url", url)
    except RemoteNotSetError:
        repo.create_remote("origin", url)


@async_wrap
def hard_checkout_ref(repo_path: Path, ref: str) -> None:
    """set local state to match origin/ref, checking out if a branch or detached head if a commit hash."""
    repo = git.Repo(repo_path)
    remote_ref = f"origin/{ref}"
    try:
        repo.refs[remote_ref]
        # It's a branch on the remote — create/reset local branch tracking it
        repo.git.checkout("-fB", ref, remote_ref)
        repo.heads[ref].set_tracking_branch(repo.refs[remote_ref])
    except IndexError:
        # Not a remote branch — treat as a commit hash, detached HEAD
        repo.git.checkout("-f", ref)
    # checkout -f resets tracked files but leaves untracked files behind, which can
    # shadow modules removed/renamed between revisions. Match origin/ref fully.
    repo.git.clean("-fd")


def github_web_url_from_remote_url(remote_url: str, branch: str | None) -> str | None:
    """Browsable ``github.com`` URL for ``remote_url`` at ``branch``, or None.

    Converts an origin remote (HTTPS, credential-bearing, or SCP-style SSH) into a
    ``https://github.com/<owner>/<repo>[/tree/<branch>]`` link. Returns None when the
    remote isn't a GitHub repo so callers can simply hide the link.
    """
    if not is_github_repo_url(remote_url):
        return None
    if _SCP_STYLE_SSH_RE.match(remote_url):
        # SCP shorthand (git@github.com:owner/repo.git): path follows the first colon.
        path = remote_url.split(":", 1)[1]
    else:
        parsed = urllib.parse.urlparse(remote_url)
        if parsed.scheme not in _KNOWN_SCHEMES:
            parsed = urllib.parse.urlparse("https://" + remote_url)
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        return None
    url = f"https://github.com/{path}"
    if branch:
        url = f"{url}/tree/{urllib.parse.quote(branch)}"
    return url


def github_web_url_from_local_repo(repo_path: Path) -> str | None:
    """Browsable GitHub link to the checkout at ``repo_path`` (current branch/fork).

    Best-effort and non-raising: it only surfaces a "view source" link, so a tarball
    deploy (no ``.git``), a detached HEAD, a missing ``origin``, or a non-GitHub remote
    all simply yield None instead of an error.
    """
    try:
        repo = git.Repo(repo_path)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        return None
    try:
        branch: str | None = repo.active_branch.name
    except TypeError:
        branch = None
    try:
        remote_url = _get_remote(repo).url
    except RemoteNotSetError:
        return None
    if not remote_url:
        return None
    return github_web_url_from_remote_url(_strip_credentials(remote_url), branch)


# The nav's "view source" link. Resolved once: it describes the code the process
# is serving, which is fixed for its lifetime. ``set-remote`` rewrites origin
# without restarting, but that pins where the next update comes from, not where
# the running code came from.
SOURCE_URL = github_web_url_from_local_repo(OPENHOST_PROJECT_DIR)
