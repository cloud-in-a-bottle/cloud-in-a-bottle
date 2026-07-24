# OpenHost Manifest Spec (v0.1)

Apps declare how they should be deployed on OpenHost by placing an `openhost.toml` file at the root of their repository. For a walkthrough of building an app from scratch, see [Creating an App](creating_an_app.md).

## Field Reference

### `[app]` — required

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique app identifier (lowercase, hyphens ok) |
| `version` | string | yes | Version string. Conventionally semver, but only checked for non-emptiness (not validated as semver). |
| `description` | string | no | Short description |
| `authors` | string[] | no | List of author names |

### `[runtime.container]` — required

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `image` | string | yes | — | Path to Dockerfile relative to repo root |
| `port` | integer | yes | — | Port the container listens on |
| `command` | string | no | — | Override container CMD |
| `extra_ports` | string[] | no | `[]` | **Deprecated.** Present entries emit a WARNING log at parse time but do not produce any port mappings.  Use `[[ports]]` instead. |
| `capabilities` | string[] | no | `[]` | **Additional** Linux capabilities to grant inside the container, on top of the Docker-default baseline (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, NET_BIND_SERVICE, SETFCAP, SETGID, SETPCAP, SETUID, SYS_CHROOT, NET_RAW, MKNOD, AUDIT_WRITE) that every container receives automatically. Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_CAPABILITIES`); entries like `"SYS_ADMIN"` are rejected at parse time. Accepts names with or without the `CAP_` prefix. |
| `devices` | string[] | no | `[]` | Host devices to pass through (e.g., `"/dev/net/tun"`). Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_DEVICE_PATHS`); paths like `/dev/mem`, `/dev/kvm`, or raw block devices are rejected at parse time. |

### `[[ports]]` — optional, repeatable

Declares additional port mappings for the container. Each entry binds a container port to a host port (TCP+UDP on 0.0.0.0). Set `host_port = 0` for auto-assignment from the 9000-9999 range.

Rootless podman can bind ports >= 25 only; `host_port` values below 25 are rejected at parse time. Ports `80` and `443` are not rejected by the manifest parser, but they are claimed at runtime by the built-in Caddy front-door, so an app that requests them will fail to bind. For public HTTP/HTTPS, route through the router proxy (apps live under `https://{app_name}.{zone_domain}/`); for other protocols (e.g. SMTP on `25`), pick `host_port = 25` or any port in the 9000-9999 auto-assign range.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | — | Unique label for this port mapping (e.g., `"metrics"`) |
| `container_port` | integer | yes | — | Port inside the container |
| `host_port` | integer | no | `0` | Port on the host (0 = auto-assign) |

### `[routing]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health_check` | string | no | — | Health check path |
| `public_paths` | string[] | no | `[]` | Route prefixes accessible without authentication |

### `[[links]]` — optional, repeatable

