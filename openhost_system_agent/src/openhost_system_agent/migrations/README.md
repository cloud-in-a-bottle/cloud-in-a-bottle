# System Migrations

System migrations modify host-level state (apt packages, systemd units, toolchain versions, sysctls, etc.) that can't
be managed through normal code deploys. They run as root via `sudo openhost_system_agent update apply`.

## How an update works

Updates are **tag-based**: by default hosts update to the latest semver tag, not the latest commit. When a host is
behind, it walks forward through intermediate tags one at a time:

```
for each tag after current, up to the destination:
    checkout tag → run that tag's apply_after_checkout.py →
    migrations → pixi install
finally: restart openhost
```

The **destination** is normally the latest release tag. A host can instead be pinned to a specific branch or commit
(the **target ref**) — see "Pinning to a target ref" below. When pinned, the release tags are still walked as
stepping stones (only those the target contains), and the pinned ref is the final hop.

Running each step under the checked-out tag's code is the critical design choice. This means:

- Each tag's code controls its own migration + install sequence.
- Migrations run *before* `pixi install`, so a toolchain upgrade (like pixi) takes effect before deps are installed and
  the lockfile format can change in the same release.

`update apply` execs into `apply_after_checkout.py` and never returns a result: on success it restarts openhost so the
new code takes over (which may kill the apply process itself — that's fine, systemd completes the restart), and on
failure it exits non-zero with the error on stderr. The freshly-started compute_space reads the migration log to see
what happened.

## Writing a migration

1. Create `migrations/versions/v{NNNN}_{name}.py`:

```python
from openhost_system_agent.migrations.base import SystemMigration

class Migration{NNNN}{Name}(SystemMigration):
    version = {NNNN}

    def up(self) -> None:
        # idempotent host-level changes here
        ...
```

2. Register it in `migrations/registry.py` (append to `REGISTRY`).

3. Migrations must be **idempotent** — safe to re-run if a previous attempt failed partway through and is retried.

4. Versions must be **contiguous** starting at 2 (v1 is the ansible baseline).

## The stepping-stone guarantee

The tag walk ensures a host that has been offline for a long time can catch up safely by walking through each
intermediate tag rather than jumping straight to the latest.

### The algorithm

1. `apply_update` fetches tags, finds the next step toward the destination (`_next_step`), checks it out, and launches
   `apply_after_checkout.py`.
2. `apply_after_checkout.py` runs migrations → pixi install at that tag's code.
3. It then asks `_next_step` for the next ref. If there is one, it checks it out and `os.execv`s into itself —
   replacing the process with that ref's code.
4. This repeats until `_next_step` returns `None` (the destination is reached). Then it restarts openhost and the host
   is up to date.

`_next_step` walks the release tags in ascending order, then (if a target ref is pinned) hops to the target as the
final step. It is careful to (a) treat "already on the target commit" as terminal *before* walking tags, and (b) only
step through tags that are ancestors of the pinned target. Without both, a target that does not contain the newest tag
(a branch cut from an older release, or a rollback pin) would oscillate forever between the newest tag and the target.

## Pinning to a target ref

By default the destination is the latest release tag. To pin a host to a specific branch or commit, append `@<ref>` to
the remote URL (e.g. via `update set_remote` or the dashboard): `https://github.com/cloud-in-a-bottle/cloud-in-a-bottle@my-branch`.
This persists `openhost.target-ref` in git config. Updates then walk the release tags the target contains as stepping
stones and end on the target's tip instead of the latest tag. Passing a URL with no `@<ref>` clears the pin and
restores latest-tag behavior.

Because the pin is persisted, be careful not to round-trip a resolved ref (like the current tag) back into the remote
URL — doing so would pin the host to that tag and freeze it there.

## What to be aware of

### Migrations run before `pixi install`, with a restricted import surface

Migrations run *before* this tag's `pixi install`, so the env still holds the *previous* tag's dependencies. A
migration may only use:
- Python stdlib and `subprocess` (the normal way to do host-level work)
- Packages already installed in the previous env (attr, loguru, and anything else in the prior lockfile)

This is rarely a constraint — migrations change host state (apt, sysctls, systemd, files) via `subprocess`, not the
project's Python deps. Note that `services.py` imports every migration module at startup, so a migration module must
not import a new dependency at module top level either.

If a migration genuinely needs a new dependency from this tag's lockfile, run `subprocess.run([PIXI_BIN, "install"])`
at the top of its `up()` first. (Adding the dependency in a *prior* release so it's already installed is cleaner when
possible.)

### No `down()` migrations

Migrations are forward-only. Host state (apt packages, systemd units, sysctls) rarely reverses cleanly. Rely on
idempotency and retry to converge. If a migration is wrong, write a new migration that fixes it.

### Migration log

Applied migrations are tracked in `/etc/openhost/migrations.jsonl` — one JSON line per attempt (success or failure).
The host's current version is the highest successful entry. Failed entries are logged but don't advance the version, so
retries pick up from the last success.

### Git remote URL changes

If the openhost repo moves to a new GitHub org or URL, GitHub's
[repository redirects](https://github.blog/news-insights/product-news/repository-redirects-are-here/) handle HTTPS
clones automatically — old URLs redirect to the new location indefinitely. So for hosts using HTTPS remotes (the
default), a rename is transparent: `git fetch` follows the redirect without any migration.
