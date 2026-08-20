# cb — Cloud in a Bottle CLI

Command-line tool for managing apps on your Cloud in a Bottle compute space.

## Install

HTTPS:
```bash
uv tool install "cb @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
```
SSH:
```bash
uv tool install "cb @ git+ssh://git@github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
```

## Setup

```bash
cb instance login                    # add an instance interactively
cb instance set-default x.host.com   # set it as default
```

This will prompt you for your compute space URL and walk you through creating an API token. The instance is saved under its domain name (e.g. `x.host.com`) to `~/.cb/compute_space_cli.toml`.

For development, use an editable install so changes take effect immediately:

```bash
cd compute_space_cli && uv tool install --editable .
```

## Usage

```bash
cb status                                    # check if compute space is reachable
cb version                                   # show git branch/SHA of the running openhost
cb logs                                      # view zone-level router logs
cb logs --follow                             # tail router logs

cb app list                                  # list apps and status
cb app deploy https://github.com/you/myapp   # deploy from git repo
cb app deploy https://github.com/you/myapp --name cool-app  # custom name
cb app deploy https://github.com/you/myapp --wait           # block until running
cb app deploy https://github.com/you/myapp --grant-permissions-v2  # auto-grant all manifest permissions
cb app deploy https://github.com/you/myapp --port web=8080  # override a port mapping
cb app status cool-app                       # check status
cb app logs cool-app                         # view logs
cb app logs cool-app --follow                # tail logs
cb app reload cool-app                       # rebuild + restart
cb app reload cool-app --update --wait       # git pull, rebuild, wait until running
cb app ssh cool-app                          # open a shell inside the running container
cb app ssh cool-app --shell bash             # use bash instead of sh (default: sh)
cb app ssh cool-app -- ls -la /data          # run a command in the container and exit
cb app ssh cool-app "cat /etc/os-release"    # ...or pass it as a single quoted string
cb app stop cool-app                         # stop app
cb app remove cool-app                       # remove app + data
cb app remove cool-app --keep-data           # remove but keep data
cb app rename cool-app new-name              # rename app

cb tokens list                               # list API tokens
cb tokens create --name "ci" --expiry-hours 72
cb tokens delete 3                           # delete by token ID
```

`cb logs` shows zone-level router logs (deploy errors, routing issues). `cb app logs` shows a specific app's container output.

`--grant-permissions-v2` automatically grants all `[[services.v2.consumes]]` entries from the manifest at deploy time, skipping the manual approval step in the dashboard. See [cross_app_services.md](../docs/src/cross_app_services.md) for details on the permissions model.

`--port` can be repeated for multiple overrides: `--port web=8080 --port metrics=9090`.

## Multi-instance support

The CLI supports managing multiple named instances.

### Instance management

```bash
cb instance login                            # interactive login (saves as domain name)
cb instance list                             # list all instances
cb instance add user.host.com TOKEN          # add non-interactively
cb instance alias user.host.com dev          # set a short alias
cb instance set-default dev                  # set default (by hostname or alias)
cb instance remove dev                       # remove (by hostname or alias)
cb instance token                            # print stored token for current instance
```

### Targeting instances

```bash
cb --instance dev app list                   # target by alias
cb --instance user.host.com app list         # target by hostname
CB_INSTANCE=dev cb app list                  # same, via env var
```

Resolution order: `--instance` flag > `CB_INSTANCE` env var > default instance.
Names are resolved as hostnames first, then aliases.

## SSH access

```bash
cb instance configure-ssh-key ~/.ssh/id_ed25519   # register SSH key for this instance
cb instance ssh                                    # SSH into the zone server as the host user
cb instance ssh -- -L 5432:localhost:5432          # SSH with extra arguments (port forward etc.)
cb instance rsync -av ./local/ host@myzone.example.com:/path/  # rsync via the configured key
```

`cb instance ssh` is a shorthand for `ssh [-i key] host@<hostname>`. Without a key configured via `configure-ssh-key`, it falls back to your SSH agent or default key.

Note: `podman` is installed via pixi, not system-wide. Use the full path above or `cd ~/openhost && pixi run -e dev podman ...` if pixi is on your PATH. App data lives at `~/.openhost/local_compute_space/persistent_data/app_data/`.

## Authenticated HTTP requests

```bash
cb curl https://myzone.example.com/api/apps          # GET with bearer token injected
cb curl -X POST https://myzone.example.com/api/...   # any curl args work
```

`cb curl` runs `curl` with `Authorization: Bearer <token>` pre-injected for the current instance. Useful for hitting the API or testing app endpoints without copying tokens by hand.

## Update

```bash
uv tool upgrade cb
```
