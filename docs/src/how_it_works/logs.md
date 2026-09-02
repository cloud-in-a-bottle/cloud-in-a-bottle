# Logs

## Router logs

The router writes to two places at once.

**A file**, at `<data dir>/persistent_data/openhost/compute_space.log`:

- INFO and above.
- Truncated when the router starts, so it holds only the current run.
- Rotates at 10 MB, keeping 5 rotated files alongside it.
- Read it with `bottle logs`.

**journald**, via systemd:

- DEBUG and above.
- Includes Caddy and CoreDNS output, since both are child processes of the router.
- Survives restarts, so this is where to look when the router died or failed to start before file logging was up.
- Read it with `journalctl -u openhost`, or `-f` to follow. `systemctl status openhost` shows the last few lines.

The journal is capped at 500 MB (`SystemMaxUse` in `/etc/systemd/journald.conf.d/10-openhost.conf`) so it can't fill the disk.

## App logs

Each app has two log files, both under `<data dir>/temporary_data/app_temp_data/<app>/`:

- `docker.log`: build output from `podman build`, plus container start and stop events.
- `container.log`: the container's own stdout and stderr.

`bottle app logs <app>` shows the build log followed by the live container log, which is usually what you want while a deploy is in flight. The dashboard shows the same thing.

On each reload the build log is archived with a timestamp suffix (`docker.log.20240101_120000`); the last 5 are kept. Everything is deleted when the app is removed.

App output does not go to journald.

## Other services

JuiceFS, which backs the [archive tier](./data.md#the-archive-tier), runs as its own unit: `journalctl -u openhost-juicefs`.
