# bottle — Cloud in a Bottle CLI

Command-line tool for managing apps on your Cloud in a Bottle compute space.

## Install

HTTPS:
```bash
uv tool install "bottle @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
```
SSH:
```bash
uv tool install "bottle @ git+ssh://git@github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
```

## Setup

```bash
bottle instance login                    # add an instance interactively
bottle instance set-default x.host.com   # set it as default
```

This will prompt you for your compute space URL and walk you through creating an API token. The instance is saved under its domain name (e.g. `x.host.com`) to `~/.bottle/compute_space_cli.toml`.

For development, use an editable install so changes take effect immediately:

```bash
cd compute_space_cli && uv tool install --editable .
```

## Usage

```bash
bottle status                                    # check if compute space is reachable
bottle version                                   # show git branch/SHA of the running openhost
bottle logs                                      # view zone-level router logs
bottle logs --follow                             # tail router logs

bottle app list                                  # list apps and status
bottle app deploy https://github.com/you/myapp   # deploy from git repo
bottle app deploy https://github.com/you/myapp --name cool-app  # custom name
bottle app deploy https://github.com/you/myapp --wait           # block until running
bottle app deploy https://github.com/you/myapp --grant-permissions-v2  # auto-grant all manifest permissions
bottle app deploy https://github.com/you/myapp --port web=8080  # override a port mapping
bottle app status cool-app                       # check status
bottle app logs cool-app                         # view logs
bottle app logs cool-app --follow                # tail logs
bottle app reload cool-app                       # rebuild + restart
bottle app reload cool-app --update --wait       # git pull, rebuild, wait until running
bottle app ssh cool-app                          # open a shell inside the running container
bottle app ssh cool-app --shell bash             # use bash instead of sh (default: sh)
bottle app ssh cool-app -- ls -la /data          # run a command in the container and exit
bottle app ssh cool-app "cat /etc/os-release"    # ...or pass it as a single quoted string
bottle app stop cool-app                         # stop app
bottle app remove cool-app                       # remove app + data
bottle app remove cool-app --keep-data           # remove but keep data
bottle app rename cool-app new-name              # rename app

bottle tokens list                               # list API tokens
bottle tokens create --name "ci" --expiry-hours 72
bottle tokens delete 3                           # delete by token ID
```

`bottle logs` shows zone-level router logs (deploy errors, routing issues). `bottle app logs` shows a specific app's container output.

`--grant-permissions-v2` automatically grants all `[[services.v2.consumes]]` entries from the manifest at deploy time, skipping the manual approval step in the dashboard. See [cross_app_services.md](../docs/src/cross_app_services.md) for details on the permissions model.

`--port` can be repeated for multiple overrides: `--port web=8080 --port metrics=9090`.

## Multi-instance support

The CLI supports managing multiple named instances.

### Instance management

```bash
bottle instance login                            # interactive login (saves as domain name)
bottle instance list                             # list all instances
bottle instance add user.host.com TOKEN          # add non-interactively
bottle instance alias user.host.com dev          # set a short alias
bottle instance set-default dev                  # set default (by hostname or alias)
bottle instance remove dev                       # remove (by hostname or alias)
bottle instance token                            # print stored token for current instance
```

### Targeting instances

```bash
bottle --instance dev app list                   # target by alias
bottle --instance user.host.com app list         # target by hostname
BOTTLE_INSTANCE=dev bottle app list                  # same, via env var
```

Resolution order: `--instance` flag > `BOTTLE_INSTANCE` env var > default instance.
Names are resolved as hostnames first, then aliases.

## SSH access

```bash
bottle instance configure-ssh-key ~/.ssh/id_ed25519   # register SSH key for this instance
bottle instance ssh                                    # SSH into the zone server as the host user
bottle instance ssh -- -L 5432:localhost:5432          # SSH with extra arguments (port forward etc.)
bottle instance rsync -av ./local/ host@myzone.example.com:/path/  # rsync via the configured key
```

`bottle instance ssh` is a shorthand for `ssh [-i key] host@<hostname>`. Without a key configured via `configure-ssh-key`, it falls back to your SSH agent or default key.

Note: `podman` is installed via pixi, not system-wide. Use the full path above or `cd ~/openhost && pixi run -e dev podman ...` if pixi is on your PATH. App data lives at `~/.openhost/local_compute_space/persistent_data/app_data/`.

## Authenticated HTTP requests

```bash
bottle curl https://myzone.example.com/api/apps          # GET with bearer token injected
bottle curl -X POST https://myzone.example.com/api/...   # any curl args work
```

`bottle curl` runs `curl` with `Authorization: Bearer <token>` pre-injected for the current instance. Useful for hitting the API or testing app endpoints without copying tokens by hand.

## Update

```bash
uv tool upgrade bottle
```
