"""Move persisted GitHub owner references off the old org onto the new one.

An instance stores the URL it installed each app from in ``apps.repo_url``, and
the app's checkout carries the same URL as its git ``origin``.  Both outlive the
install, so after the GitHub org is renamed every deployed instance keeps
fetching through the old owner name.

That works -- GitHub redirects a renamed owner for both git and the API -- but
only until someone claims the freed-up name.  GitHub releases the old
organization name for anyone to register, and per GitHub's documentation the new
holder "can create repositories that override the redirect entries".  An
instance that still points at the old owner would then fetch an attacker's
repository and build it, so the redirect is a migration window, not a
destination.

Hence this reconcile.  It runs on every boot rather than as a one-shot versioned
migration so that it can land inert (see ``ORG_RENAME_COMPLETE``) and start
working on whichever release an owner eventually installs.  It is idempotent, and
it never raises: a boot must not fail because a rewrite did not apply.

What this reconcile is NOT is a guarantee.  Updates are owner-initiated -- the
only triggers are ``POST /api/settings/update`` behind owner auth and the
``openhost update`` CLI, and nothing schedules them -- so an instance can sit on
pre-rename code indefinitely and never run this code at all.  Any instance that
never updates keeps resolving through the owner redirect forever.

That means the redirect, not this reconcile, is what protects the long tail, so
the old organization name must stay in our hands.  The plan is to re-register
``imbue-openhost`` immediately after the rename and hold it empty.  Holding the
name does not break the redirects -- only creating a repository that collides with
one does -- so a never-updating instance keeps resolving indefinitely.  Precedent:
``dotcloud`` and ``elasticsearch`` both still exist with zero public repos while
their redirects still resolve.

The load-bearing assumption is therefore that we win that re-registration.  If we
ever lose it, instances still on the old owner would fetch and build whatever the
new holder publishes, so treat the gap between renaming and re-registering as an
incident window: have the placeholder ready before renaming.

This reconcile is hygiene on top of that: it moves the instances that do update
off the old namespace, so the redirect stops being load-bearing for them.

Sequencing matters, and is why the owner name and the decision to act on it are
separate constants.  ``NEW_ORG`` is settled data; ``ORG_RENAME_COMPLETE`` is the
switch.  Rewriting before the org is actually renamed would point instances at
an owner that does not exist yet, which is strictly worse than the redirect
dependency it is meant to remove.  Flip ``ORG_RENAME_COMPLETE`` only in the
release that ships with (or after) the rename.
"""

import sqlite3
import urllib.parse

import git

from compute_space.core.git_ops import is_github_repo_url
from compute_space.core.git_ops import is_ssh_url
from compute_space.core.logging import logger

# The owner every currently-deployed instance has persisted.
OLD_ORG = "imbue-openhost"

# The owner to move to.  Decided; the GitHub org has not been renamed yet.
NEW_ORG = "cloud-in-a-bottle"

# The activation switch, deliberately separate from the name above.  While this
# is False the reconcile is a total no-op, which is the correct state until the
# org has actually been renamed: rewriting early would point instances at an
# owner that does not resolve.  Flip this in the release that ships with (or
# after) the rename.
ORG_RENAME_COMPLETE = False


def enabled() -> bool:
    """True when persisted owners should be rewritten.

    Read at call time so flipping ``ORG_RENAME_COMPLETE`` (and patching it in
    tests) takes effect.
    """
    return bool(ORG_RENAME_COMPLETE and NEW_ORG and NEW_ORG != OLD_ORG)


