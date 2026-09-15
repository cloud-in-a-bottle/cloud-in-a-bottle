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
| `/data/app_data`, permanent app files, including database files | Archive data on both local and S3 backends |
| `/data/app_temp_data`, scratch and build artifacts | The backup app's own permanent-data directory, including its configuration, history and any local repository stored there |
| | Instance state: the router database, TLS certificates, identity keys and JuiceFS metadata |

Standard app mounts do not expose instance state to the backup app. Restoring a snapshot writes back captured app files; it does not reinstall apps or rebuild the instance's configuration. See [what to keep yourself](#what-the-app-cannot-reach) below.

Backups read live files. The app does not stop other apps or create database-consistent dumps, so including database files does not guarantee an application-consistent recovery point.

Archive data is excluded on both backends. The local backend has no off-machine copy from the bundled backup app. S3 stores archive objects outside the instance, but recovering the archive also requires JuiceFS metadata. See [Data](../how_it_works/data.md#the-archive-tier) for the metadata dependency and recovery limitations.

## Restore

Pick a snapshot in the backup app's snapshot browser. Restore writes all captured data roots back in place, overwriting matching files, while excluding the backup app's own permanent-data directory and the archive tier. Browsing into a folder does not restrict what the Restore button restores. Selecting a single data root is available only through the restore API.

The app does not stop affected apps before restoring. Stop them before restoring their files, then reload them from the dashboard afterwards so they use the restored data.

## Moving to another machine

The backup app's migration tab transfers the app list and permanent app data to another instance. Both instances need the backup app installed, and migration needs API tokens for the source and target routers. Source apps must be stopped before the transfer. The target stops apps, clears the permanent-data directories for migrated apps, receives the data, and requests app deployment or reload. Temporary data, archive data and instance configuration are not transferred.

If the source machine is already gone, restoring a snapshot recovers only the captured files. Apps must be reinstalled and instance configuration reconstructed or recovered separately; archive recovery is also separate. The snapshot browser hides snapshots tagged with a different zone/domain, so a replacement instance using a different domain may not show the source instance's snapshots.

## What the app cannot reach

Neither backup nor migration carries the router's own state, so keep a copy of it yourself if a rebuild would hurt:

```bash
bottle instance rsync -a host@<your-domain>:/home/host/.openhost/local_compute_space/persistent_data/openhost/ ./instance-state/
```

That directory holds `router.db` (your apps, domains, API tokens, owner account), TLS certificates, identity keys, and JuiceFS state, including `juicefs/state/meta.db`. Certificates can be reissued, but neither the router database nor JuiceFS metadata is reconstructed by the backup app's restore. The command above copies files; copying databases while they are being written does not guarantee a consistent backup.