User-facing links the app advertises for paths on its own URL that aren't the bare root — for example an admin console at `/_openhost/admin`. The dashboard displays these on the app's detail page. The `path` is taken at face value: OpenHost does not check that it exists, is reachable, or is (or isn't) behind auth — it just shows it to the user.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Display name for the link (e.g., `"admin"`) |
| `path` | string | yes | — | Path on the app's URL (e.g., `"/_openhost/admin"`) |

### `[resources]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `memory_mb` | integer | no | 128 | Max memory in MB |
| `cpu_cores` | float | no | 0.1 | CPU allocation in cores (1.0 = 1 core) |
| `gpu` | boolean | no | false | Whether GPU access is needed |

### `[data]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `app_data` | boolean | no | **true** | Request access to permanent filesystem directory (backed up). Set `false` to explicitly opt out. |
| `app_temp_data` | boolean | no | false | Request access to temporary filesystem directory (not backed up) |
| `app_archive` | boolean | no | false | Request access to the archive directory for bulk content. Always available: backed by local disk by default, upgradable to S3 (via JuiceFS) by the operator. Apps with this flag install on any zone; on a local-backed zone the operator sees an install-time notice that the data is on non-durable local disk. |
| `sqlite` | string[] | no | [] | SQLite database names to provision (implicitly enables `app_data`) |
| `access_vm_data` | boolean | no | false | Whether the app can access the VM's shared data directory (read-only) |
| `access_all_app_data` | boolean | no | false | Mount all apps' permanent data and temp data parent directories (rw). Also grants rw access to vm_data. For admin tools like file browsers. |
| `access_all_archive` | boolean | no | false | Mount all apps' archive parent directory. Permissive: silently skipped if the archive mount is transiently unavailable. For backup tools. |
| `access_all_data` | boolean | no | false | Convenience shorthand for `access_all_app_data = true` + `access_all_archive = true`. |


## Data Directory Structure

Apps have three storage areas, each with different durability + size + latency tradeoffs. **By default, apps receive a permanent data directory (`app_data`).** Other tiers must be explicitly requested:

- **Permanent data** (`/data/app_data/{app_name}/`) — local disk. Small, fast, backed up. Enabled by `app_data = true` or by requesting `sqlite` entries. **SQLite databases must live here**, not in `app_archive`: the archive tier may be backed by a network FS with close-to-open consistency that corrupts SQLite WAL.
- **Temporary data** (`/data/app_temp_data/{app_name}/`) — local disk scratch. Not backed up, recreatable. Enabled by `app_temp_data = true`.
- **Archive data** (`/data/app_archive/{app_name}/`) — bulk content storage. Always available. Backed by **local disk** by default (kept under `persistent_data_dir`, survives rebuilds, included in backups). The operator can upgrade the zone to **S3** (JuiceFS) from the dashboard for elastic, durable object storage; existing local archive data is migrated into the bucket. Higher-latency on uncached reads once on S3, durability then tied to the provider's SLA. Intended for bulk content (videos, photos, attachments). Enabled by `app_archive = true`.
- **VM data** (`/data/vm_data/`) — router database and VM-level shared data. Only accessible if `access_vm_data = true`.

The archive tier defaults to local-disk backing and is always available. The operator can upgrade a zone to S3 one-shot from the dashboard, supplying S3 credentials and a per-zone prefix; archive bytes then route through a JuiceFS mount of the operator-supplied bucket (and any existing local archive data is migrated into it). Apps see `/data/app_archive/<app>/` as a normal POSIX directory and don't need to know which backing is in use.

The storage guard requires a minimum amount of free disk space, stopping running apps when free space drops below `storage_min_free_mb` until space is freed. It is enabled by default; the host operator can change the threshold (or disable it with `0`) in the OpenHost config and reboot.

All data dirs live under `/data/` in the container. All apps see the same path structure regardless of permissions — only the dirs they have access to are mounted. With `access_all_app_data`, the parent dirs `/data/app_data/` and `/data/app_temp_data/` are mounted so the app can see all apps' data. With `access_all_archive`, the `/data/app_archive/` parent is mounted.

## Environment Variable Injection

The host provisions requested data services and injects connection info as environment variables:

- `OPENHOST_SQLITE_<NAME>` — filesystem path to the named sqlite database (only if `sqlite` entries requested)
- `OPENHOST_APP_DATA_DIR` — `/data/app_data/{app_name}` (only if app_data access granted)
- `OPENHOST_APP_TEMP_DIR` — `/data/app_temp_data/{app_name}` (only if app_temp_data access granted)
- `OPENHOST_APP_ARCHIVE_DIR` — `/data/app_archive/{app_name}` (only if app_archive access granted)
- `OPENHOST_ROUTER_URL` — URL of the router's HTTP server, reachable from inside the container.
- `OPENHOST_OWNER_USERNAME` — the compute space owner's chosen display name; use to seed SSO account names. Defaults to `owner` if not explicitly configured.

The host also injects identity/routing variables (`OPENHOST_APP_NAME`, `OPENHOST_APP_ID`, `OPENHOST_APP_TOKEN`, `OPENHOST_ZONE_DOMAIN`, `OPENHOST_MY_REDIRECT_DOMAIN`); see the full table in [Creating an App](creating_an_app.md). The zone's identity public key is **not** injected as an environment variable — it is served over HTTP at `/.well-known/jwks.json` (JWKS) and `/.well-known/openhost-identity` (PEM).

## Examples

### Basic app

```toml
[app]
name = "my-app"
version = "0.1.0"
description = "A simple web app"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
health_check = "/health"

[resources]
memory_mb = 128
cpu_cores = 0.1

[data]
sqlite = ["main"]
```

### App with extra container permissions

```toml
[app]
name = "ha-tunnel"
version = "0.2.0"
description = "WebSocket tunnel to Home Assistant"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
public_paths = ["/tunnel"]

[resources]
memory_mb = 128
cpu_cores = 0.1
```

### App with extra port mappings

```toml
[app]
name = "monitoring"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[ports]]
label = "metrics"
container_port = 9090
host_port = 9090

[[ports]]
label = "debug"
container_port = 5005
host_port = 0  # auto-assigned
```

### Minimal app (wrapping existing software)

```toml
[app]
name = "file-browser"
version = "0.1.0"
description = "Web-based file browser"

[runtime.container]
image = "Dockerfile"
port = 5000
command = "/data -A"

[data]
access_all_app_data = true
access_all_archive = true
```

### App advertising user-facing links

```toml
[app]
name = "synapse"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 3000

[[links]]
name = "admin"
path = "/_openhost/admin"
```
