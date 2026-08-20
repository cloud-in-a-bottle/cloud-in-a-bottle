## Logs

Cloud in a Bottle produces several distinct log streams, stored in different places depending on the process.

---

### Router (`compute_space`)

The router writes to two sinks simultaneously via loguru:

**File sink** — `<data_dir>/compute_space.log`

- Written at INFO level and above.
- Truncated on each process restart, so it always contains only the current invocation's logs.
- Rotates automatically at 10 MB mid-run; up to 5 rotated files kept (named `compute_space.YYYY-MM-DD_HH-MM-SS_ffffff.log` alongside the active file). Note: rotation-based retention only fires when the 10 MB limit is actually hit — rotated files from old runs are not pruned until the next mid-run rotation.
- Access via `cb logs` or `cb logs --instance <name>`.

**Stderr sink** — journald (via systemd)

- Written at DEBUG level and above.
- Captured by systemd from the router process's stderr and stored in journald's binary database at `/var/log/journal/`.
- Also contains output from Caddy and CoreDNS, because both are child processes of the router — their stdout is read line-by-line and re-emitted through the router's logger.
- Persists across restarts and accumulates until journald's on-disk size cap is hit. Cloud in a Bottle caps the total journal at 500 MB via a drop-in at `/etc/systemd/journald.conf.d/10-openhost.conf` (`SystemMaxUse=500M`) so the journal alone can't fill the host disk; older entries are discarded once the cap is reached.
- Access via `journalctl -u openhost` or `journalctl -u openhost -f` to follow.

---

### App build and runtimelogs

During deploy and reload, `podman build` output and container start/stop events are streamed to a per-app file:

`<temp_data_dir>/app_temp_data/<app_name>/docker.log`

Container stdout and stderr are written to a per-app file via podman's `k8s-file` log driver:

`<temp_data_dir>/app_temp_data/<app_name>/container.log`

- Archived (not deleted) at the start of each reload. Both logs have the build time appended: `docker.log.20240101_120000`.
- Up to 5 archived build logs are kept per app; older ones are deleted when the limit is exceeded.
- Deleted entirely when the app is removed.
- Access via `cb app logs <name>` (the current runtime log is appended to the build log).
- Runtime logs do not appear in journald, which is used for Cloud in a Bottle status logs

---

### JuiceFS (`openhost-juicefs.service`)

If an S3 archive backend is configured, JuiceFS runs as a separate systemd unit. Its logs go to journald and can be accessed via:

```
journalctl -u openhost-juicefs
```

---

### systemd / journalctl

`journalctl -u openhost` gives a unified view of everything the router process wrote to stderr, including forwarded Caddy and CoreDNS output. This is the right tool for:

- Diagnosing startup failures (before the file sink is initialised)
- Viewing logs across restarts in one stream
- Checking recent router activity without `cb` access: `systemctl status openhost` shows the last few lines

`journalctl -u openhost` does **not** contain:
- App container stdout/stderr (now written to `container.log` via k8s-file)
- The contents of `compute_space.log` (the file sink is separate from journald)

Journald stores logs in a binary indexed format across multiple files. Queries are indexed by unit, priority, and time — they are not a linear scan — but total journal size still affects seek time. Cloud in a Bottle caps the total on-disk journal at 500 MB via `SystemMaxUse` in a drop-in at `/etc/systemd/journald.conf.d/10-openhost.conf`. Operators who need a different limit can add `SystemMaxFiles` or `MaxRetentionSec`, or override `SystemMaxUse`, in a higher-numbered drop-in (e.g. `/etc/systemd/journald.conf.d/20-local.conf`).