def rewrite_owner(repo_url: str, old_org: str | None = None, new_org: str | None = None) -> str | None:
    """Return ``repo_url`` with its GitHub owner moved ``old_org`` -> ``new_org``.

    ``old_org``/``new_org`` default to the module constants, read at call time
    rather than bound as default arguments so that flipping ``NEW_ORG`` (and
    patching it in tests) takes effect.

    Returns None when the URL should be left alone, which covers: the reconcile
    being disabled (``new_org`` empty), a non-GitHub host, a look-alike host
    such as ``github.com.evil.example``, an SSH URL, and any owner other than
    ``old_org``.

    Only the owner segment is touched.  The repository name, any ``@ref``
    suffix, a ``.git`` suffix, and any query or fragment are preserved --
    including for a repository whose *name* happens to contain the old owner
    string.
    """
    old_org = OLD_ORG if old_org is None else old_org
    new_org = NEW_ORG if new_org is None else new_org
    if not new_org or not old_org or new_org == old_org:
        return None
    if not repo_url or is_ssh_url(repo_url):
        return None
    if not is_github_repo_url(repo_url):
        return None

    # Normalise a scheme-less "github.com/owner/repo" so the owner is always
    # the first path segment, then restore the original shape afterwards.
    had_scheme = "://" in repo_url
    parsed = urllib.parse.urlparse(repo_url if had_scheme else "https://" + repo_url)
    segments = parsed.path.split("/")
    # path is "/owner/repo..." so segments == ["", owner, repo, ...]
    if len(segments) < 3 or not segments[1]:
        return None
    # GitHub owner names are case-insensitive; compare accordingly but write the
    # new owner exactly as configured.
    if segments[1].casefold() != old_org.casefold():
        return None

    segments[1] = new_org
    rebuilt = parsed._replace(path="/".join(segments)).geturl()
    if not had_scheme:
        rebuilt = rebuilt.removeprefix("https://")
    return rebuilt if rebuilt != repo_url else None


def _reconcile_checkout(repo_path: str, expected_url: str) -> str | None:
    """Point an app checkout's origin at ``expected_url``.

    Returns a description of what changed, or None if nothing needed doing.
    Never raises: a missing or broken checkout is not a reason to fail boot,
    and the DB row is the value that matters for the next clone.

    Uses gitpython directly rather than git_ops.get_remote_url/set_remote_url
    because those are @async_wrap coroutines and this runs on the sync boot
    path; bridging an event loop here would buy nothing over four lines of
    gitpython.
    """
    if not repo_path:
        return None
    try:
        repo = git.Repo(repo_path)
        current = repo.remotes.origin.url
    except Exception:
        return None
    rewritten = rewrite_owner(current)
    if rewritten is None:
        return None
    try:
        with repo.remotes.origin.config_writer as cw:
            cw.set("url", rewritten)
    except Exception as exc:
        logger.warning("org_rename: could not update origin in %s: %s", repo_path, exc)
        return None
    return f"{current} -> {rewritten}"


def reconcile_app_repo_urls(db: sqlite3.Connection) -> int:
    """Rewrite persisted app repo URLs (and their checkouts' origin) to NEW_ORG.

    Idempotent, and a no-op until ``ORG_RENAME_COMPLETE``.  Returns the number
    of app rows changed.  Never raises.
    """
    if not enabled():
        return 0

    changed = 0
    try:
        rows = list(db.execute("SELECT app_id, name, repo_url, repo_path FROM apps WHERE repo_url IS NOT NULL"))
    except Exception as exc:
        logger.warning("org_rename: could not read apps: %s", exc)
        return 0

    for row in rows:
        repo_url = row["repo_url"]
        rewritten = rewrite_owner(repo_url)
        if rewritten is None:
            continue
        try:
            db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (rewritten, row["app_id"]))
            db.commit()
        except Exception as exc:
            logger.warning("org_rename: could not update repo_url for %s: %s", row["name"], exc)
            continue
        changed += 1
        logger.info("org_rename: %s repo_url %s -> %s", row["name"], repo_url, rewritten)
        moved = _reconcile_checkout(row["repo_path"], rewritten)
        if moved:
            logger.info("org_rename: %s origin %s", row["name"], moved)

    if changed:
        logger.info("org_rename: moved %d app(s) from %s to %s", changed, OLD_ORG, NEW_ORG)
    return changed
