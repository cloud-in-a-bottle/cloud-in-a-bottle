# The bottle CLI

`bottle` does what the dashboard does, without a browser. Nothing needs it; it exists for people who prefer a terminal, for scripting, and for handing an instance to an AI agent.

## Install and log in

```bash
uv tool install "cloud-in-a-bottle-cli @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
```

From a local clone instead, which then tracks whatever you pull:

```bash
cd <clone>/compute_space_cli && uv tool install --editable .
```

Then:

```bash
bottle instance login
```

It asks for your instance URL, sends you to the dashboard to create an API token, and takes the token back. Config lands in `~/.cloud_in_a_bottle_cli/compute_space_cli.toml`.

## Instances

One CLI can hold several instances. Commands use the default unless you say otherwise:

```bash
bottle --instance work app list
```

| Command | Does |
|---|---|
| `bottle instance list` | Configured instances, with aliases and the default marked |
| `bottle instance add <url> <token> [--alias a] [--set-default]` | Add one non-interactively |
| `bottle instance set-default <name>` | Change the default |
| `bottle instance alias <name> <alias>` | Give one a short name |
| `bottle instance remove <name>` | Forget it locally |
| `bottle instance token` | Print the stored API token |

## Apps

| Command | Does |
|---|---|
| `bottle app list` | Every app and its status |
| `bottle app deploy <git-url> [--name n] [--wait]` | Install from a repo |
| `bottle app status <app>` | One app's state |
| `bottle app logs <app> [--follow]` | Build log, then container log |
| `bottle app reload <app> [--update] [--wait]` | Rebuild and restart; `--update` pulls first |
| `bottle app stop <app>` | Stop it |
| `bottle app remove <app> [--keep-data]` | Remove it, and its data unless told otherwise |
| `bottle app rename <app> <new>` | Rename it, and its subdomain with it |
| `bottle app ssh <app> [--shell bash] [cmd]` | A shell inside the container |
| `bottle app diagnostics <app>` | JSON bundle of that app's state |

Deploying from a private GitHub repo needs a one-time browser authorization; the CLI prints the link. After that it is non-interactive.

## The instance itself

| Command | Does |
|---|---|
| `bottle status` | Is it reachable? |
| `bottle version` | Which branch and commit are running |
| `bottle logs [--follow]` | Router logs |
| `bottle diagnostics` | JSON bundle of instance state |
| `bottle tokens list \| create \| delete` | API tokens (`create --name ci --expiry-hours 72`) |
| `bottle curl <args>` | `curl` with your bearer token attached |
| `bottle instance ssh [args]` | SSH to the machine as `host` |
| `bottle instance rsync <args>` | `rsync` over that same SSH |

`bottle instance configure-ssh-key <path>` stores the key the last two use. `bottle --help` is authoritative for anything not listed here.
