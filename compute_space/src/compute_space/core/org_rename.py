"""Retired: the org-rename reconcile.

This module used to rewrite persisted app repo URLs (``apps.repo_url`` and the
matching git ``origin``) whenever the GitHub owner changed.  It was built for one
threat: GitHub releases an organization's old name when you rename it, and per
GitHub's documentation whoever claims that name can create repositories that
override the redirect entries.  An instance still pointing at the old owner would
then fetch and build someone else's code.

That threat is closed.  ``imbue-openhost`` is now held by us as an empty
organization, so nobody else can take it, and holding a name does not break its
redirects -- only creating a colliding repository does.  Every path an instance
can be holding still resolves, including multi-hop chains such as
``imbue-openhost/openhost-catalog`` (old owner plus two subsequent repo renames).

The reconcile could never have protected the instances that mattered in any case.
Updates are owner-initiated -- the only triggers are ``POST /api/settings/update``
behind owner auth and the ``openhost update`` CLI, with nothing scheduling either
-- so it only ever ran on instances whose owners actively update, which is exactly
the population least at risk.  What remained was cosmetic: making a stored URL
match the current repo path.  That is not worth code which mutates persisted
state and git remotes on every boot across the fleet, particularly given the
rewrite had a correctness bug: it moved only the owner segment, so a stored
``imbue-openhost/openhost-nextcloud`` (any instance that installed before the repo
renames) would have become ``cloud-in-a-bottle/openhost-nextcloud``, which does
not exist -- breaking the very updates it was meant to protect.

``reconcile_app_repo_urls`` is kept as an explicit no-op only because
``web/app.py`` still calls it, and that file is CODEOWNERS-gated.  Removing the
call and deleting this module is a small follow-up that needs that review.
"""

import sqlite3

# The owner that instances installed from before the rename. Retained only so the
# history above is legible; nothing reads it.
OLD_ORG = "imbue-openhost"


def reconcile_app_repo_urls(db: sqlite3.Connection) -> int:
    """Do nothing, and return zero rows changed.

    Persisted URLs under the old owner keep resolving through GitHub's redirects,
    which are safe now that we hold the old organization name. See the module
    docstring for why the rewrite was removed rather than fixed.
    """
    del db  # unused: intentionally inert
    return 0
