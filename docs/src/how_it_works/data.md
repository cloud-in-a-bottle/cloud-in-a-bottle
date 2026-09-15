# Data

Each app gets its own directories, mounted into its container under `/data/`. Apps see the same path layout no matter what they have access to (only the directories they were granted are actually mounted), so the structure never changes when permissions do.

## The three tiers

| Tier | In the container | Backing | In bundled backups | For |
|---|---|---|---|---|
| Permanent | `/data/app_data/<app>` | Local disk | Included | SQLite databases, notes, config, small assets |
| Temporary | `/data/app_temp_data/<app>` | Local disk | Included | Thumbnails, transcodes, build artifacts, anything recreatable |
| Archive | `/data/app_archive/<app>` | JuiceFS, local or S3 | Excluded | Bulk content: photos, video, attachments, model weights |

The owner must configure the [backup app](../operation/backups.md) before it creates snapshots. It excludes its own permanent-data directory. Temporary data should still be recreatable; direct app migration does not copy it.

Apps get permanent data by default and request the other two in their manifest (`app_temp_data`, `app_archive`). They should read the paths from `BOTTLE_APP_DATA_DIR`, `BOTTLE_APP_TEMP_DIR` and `BOTTLE_APP_ARCHIVE_DIR` rather than hardcoding them. See [Creating an App](../creating_an_app/overview.md#data-storage) for the app author's view.

The split between permanent and archive matters more than it looks. Permanent data is local disk with real `fsync` and strict POSIX semantics, which is what an embedded database needs: SQLite, LMDB, RocksDB and friends belong there and nowhere else. The archive tier is a network-shaped filesystem: fine for whole files, wrong for a write-ahead log or for `fcntl` locks used for correctness. An app that stores bulk content normally uses both, keeping its index in permanent data and the bytes in the archive.

An app can also request `access_all_app_data`, which mounts every app's directories read-write. This is for file browsers, backup tools and the like.

## The archive tier

The archive is always a JuiceFS volume, so an app that asks for it installs anywhere. Only the object storage underneath differs:

- **Local (default)**: objects live on the instance's own disk. Nothing to configure, but there is no copy anywhere else, and the bundled backup app skips the archive tier.
- **S3**: objects live in a bucket you supply, configured in the dashboard. This stores them outside the instance, at the cost of tens to hundreds of milliseconds on an uncached first read. The bundled backup app still skips the archive tier.

JuiceFS keeps the mappings from filenames to objects in a local SQLite metadata database. It also writes metadata dumps to its object store. With S3, the dashboard's archive settings show the latest available dump. If the local metadata is lost, recovery requires a usable metadata dump and a manual `juicefs load`; restoring a snapshot in the backup app does not perform this step. S3 objects alone are not enough to reconstruct the archive filesystem.

Switching from local to S3, or from one bucket to another, is done from the dashboard behind a confirmation. The objects are copied and verified, then the same volume is re-pointed at the new store; the metadata database is untouched, so every file, permission and owner is preserved. It fails open: if anything goes wrong before the switch commits, the volume keeps reading from the store it was already using.

## Where it lives on disk

Everything sits under the instance's data directory (`data_root_dir` in `config.toml`, normally `~/.openhost/local_compute_space/`):

| Path | Contents |
|---|---|
| `persistent_data/app_data/<app>/` | Permanent app data |
| `persistent_data/app_archive_local_objects/` | JuiceFS objects, on the local backend only |
| `persistent_data/openhost/` | Router database, TLS certificates, keys and JuiceFS state |
| `persistent_data/openhost/juicefs/state/meta.db` | JuiceFS filename-to-object mappings |
| `temporary_data/app_temp_data/<app>/` | Temporary app data, plus that app's build and container logs |
| `app_archive/` | The JuiceFS mount |

The configured [bundled backup app](../operation/backups.md) copies `persistent_data/app_data/` and `temporary_data/app_temp_data/`, excluding its own permanent-data directory. It does not copy `persistent_data/openhost/` or the raw local archive objects: standard app mounts do not expose those directories. Reaching instance state means SSH or the terminal in the dashboard.

## Storage guard

Running a disk to zero on a machine that hosts your own data is worse than stopping early, so the instance reserves headroom. When free space drops below `storage_min_free_mb` (500 MB by default) the storage guard stops running apps until space is freed. Change the threshold in `config.toml` (or set it to `0` to switch the guard off), then restart.
