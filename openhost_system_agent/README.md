# openhost_system_agent

A privileged agent that manages host-level system state for Cloud in a Bottle instances.
It runs as root (via `sudo`) and handles three concerns:

1. **System migrations** — idempotent, versioned steps that bring the host into
   the expected configuration (kernel settings, container runtime, systemd units,
   etc.). Migrations pick up where `ansible/setup.yml` leaves off and let us ship
   host-side changes without re-running the full Ansible playbook.

2. **Code updates** — fetching and applying new versions of the Cloud in a Bottle software
   from the configured git remote.

3. **Seamless-update support** — while an update applies, the agent appends a
   JSONL progress log (`<data dir>/updater/progress.jsonl`) that the dashboard's
   `/updating` page streams, and just before the final restart it launches a
   detached downtime mini-server (a transient systemd unit,
   `openhost-updater.service`) that holds 80/443 with the on-disk TLS certs and
   serves the update page until the new compute_space is back.

## Running

The agent must be run as root:

```
sudo openhost_system_agent <command>
```

Key subcommands:

- `update fetch` — fetch latest tags/code from remote and report whether a newer release is available
- `update apply` — apply the pending update: walk the release tags as stepping stones (running each tag's migrations → `pixi install` → checkout next), record progress to the updater log, launch the downtime server, then restart openhost. The freshly booted compute_space appends the terminal "done" entry
- `status` — show current system version and whether migrations are pending
- `updater launch|serve|stop|set-token|clear-token` — internals of the seamless-update downtime server (normally driven by compute_space, not by hand): `set-token` persists the browser token that gates the live-progress endpoint (resetting the progress log), `launch` starts the detached server, `stop` releases 80/443, `clear-token` removes the token after a failed apply

The updater paths (progress log, token, certs) resolve from `OPENHOST_DATA_DIR`
when set (compute_space forwards it on every call via `sudo env` and into the
detached unit via `systemd-run --setenv`), defaulting to the production data dir.

Updates track **release tags**, not a branch. The host checkout normally sits on
a release tag (a detached HEAD by git's definition, which is expected here). To
pin an instance to a specific branch or commit instead of the latest tag, use
`update set_remote <url>@<ref>`; updates then walk the tags as usual but end on
that ref.

## Adding a New Migration

1. Create a new file in `openhost_system_agent/src/openhost_system_agent/migrations/versions/`
   named `v{N}_{description}.py` where `N` is the next integer after the current
   highest version.

2. Define a class inheriting from `SystemMigration` with `version = N` and an
   `up()` method containing the migration logic. All steps should be idempotent —
   assume migrations may be re-run on already-configured hosts.

   ```python
   from openhost_system_agent.migrations.base import SystemMigration
   from openhost_system_agent.migrations.helpers import run, write_file

   class Migration000NMyChange(SystemMigration):
       version = N

       def up(self) -> None:
           write_file("/etc/example.conf", "content\n", mode=0o644)
           run("systemctl", "restart", "some-service")
   ```

3. Register the migration in `services.py` by appending an instance to `REGISTRY`.
   Versions must be contiguous starting at 2 — the validator will error if they're not.

## Helpers

`migrations/helpers.py` provides utilities for common migration tasks:

- `run(*cmd)` — run a subprocess, raising on failure
- `write_file(path, content, *, mode=0o600)` — write a file with explicit permissions,
  creating parent dirs as needed. Note: runs as root, so files are owned root:root.
  Pass `mode=0o644` for world-readable config files (sysctl snippets, systemd units, etc.)
- `ensure_line(path, line)` — append a line to a file if not already present
- `set_sshd_option(key, value)` — set or update an sshd_config option
- `get_host_uid()` — return the UID of the `host` user
