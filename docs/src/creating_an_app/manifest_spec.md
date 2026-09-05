# Cloud in a Bottle Manifest Spec

Apps declare how they should be deployed on Cloud in a Bottle by placing a `cloudinabottle.toml` file at the root of their git repository. This file defines the spec of the manifest in detail; for a more general walkthrough of creating an app, see [Creating an App](./overview.md).

## Basic Example

```toml
[app]
name = "my-app"
version = "0.1.0"
description = "A simple web app"

[runtime.container]
image = "Dockerfile"
port = 8080

[resources]
memory_mb = 128
cpu_cores = 0.1

[data]
sqlite = ["main"]
```

## Field Reference

### `[app]` (required)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique app identifier (lowercase, hyphens ok) |
| `version` | string | yes | Version string. Conventionally semver, but only checked for non-emptiness (not validated as semver). |
| `description` | string | no | Short description |
| `authors` | string[] | no | List of author names |

### `[runtime.container]` (required)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `image` | string | yes | - | Path to Dockerfile relative to repo root |
| `port` | integer | yes | - | Port the app responds to HTTP on |
| `command` | string | no | - | Override container CMD |
| `capabilities` | string[] | no | `[]` | **Additional** Linux capabilities to grant inside the container, on top of the Docker-default baseline (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, NET_BIND_SERVICE, SETFCAP, SETGID, SETPCAP, SETUID, SYS_CHROOT, NET_RAW, MKNOD, AUDIT_WRITE) that every container receives automatically. Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_CAPABILITIES`); disallowed entries like `"SYS_ADMIN"` are rejected at parse time. Accepts names with or without the `CAP_` prefix. |
| `devices` | string[] | no | `[]` | Host devices to pass through (e.g., `"/dev/net/tun"`). Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_DEVICE_PATHS`); disallowed paths like `/dev/mem`, `/dev/kvm`, or raw block devices are rejected at parse time. |

### `[[ports]]` (optional, repeatable)

Declares additional port mappings for the container. Each entry binds a container port to a host port (TCP+UDP on 0.0.0.0). Set `host_port = 0` for auto-assignment from the 9000-9999 range.

For normal HTTP/HTTPS routing, this section is not necessary. Traffic will enter the app through through the router proxy - requests to `https://{app_name}.{zone_domain}/` will be routed to the app container at port `runtime.container.port`.

Specifying additional ports in this section is only necessary if the app requires a non-HTTP protocols (e.g. SMTP on `25`). We prefer to write apps that only use HTTP-compatible protocols whenever possible.

Only host ports >= 25 can be bound. Ports `80` and `443` are reserved by the router and therefore can't be bound by apps.

Multiple apps requesting the same host port can't be installed at the same time, therefore prefer auto-assigning (`host_port = 0`) when possible to avoid conflicts.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | - | Unique label for this port mapping (e.g., `"metrics"`) |
| `container_port` | integer | yes | - | Port inside the container |
| `host_port` | integer | no | `0` | Port on the host (0 = auto-assign) |

### `[routing]` (optional)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health_check` | string | no | - | Health check path. Used to determine when the app has finished booting up. |
| `public_paths` | string[] | no | `[]` | Route prefixes accessible without authentication |

### `[[links]]` (optional, repeatable)

A convenience feature to display additional links to the instance owner on the app detail page. By default we just link to your app's root at `{app_name}.{zone_url}`. This feature allows additional links to be displayed, eg to an admin console at `/admin`. The `path` is taken at face value and is not verified in any way.

In general it's better to expose these links from within your app; this is mainly a convenience feature for supporting existing apps where we want to add a Cloud in a Bottle specific admin page that isn't exposed in the upstream app. This feature may be removed in the future.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | - | Display name for the link (e.g., `"admin"`) |
| `path` | string | yes | - | Path on the app's URL (e.g., `"/admin"`) |

### `[resources]` (optional)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `memory_mb` | integer | no | 128 | Max container memory in MB |
| `build_memory_mb` | integer | no | `memory_mb` | Memory limit (MB) for the image build step. Defaults to the app's `memory_mb`; a build that needs more must set this explicitly. |
| `cpu_cores` | float | no | 0.1 | CPU allocation in cores (1.0 = 1 core) |

### `[data]` (optional)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `app_data` | boolean | no | true | Provision a directory for this app on the instance's file system, intended for persistent app state. This data will be included in backups and instance migrations. Exposed to the app via `BOTTLE_APP_DATA_DIR`. |
| `app_temp_data` | boolean | no | false | Provision a directory for this app on the instance's file system, intended for ephemeral app data. This data will persist between container or instance boots, but will not be included in backups / instance migrations. Exposed to the app via `BOTTLE_APP_TEMP_DIR`. |
| `app_archive` | boolean | no | false | Provision a directory for this app in the instance's "archive storage", intended for persistent but bulky content. Archive storage is backed by local disk by default, but can be configured by the owner to be served by remote S3 backend to enable larger and more reliable storage than the instance's local disk. Exposed to the app via `BOTTLE_APP_ARCHIVE_DIR`. |
| `sqlite` | string[] | no | [] | SQLite databases to provision. Each entry provisions `app_data/sqlite/{name}.db`, exposed to the app as `BOTTLE_SQLITE_<NAME>`. Enabling implicitly enables `app_data`. |
| `access_all_app_data` | boolean | no | false | Mount the parent dirs for all apps' permanent, temporary, and archive data (rw). For admin, file browser, and backup apps. Mounted under `/data` in the container. |

The retired `access_all_data` and `access_all_archive` fields are deprecated aliases for `access_all_app_data`. Manifests using either receive the full permanent, temporary, and archive data permission and emit a deprecation warning.

## More Examples

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
path = "/admin"
```
