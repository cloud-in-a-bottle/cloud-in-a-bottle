# Backups and restore

Every instance comes with a backup app, installed at setup and reachable at `https://backup.<your-domain>/`. It is [restic](https://restic.net/) underneath, so backups are encrypted, incremental and deduplicated, and they go to storage you choose.

Nothing is backed up until you configure it.

## Set it up

Open the backup app and fill in:

- **Repository URL**: where snapshots go. Restic speaks S3, Backblaze B2, Google Cloud Storage, Azure Blob, Swift, SFTP, rclone remotes, a REST server, or a local path.
- **Repository password**: the encryption key. Snapshots are useless without it, and nobody can recover it for you. Store it somewhere separate from the instance.
- **Backend credentials**: whatever your storage needs, as environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and so on).
- **Interval**: seconds between automatic backups, minimum 60. Leave it at 0 and nothing runs on its own.
- **Retention**: `keep-last`, `keep-hourly`, `keep-daily`, `keep-weekly`, `keep-monthly`, `keep-yearly`. The rules add together, so `keep-last=5, keep-daily=7` keeps the five newest snapshots plus one per day for a week. All zeros means nothing is ever deleted.

Test the connection, then run a backup by hand to confirm it works. The scheduler survives restarts and does not restart its countdown.

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
