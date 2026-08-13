# oh — Cloud in a Bottle CLI

Command-line tool for managing apps on your Cloud in a Bottle compute space.

## Install

HTTPS:
```bash
uv tool install "oh @ git+https://github.com/imbue-openhost/openhost.git#subdirectory=compute_space_cli"
```
SSH:
```bash
uv tool install "oh @ git+ssh://git@github.com/imbue-openhost/openhost.git#subdirectory=compute_space_cli"
```

## Setup

```bash
oh instance login                    # add an instance interactively
oh instance set-default x.host.com   # set it as default
```

This will prompt you for your compute space URL and walk you through creating an API token. The instance is saved under its domain name (e.g. `x.host.com`) to `~/.openhost/compute_space_cli.toml`.

For development, use an editable install so changes take effect immediately:

```bash
cd compute_space_cli && uv tool install --editable .
```

## Usage

```bash
oh status                                    # check if compute space is reachable
oh version                                   # show git branch/SHA of the running openhost
oh logs                                      # view zone-level router logs
oh logs --follow                             # tail router logs

oh app list                                  # list apps and status
oh app deploy https://github.com/you/myapp   # deploy from git repo
oh app deploy https://github.com/you/myapp --name cool-app  # custom name
oh app deploy https://github.com/you/myapp --wait           # block until running
oh app deploy https://github.com/you/myapp --grant-permissions-v2  # auto-grant all manifest permissions
oh app deploy https://github.com/you/myapp --port web=8080  # override a port mapping
oh app status cool-app                       # check status
oh app logs cool-app                         # view logs
oh app logs cool-app --follow                # tail logs
oh app reload cool-app                       # rebuild + restart
oh app reload cool-app --update --wait       # git pull, rebuild, wait until running
oh app ssh cool-app                          # open a shell inside the running container
oh app ssh cool-app --shell bash             # use bash instead of sh (default: sh)
oh app ssh cool-app -- ls -la /data          # run a command in the container and exit
oh app ssh cool-app "cat /etc/os-release"    # ...or pass it as a single quoted string
oh app stop cool-app                         # stop app
oh app remove cool-app                       # remove app + data
oh app remove cool-app --keep-data           # remove but keep data
oh app rename cool-app new-name              # rename app

oh tokens list                               # list API tokens
oh tokens create --name "ci" --expiry-hours 72
oh tokens delete 3                           # delete by token ID
```

`oh logs` shows zone-level router logs (deploy errors, routing issues). `oh app logs` shows a specific app's container output.

`--grant-permissions-v2` automatically grants all `[[services.v2.consumes]]` entries from the manifest at deploy time, skipping the manual approval step in the dashboard. See [cross_app_services.md](../docs/src/cross_app_services.md) for details on the permissions model.

`--port` can be repeated for multiple overrides: `--port web=8080 --port metrics=9090`.

## Multi-instance support

The CLI supports managing multiple named instances.

### Instance management

```bash
oh instance login                            # interactive login (saves as domain name)
oh instance list                             # list all instances
oh instance add user.host.com TOKEN          # add non-interactively
oh instance alias user.host.com dev          # set a short alias
oh instance set-default dev                  # set default (by hostname or alias)
oh instance remove dev                       # remove (by hostname or alias)
oh instance token                            # print stored token for current instance
```

### Targeting instances

```bash
oh --instance dev app list                   # target by alias
oh --instance user.host.com app list         # target by hostname
OH_INSTANCE=dev oh app list                  # same, via env var
```

Resolution order: `--instance` flag > `OH_INSTANCE` env var > default instance.
Names are resolved as hostnames first, then aliases.

## SSH access

```bash
oh instance configure-ssh-key ~/.ssh/id_ed25519   # register SSH key for this instance
oh instance ssh                                    # SSH into the zone server as the host user
oh instance ssh -- -L 5432:localhost:5432          # SSH with extra arguments (port forward etc.)
oh instance rsync -av ./local/ host@myzone.example.com:/path/  # rsync via the configured key
```

`oh instance ssh` is a shorthand for `ssh [-i key] host@<hostname>`. Without a key configured via `configure-ssh-key`, it falls back to your SSH agent or default key.

Note: `podman` is installed via pixi, not system-wide. Use the full path above or `cd ~/openhost && pixi run -e dev podman ...` if pixi is on your PATH. App data lives at `~/.openhost/local_compute_space/persistent_data/app_data/`.

## Authenticated HTTP requests

```bash
oh curl https://myzone.example.com/api/apps          # GET with bearer token injected
oh curl -X POST https://myzone.example.com/api/...   # any curl args work
```

`oh curl` runs `curl` with `Authorization: Bearer <token>` pre-injected for the current instance. Useful for hitting the API or testing app endpoints without copying tokens by hand.

## Update

```bash
uv tool upgrade oh
```
