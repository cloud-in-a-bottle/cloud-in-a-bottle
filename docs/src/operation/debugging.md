# Debugging

When something is wrong and the dashboard isn't telling you enough.

## Where to start

| Symptom | Look at |
|---|---|
| Dashboard unreachable | `sudo systemctl status openhost`, then `sudo journalctl -u openhost -n 200` |
| An app won't build | `bottle app logs <app>` (the build log is the first half of it) |
| An app builds but 502s | The app's own container log, same command; check it binds the port from its manifest |
| Apps stopped on their own | Free disk. The [storage guard](../how_it_works/data.md#storage-guard) stops apps below 500 MB free |
| A domain has no certificate | `journalctl -u openhost -f` during acquisition; DNS-01 needs the zone delegated (see [Routing](../how_it_works/routing.md#tls-certificates)) |

## The service

Cloud in a Bottle runs as a single systemd unit, `openhost`, as the unprivileged `host` user.

```bash
sudo systemctl status openhost
sudo systemctl restart openhost
sudo journalctl -u openhost -f
```

| What | Where |
|---|---|
| Service | `openhost` (systemd) |
| Code | `/home/host/openhost` |
| Config | `/home/host/.openhost/local_compute_space/config.toml` |
| Data | `/home/host/.openhost/local_compute_space/`, see [Overview](../how_it_works/overview.md#on-the-machine) |

Config changes take effect on restart. Caddy and CoreDNS are children of this unit, so restarting it restarts them too.

## Logs

`bottle logs` and `journalctl -u openhost` cover the router; `bottle app logs <app>` covers one app. Which log holds what, and where the files live, is in [Logs](../how_it_works/logs.md).

## Diagnostics

```bash
bottle diagnostics              # instance-wide state, as JSON
bottle app diagnostics <app>    # one app
```

Both are meant to be pasted into an issue or handed to an agent.

## Getting a shell

The dashboard has one on the machine at `/terminal/`, which is the quickest way in when SSH isn't set up. Otherwise `bottle instance ssh`, or `ssh host@<your-domain>`. For a shell inside an app's container, `bottle app ssh <app>`.

## Updating by hand

The update button on the dashboard's settings page is the normal path: it checks the configured git remote, pulls, runs any host-level migrations, syncs dependencies, and restarts. A progress page streams it, and a stand-in server holds ports 80 and 443 during the restart.

If the dashboard is unreachable, the same thing over SSH:

```bash
sudo openhost_system_agent update apply
```
