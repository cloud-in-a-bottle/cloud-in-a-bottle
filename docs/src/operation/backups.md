# Backups and restore

Cloud in a Bottle comes with a backup app. It uses [restic](https://restic.net/) to back up your apps' files to the storage location you choose. Backups are encrypted, incremental and deduplicated.

The app does not back anything up until you have configured it to do so.

## Set it up

Open the backup app and specify:

- **Repository URL**: where backups go, most commonly an S3 bucket. It is recommended to use storage outside the instance for protection from issues that occur on the host.
- **Repository password**: the password used to encrypt your backups.
- **Backend credentials**: whatever your storage provider needs, entered as environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and so on).
- **Interval**: seconds to wait between automatic backups, minimum 60. Leave it at `0` to run backups only by hand. The interval starts after the previous backup finishes, so a backup that takes an hour adds an hour to the gap between runs.
- **Retention**: how many snapshots to keep, using `keep-last`, `keep-hourly`, `keep-daily`, `keep-weekly`, `keep-monthly`, and `keep-yearly`. The rules combine: a snapshot is kept if it matches any enabled rule. All zeros disables automatic deletion.

Test the connection, save the configuration, then run a backup by hand. Check that it succeeds and appears in the snapshot list, as a successful connection test alone does not mean your data has been backed up.

## What is in a backup

| Included | Not included |
|---|---|
| `/data/app_data`, every app's permanent data, including SQLite databases | The archive tier, which is expected to be durable where it lives |
| `/data/app_temp_data`, scratch and build artifacts | The backup app's own directory, so the repository can't back up itself |
| | Router state: the database, TLS certificates, identity keys |

The last row matters. The router's own data lives outside every app's mounts, so no app can see it, including this one. A restored instance gets your apps and their data back; it does not get the instance's own configuration back. See [what to keep yourself](#what-the-app-cannot-reach) below.

Archive data is excluded because on the S3 backend the bytes already live in your bucket. On the default local backend they do not live anywhere else, so an instance using the archive tier locally has no off-machine copy of it at all. If you keep anything you care about in the archive tier, move that zone to S3 (see [Data](../how_it_works/data.md#the-archive-tier)).

## Restore

Restoring happens from the snapshot browser in the same app. Pick a snapshot, restore everything or a single data root, and the files are written back in place, overwriting what is there. The app's own directory and the archive tier are left alone.

Reload the affected apps from the dashboard afterwards. A running container holds its own view of files it has open, and databases in particular will not notice that their files changed underneath them.

## Moving to another machine

The backup app has a migration tab that pushes apps and their data straight to another instance: it sends the app list, the target stops those apps and clears their directories, the data streams across, and the target redeploys. You need an API token for the target instance.

For a machine that is already gone, install a fresh instance, install the backup app, point it at the same repository with the same password, and restore.

## What the app cannot reach

Neither backup nor migration carries the router's own state, so keep a copy of it yourself if a rebuild would hurt:

```bash
bottle instance rsync -a host@<your-domain>:/home/host/.openhost/local_compute_space/persistent_data/openhost/ ./instance-state/
```

That directory holds `router.db` (your apps, domains, API tokens, owner account), the TLS certificates, and the identity keys. Certificates are re-acquired automatically on a new machine, so the database is the part worth having.
