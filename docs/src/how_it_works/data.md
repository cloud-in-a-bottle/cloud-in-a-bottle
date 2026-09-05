# Data

Each app gets its own directories, mounted into its container under `/data/`. Apps see the same path layout no matter what they have access to (only the directories they were granted are actually mounted), so the structure never changes when permissions do.

## The three tiers

| Tier | In the container | Backing | Backed up | For |
|---|---|---|---|---|
| Permanent | `/data/app_data/<app>` | Local disk | Yes | SQLite databases, notes, config, small assets |
| Temporary | `/data/app_temp_data/<app>` | Local disk | Not guaranteed | Thumbnails, transcodes, build artifacts, anything recreatable |
| Archive | `/data/app_archive/<app>` | JuiceFS, local or S3 | See below | Bulk content: photos, video, attachments, model weights |

Apps get permanent data by default and request the other two in their manifest (`app_temp_data`, `app_archive`). They should read the paths from `BOTTLE_APP_DATA_DIR`, `BOTTLE_APP_TEMP_DIR` and `BOTTLE_APP_ARCHIVE_DIR` rather than hardcoding them. See [Creating an App](../creating_an_app/overview.md#data-storage) for the app author's view.

The split between permanent and archive matters more than it looks. Permanent data is local disk with real `fsync` and strict POSIX semantics, which is what an embedded database needs: SQLite, LMDB, RocksDB and friends belong there and nowhere else. The archive tier is a network-shaped filesystem: fine for whole files, wrong for a write-ahead log or for `fcntl` locks used for correctness. An app that stores bulk content normally uses both, keeping its index in permanent data and the bytes in the archive.

An app can also request `access_all_app_data`, which mounts every app's directories read-write. This is for file browsers, backup tools and the like.

## The archive tier

The archive is always a JuiceFS volume, so an app that asks for it installs anywhere. Only the object storage underneath differs:

- **Local (default)**: objects live on the instance's own disk. Nothing to configure, but there is no copy anywhere else, and the bundled backup app skips the archive tier.
- **S3**: objects live in a bucket you supply, configured in the dashboard. Elastic and durable, at the cost of tens to hundreds of milliseconds on an uncached first read. Backups skip it, since the bytes already live in the bucket.

Switching from local to S3, or from one bucket to another, is done from the dashboard behind a confirmation. The objects are copied and verified, then the same volume is re-pointed at the new store; the metadata database is untouched, so every file, permission and owner is preserved. It fails open: if anything goes wrong before the switch commits, the volume keeps reading from the store it was already using.

## Where it lives on disk

Everything sits under the instance's data directory (`data_root_dir` in `config.toml`, normally `~/.openhost/local_compute_space/`):

| Path | Contents |
|---|---|
| `persistent_data/app_data/<app>/` | Permanent app data |
| `persistent_data/app_archive_local_objects/` | JuiceFS objects, on the local backend only |
| `persistent_data/openhost/` | Router database, TLS certificates and keys |
| `temporary_data/app_temp_data/<app>/` | Temporary app data, plus that app's build and container logs |
| `app_archive/` | The JuiceFS mount |

The [bundled backup app](../operation/backups.md) copies the app data under `persistent_data/`, which is the point of the split. It does not copy `persistent_data/openhost/`: router state is never mounted into any container, so no app can see it, and reaching it means SSH or the terminal in the dashboard.

## Storage guard

Running a disk to zero on a machine that hosts your own data is worse than stopping early, so the instance reserves headroom. When free space drops below `storage_min_free_mb` (500 MB by default) the storage guard stops running apps until space is freed. Change the threshold in `config.toml` (or set it to `0` to switch the guard off), then restart.
