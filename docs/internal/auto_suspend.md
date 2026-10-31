# Auto-suspend

A hosting platform can pause (snapshot) an instance's VM after a few minutes without inbound requests, and resume it on the next one. This page is the contract between that host and the instance.

## What the host does

- **Asks before suspending.** `GET https://<domain>/api/auto-suspend-status` with `Authorization: Bearer <token>`, where the token is the one the host put in `config.toml` (see below). Suspend only on a `200` whose `busy_reasons` is empty. Treat everything else as busy and ask again later, in particular:
  - `503` with `{"error":"updating"}`, or no answer at all: an openhost update has the router stopped, and the downtime updater (or nothing, briefly) holds the port. Updates have a hard one-hour cap, after which the router always comes back.
  - `404`: the instance has no status token configured. An unclaimed instance also answers `404`, because only the setup page is served until the owner claims it.
- **Doesn't count its own status requests as activity**, or the instance never goes idle.
- **Sets the guest clock on resume**, for example with the QEMU guest agent's `guest-set-time`. A resumed VM's clock still reads the time it was paused until something corrects it, and the instance's catch-up (below) and TLS both depend on it.
- **Wakes every instance at least once a day**, at or soon after its `auto_update_time_utc` when it has one, and otherwise at times spread across instances, and keeps it up for the usual idle timeout. Scheduled work catches up on that wake, and the status endpoint keeps the instance awake until it's done.

## `busy_reasons`

A list, empty when the instance can be suspended. New reasons may be added, so a host should treat any reason it doesn't recognise as busy.

- `platform_update`: an openhost update is being checked for or started. Once it's underway the router stops, so this turns into a `503`.
- `os_packages`: Ubuntu's `apt-daily` or `apt-daily-upgrade` is running (unattended security upgrades).
- `tls_certificate`: a certificate is being acquired.
- `apps`: an app is building, starting or being removed.
- `platform_operation`: a domain or archive backend change is in progress.

## `auto_update_time_utc`

The time of day (UTC, `"HH:MM:SS"`) the owner has chosen for openhost to update itself, or `null` with auto-updates off. Use it to schedule the instance's daily wake: the update runs within a minute of the first wake after this time, and the status endpoint reports `platform_update` while it starts. A wake at any other time still catches up, so this is a hint, not a requirement.

## The token

Put it in `config.toml` (rendered from `ansible/templates/config.toml.j2`, variable `auto_suspend_status_token`):

```toml
[openhost]
auto_suspend_status_token = "..."
```

It's read at startup, so the host rotates it by rewriting the file and restarting openhost.

## What the instance does on resume

asyncio's timers stop while the VM is paused, so scheduled work polls the wall clock once a minute instead of sleeping until it's due:

- **Auto-update** runs once after each scheduled time. A scheduled time that passed while the VM was paused (or the router was down) runs within a minute of resuming.
- **TLS renewal** checks every 12 hours of wall-clock time. Certs renew 7 days before expiry, so a daily wake is plenty.
- **Ubuntu's apt timers** are systemd calendar timers, which fire on resume once the clock is corrected.
