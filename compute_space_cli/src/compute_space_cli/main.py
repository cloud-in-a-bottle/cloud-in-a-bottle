from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import attrs
import cappa
import httpx
from cappa import Dep

from compute_space_cli import config
from compute_space_cli.helpers import make_api_request
from compute_space_cli.helpers import resolve_app_id_by_name
from compute_space_cli.helpers import wait_for_app_removed
from compute_space_cli.helpers import wait_for_app_running


def _load_or_exit() -> config.MultiConfig:
    """Load MultiConfig or print a friendly message and exit."""
    try:
        return config.get_multi_config()
    except config.ConfigFileNotFoundError:
        print("No config file. Run 'oh instance login' first.", file=sys.stderr)
        raise SystemExit(1) from None
    except config.ConfigInvalidError as e:
        print(f"Invalid config file: {e}", file=sys.stderr)
        raise SystemExit(1) from None


def _load_or_create() -> config.MultiConfig:
    """Load MultiConfig or return an empty one if no valid config exists."""
    try:
        return config.get_multi_config()
    except config.ConfigFileNotFoundError:
        return config.MultiConfig()
    except config.ConfigInvalidError as e:
        print(f"Warning: existing config is invalid and will be replaced: {e}", file=sys.stderr)
        return config.MultiConfig()


def resolve_instance(oh: Oh) -> config.Instance:
    """Resolve which instance to target, respecting --instance."""
    multi = _load_or_exit()
    try:
        return multi.resolve(instance_name=oh.instance)
    except config.InstanceNotFoundError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None


@cappa.command(name="status", help="Check if your compute space is reachable.")
@attrs.define
class Status:
    def __call__(self, cfg: Annotated[config.Instance, Dep(resolve_instance)]) -> None:
        try:
            resp = httpx.get(cfg.url, headers={"Authorization": f"Bearer {cfg.token}"}, timeout=10)
            print(f"{cfg.url} — up (HTTP {resp.status_code})")
        except httpx.ConnectError:
            print(f"{cfg.url} — unreachable", file=sys.stderr)
            raise SystemExit(1) from None
        except httpx.TimeoutException:
            print(f"{cfg.url} — timed out", file=sys.stderr)
            raise SystemExit(1) from None


@cappa.command(name="app", help="Manage apps.")
@attrs.define
class AppCmd:
    @cappa.command(name="deploy")
    def deploy(
        self,
        repo_url: Annotated[str, cappa.Arg(help="Git repository URL")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        name: Annotated[str | None, cappa.Arg(long=True, help="App name (default: from manifest)")] = None,
        wait: Annotated[bool, cappa.Arg(long=True, help="Wait for deploy to finish")] = False,
        port: Annotated[
            list[str] | None, cappa.Arg(long=True, help="Port override: label=host_port (repeatable)")
        ] = None,
        grant_permissions_v2: Annotated[
            bool,
            cappa.Arg(
                long="--grant-permissions-v2",
                help="Grant all [[services.v2.consumes]] entries declared in the manifest",
            ),
        ] = False,
    ) -> None:
        """Deploy an app from a git repo."""
        data: dict[str, str] = {"repo_url": repo_url}
        if name:
            data["app_name"] = name
        if port:
            for p in port:
                label, _, val = p.partition("=")
                data[f"port_override.{label}"] = val
        if grant_permissions_v2:
            data["grant_permissions_v2"] = "true"
        result = make_api_request(cfg.url, cfg.token, "POST", "/api/add_app", data=data, raw=True)
        try:
            body = result.json()
        except Exception:
            print(f"Error ({result.status_code}): {result.text[:500]}", file=sys.stderr)
            raise SystemExit(1) from None
        if result.status_code == 401 and body.get("authorize_url"):
            auth_url = body["authorize_url"]
            if auth_url.startswith("//"):
                proto = cfg.url.split("://")[0]
                auth_url = f"{proto}:{auth_url}"
            print("This repo requires GitHub authorization.", file=sys.stderr)
            print("Open this link to connect your GitHub account:", file=sys.stderr)
            print(f"  {auth_url}", file=sys.stderr)
            print("\nThen re-run this command.", file=sys.stderr)
            raise SystemExit(1)
        if result.status_code >= 400:
            print(
                f"Error ({result.status_code}): {body.get('error', result.text)}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        app_id = body.get("app_id")
        app_name = body.get("app_name")
        print(f"Deploying {app_name}...")
        print(f"  {cfg.url}/app_detail/{app_name}")
        if not wait:
            print(f"Status: {body.get('status', 'submitted')}")
            return
        wait_for_app_running(cfg.url, cfg.token, app_id, app_name)

    @cappa.command(name="status")
    def status(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Get app status."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        result = make_api_request(cfg.url, cfg.token, "GET", f"/api/app_status/{app_id}").json()
        print(f"{app_name}: {result.get('status', 'unknown')}")
        if result.get("error"):
            print(f"  error: {result['error']}")
        sha = result.get("git_sha")
        if sha:
            branch = result.get("git_branch") or "(detached HEAD)"
            dirty = " (dirty)" if result.get("git_dirty") else ""
            print(f"  git: {branch} @ {sha}{dirty}")

    @cappa.command(name="logs")
    def logs(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        follow: Annotated[bool, cappa.Arg(long=True, help="Poll for new logs")] = False,
        interval: Annotated[int, cappa.Arg(long=True, help="Poll interval in seconds")] = 5,
    ) -> None:
        """View app logs."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        if not follow:
            print(make_api_request(cfg.url, cfg.token, "GET", f"/app_logs/{app_id}").text)
            return
        seen = ""
        while True:
            text = make_api_request(cfg.url, cfg.token, "GET", f"/app_logs/{app_id}").text
            if text != seen:
                print(text[len(seen) :], end="", flush=True)
                seen = text
            time.sleep(interval)

    @cappa.command(name="reload")
    def reload(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        update: Annotated[bool, cappa.Arg(long=True, help="Git pull before rebuilding")] = False,
        wait: Annotated[bool, cappa.Arg(long=True, help="Wait for reload to finish")] = False,
    ) -> None:
        """Reload (rebuild + restart) an app."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        action = "Updating and reloading" if update else "Reloading"
        print(f"{action} {app_name}...")
        # An explicit `--update` is itself the owner's approval: send
        # approve_new_permissions so the dashboard's change-review gate doesn't
        # silently refuse the update (the browser reviews changes interactively).
        data = {"update": "1", "approve_new_permissions": "1"} if update else None
        make_api_request(cfg.url, cfg.token, "POST", f"/reload_app/{app_id}", data=data)
        if wait:
            wait_for_app_running(cfg.url, cfg.token, app_id, app_name)
        else:
            print("OK")

    @cappa.command(name="stop")
    def stop(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Stop a running app."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        make_api_request(cfg.url, cfg.token, "POST", f"/stop_app/{app_id}")
        print(f"Stopped {app_name}")

    @cappa.command(name="remove")
    def remove(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        keep_data: Annotated[bool, cappa.Arg(long=True, help="Keep app data on disk")] = False,
    ) -> None:
        """Remove an app."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        data = {"keep_data": "1"} if keep_data else None
        # /remove_app returns 202; poll until the row is actually gone.
        make_api_request(cfg.url, cfg.token, "POST", f"/remove_app/{app_id}", data=data)
        suffix = " (data kept)" if keep_data else ""
        print(f"Removing {app_name}{suffix}...")
        wait_for_app_removed(cfg.url, cfg.token, app_id, app_name)
        print(f"Removed {app_name}{suffix}")

    @cappa.command(name="rename")
    def rename(
        self,
        app_name: Annotated[str, cappa.Arg(help="Current app name")],
        new_name: Annotated[str, cappa.Arg(help="New app name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Rename an app."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        result = make_api_request(
            cfg.url,
            cfg.token,
            "POST",
            f"/rename_app/{app_id}",
            data={"name": new_name},
        ).json()
        print(f"Renamed {app_name} → {result.get('name', new_name)}")

    @cappa.command(name="ssh")
    def ssh(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        shell: Annotated[str, cappa.Arg(long=True, help="Shell to invoke inside container")] = "sh",
        args: Annotated[
            list[str] | None,
            cappa.Arg(num_args=-1, help="Command to run inside the container (default: interactive shell)"),
        ] = None,
    ) -> None:
        """Run a command inside an app's container, or open a shell if no command is given."""
        _SHELL_SHORTHANDS = {"bash": "/usr/bin/bash", "sh": "/bin/sh"}
        shell = _SHELL_SHORTHANDS.get(shell, shell)

        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            print("ssh not found on PATH", file=sys.stderr)
            raise SystemExit(1)

        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        status = make_api_request(cfg.url, cfg.token, "GET", f"/api/app_status/{app_id}").json()
        container_id = status.get("container_id")
        if not container_id:
            print(f"No running container for {app_name} (status: {status.get('status')})", file=sys.stderr)
            raise SystemExit(1)

        interactive = not args and sys.stdin.isatty()
        exec_flags = "-it" if interactive else "-i"
        exec_cmd = f"podman exec {exec_flags} {shlex.quote(container_id)} {shlex.quote(shell)}"
        if args:
            exec_cmd += f" -c {shlex.quote(' '.join(args))}"

        remote_cmd = f"cd ~/openhost && /home/host/.pixi/bin/pixi run -e dev {exec_cmd}"
        cmd = [ssh_bin]
        if interactive:
            cmd.append("-t")
        if cfg.ssh_key:
            cmd += ["-i", cfg.ssh_key]
        cmd += [f"host@{cfg.hostname}", remote_cmd]
        raise SystemExit(subprocess.call(cmd))

    @cappa.command(name="list")
    def list_apps(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """List all apps and their status."""
        apps = make_api_request(cfg.url, cfg.token, "GET", "/api/apps").json()
        if not apps:
            print("No apps installed.")
            return
        max_name = max(len(a.get("name", "")) for a in apps)
        for app in sorted(apps, key=lambda a: a.get("name", "")):
            name = app.get("name", "?")
            s = app.get("status", "unknown")
            err = app.get("error_message", "")
            line = f"  {name:<{max_name}}  {s}"
            if err:
                line += f"  ({err})"
            print(line)

    @cappa.command(name="diagnostics")
    def diagnostics(
        self,
        app_name: Annotated[str, cappa.Arg(help="App name")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Print a per-app diagnostics bundle (JSON) for debugging."""
        app_id = resolve_app_id_by_name(cfg.url, cfg.token, app_name)
        result = make_api_request(cfg.url, cfg.token, "GET", f"/api/app_diagnostics/{app_id}")
        print(json.dumps(result.json(), indent=2))


@cappa.command(name="tokens", help="Manage API tokens.")
@attrs.define
class TokensCmd:
    @cappa.command(name="list")
    def list_tokens(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """List API tokens."""
        tokens = make_api_request(cfg.url, cfg.token, "GET", "/api/tokens").json()
        if not tokens:
            print("No tokens.")
            return
        for t in tokens:
            exp = t.get("expires_at") or "never"
            expired = " (expired)" if t.get("expired") else ""
            print(f"  [{t['id']}] {t['name']}  expires: {exp}{expired}")

    @cappa.command(name="create")
    def create(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        name: Annotated[str, cappa.Arg(long=True, help="Token name")] = "Untitled",
        expiry_hours: Annotated[str, cappa.Arg(long=True, help='Hours until expiry, or "never"')] = "8",
    ) -> None:
        """Create a new API token."""
        result = make_api_request(
            cfg.url,
            cfg.token,
            "POST",
            "/api/tokens",
            data={"name": name, "expiry_hours": expiry_hours},
        ).json()
        print(f"Token: {result['token']}")
        print(f"  name: {result.get('name', name)}")
        print(f"  expires: {result.get('expires_at') or 'never'}")

    @cappa.command(name="delete")
    def delete(
        self,
        token_id: Annotated[int, cappa.Arg(help="Token ID")],
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Delete an API token."""
        make_api_request(cfg.url, cfg.token, "DELETE", f"/api/tokens/{token_id}")
        print(f"Deleted token {token_id}")


@cappa.command(name="logs", help="View compute space logs.")
@attrs.define
class LogsCmd:
    follow: Annotated[bool, cappa.Arg(long=True, help="Poll for new logs")] = False
    interval: Annotated[int, cappa.Arg(long=True, help="Poll interval in seconds")] = 5

    def __call__(self, cfg: Annotated[config.Instance, Dep(resolve_instance)]) -> None:
        if not self.follow:
            print(make_api_request(cfg.url, cfg.token, "GET", "/api/compute_space_logs").text)
            return
        seen = ""
        while True:
            text = make_api_request(cfg.url, cfg.token, "GET", "/api/compute_space_logs").text
            if text != seen:
                print(text[len(seen) :], end="", flush=True)
                seen = text
            time.sleep(self.interval)


@cappa.command(name="instance", help="Manage configured instances.")
@attrs.define
class InstanceCmd:
    @cappa.command(name="login")
    def login(self) -> None:
        """Log in to an instance interactively."""
        url = input("Compute space URL (eg username.host.imbue.com/): ").strip().rstrip("/")
        if not url:
            print("No URL provided.", file=sys.stderr)
            raise SystemExit(1)
        url = config.normalize_url(url)
        hostname = config.hostname_from_url(url)

        print("\nOpen this link and create an API token:")
        print(f"  {url}")
        print()

        token = input("Paste your API token here: ").strip()
        if not token:
            print("No token provided.", file=sys.stderr)
            raise SystemExit(1)

        print("\nVerifying...", end=" ", flush=True)
        try:
            resp = httpx.get(
                f"{url}/dashboard",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                follow_redirects=False,
            )
            if resp.status_code != 200:
                print("failed (invalid token or unreachable)", file=sys.stderr)
                raise SystemExit(1)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            print(f"failed ({e})", file=sys.stderr)
            raise SystemExit(1) from None
        print("ok")

        alias = input(f"\nAlias for '{hostname}' (optional, press Enter to skip): ").strip() or None
        inst = config.Instance(hostname=hostname, token=token, alias=alias)
        multi = _load_or_create()
        multi.upsert_instance(inst).save()

        display_name = alias or hostname
        print(f"\nSaved as '{hostname}'.", end="")
        if alias:
            print(f" Alias: '{alias}'.")
        else:
            print()
        if not multi.default_instance:
            answer = input(f"Set '{display_name}' as default instance? [Y/n] ").strip().lower()
            if answer != "n":
                _load_or_exit().evolve(default_instance=hostname).save()
                print(f"Default instance set to '{display_name}'")
            else:
                print(f"Use with: oh --instance {display_name} <command>")
        else:
            print(f"Use with: oh --instance {display_name} <command>")

    @cappa.command(name="list")
    def list_instances(self) -> None:
        """List all configured instances."""
        multi = _load_or_exit()

        if not multi.instances:
            print("No instances configured.")
            return

        max_name = max(len(h) for h in multi.instances)
        for hostname, inst in sorted(multi.instances.items()):
            flags: list[str] = []
            if hostname == multi.default_instance:
                flags.append("default")
            if inst.alias:
                flags.append(f"alias: {inst.alias}")
            if inst.ssh_key:
                flags.append(f"ssh-key: {inst.ssh_key}")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {hostname:<{max_name}}{flag_str}")

    @cappa.command(name="add")
    def add(
        self,
        url: Annotated[str, cappa.Arg(help="Instance URL or hostname")],
        token: Annotated[str, cappa.Arg(help="API token")],
        alias: Annotated[str | None, cappa.Arg(long=True, help="Short alias")] = None,
        set_default: Annotated[bool, cappa.Arg(long=True, help="Set as default instance")] = False,
    ) -> None:
        """Add a new instance to the config."""
        hostname = config.hostname_from_url(config.normalize_url(url))
        inst = config.Instance(hostname=hostname, token=token, alias=alias)
        _load_or_create().upsert_instance(inst, set_default=set_default).save()
        print(f"Added instance '{hostname}'")
        if alias:
            print(f"  alias: {alias}")
        if set_default:
            print("  set as default")

    @cappa.command(name="remove")
    def remove(
        self,
        name: Annotated[str, cappa.Arg(help="Hostname or alias")],
    ) -> None:
        """Remove an instance from the config."""
        multi = _load_or_exit()
        try:
            multi.remove_instance(name).save()
        except config.InstanceNotFoundError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None
        print(f"Removed instance '{name}'")

    @cappa.command(name="set-default")
    def set_default(
        self,
        name: Annotated[str, cappa.Arg(help="Hostname or alias")],
    ) -> None:
        """Set the default instance."""
        multi = _load_or_exit()
        try:
            hostname = multi._resolve_name(name)
        except config.InstanceNotFoundError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None
        multi.evolve(default_instance=hostname).save()
        print(f"Default instance set to '{name}'")

    @cappa.command(name="alias")
    def alias(
        self,
        name: Annotated[str, cappa.Arg(help="Hostname or current alias")],
        new_alias: Annotated[str, cappa.Arg(help="Alias to set")],
    ) -> None:
        """Set or update an alias for an instance."""
        multi = _load_or_exit()
        try:
            hostname = multi._resolve_name(name)
        except config.InstanceNotFoundError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None
        old = multi.instances[hostname]
        updated = config.Instance(hostname=old.hostname, token=old.token, alias=new_alias)
        multi.upsert_instance(updated).save()
        print(f"Alias '{new_alias}' set for {hostname}")

    @cappa.command(name="token")
    def token(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
    ) -> None:
        """Print the stored API token for an instance."""
        print(cfg.token)

    @cappa.command(name="configure-ssh-key")
    def configure_ssh_key(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        key_path: Annotated[str, cappa.Arg(help="Path to SSH private key")],
    ) -> None:
        """Store an SSH key path for an instance."""
        resolved = Path(key_path).expanduser().resolve()
        if not resolved.exists():
            print(f"Key file not found: {resolved}", file=sys.stderr)
            raise SystemExit(1)

        multi = _load_or_exit()
        updated = config.Instance(
            hostname=cfg.hostname,
            token=cfg.token,
            alias=cfg.alias,
            ssh_key=str(resolved),
        )
        multi.upsert_instance(updated).save()
        print(f"SSH key for {cfg.hostname} set to {resolved}")

    @cappa.command(name="ssh")
    def ssh(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        args: Annotated[list[str] | None, cappa.Arg(num_args=-1, help="Extra arguments passed to ssh")] = None,
    ) -> None:
        """SSH into an instance as the host user."""
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            print("ssh not found on PATH", file=sys.stderr)
            raise SystemExit(1)

        cmd = [ssh_bin]
        if cfg.ssh_key:
            cmd += ["-i", cfg.ssh_key]
        cmd += [f"host@{cfg.hostname}", *(args or [])]
        raise SystemExit(subprocess.call(cmd))

    @cappa.command(name="rsync")
    def rsync(
        self,
        cfg: Annotated[config.Instance, Dep(resolve_instance)],
        args: Annotated[list[str] | None, cappa.Arg(num_args=-1, help="Arguments passed to rsync")] = None,
    ) -> None:
        """Run rsync against an instance over SSH."""
        rsync_bin = shutil.which("rsync")
        if not rsync_bin:
            print("rsync not found on PATH", file=sys.stderr)
            raise SystemExit(1)

        ssh_cmd = "ssh"
        if cfg.ssh_key:
            ssh_cmd = f"ssh -i {cfg.ssh_key}"
        cmd = [rsync_bin, "-e", ssh_cmd, *(args or [])]
        raise SystemExit(subprocess.call(cmd))


@cappa.command(name="version", help="Show the git branch/SHA of the running openhost.")
@attrs.define
class Version:
    def __call__(self, cfg: Annotated[config.Instance, Dep(resolve_instance)]) -> None:
        result = make_api_request(cfg.url, cfg.token, "GET", "/api/version").json()
        sha = result.get("sha") or ""
        short = result.get("short_sha") or (sha[:8] if sha else "")
        branch = result.get("branch") or "(detached HEAD)"
        if not sha:
            print(f"{cfg.url}: openhost is not a git checkout — no version info available")
            return
        dirty = " (dirty)" if result.get("dirty") else ""
        print(f"{cfg.url}: {branch} @ {short}{dirty}")
        print(f"  full sha: {sha}")


@cappa.command(name="diagnostics", help="Print a full instance diagnostics bundle (JSON) for debugging.")
@attrs.define
class Diagnostics:
    def __call__(self, cfg: Annotated[config.Instance, Dep(resolve_instance)]) -> None:
        result = make_api_request(cfg.url, cfg.token, "GET", "/api/diagnostics")
        print(json.dumps(result.json(), indent=2))


@cappa.command(name="curl", help="curl with your OpenHost bearer token injected.")
@attrs.define
class Curl:
    args: Annotated[
        list[str],
        cappa.Arg(num_args=-1, help="Arguments passed to curl"),
    ] = attrs.Factory(list)

    def __call__(self, cfg: Annotated[config.Instance, Dep(resolve_instance)]) -> None:
        curl = shutil.which("curl")
        if not curl:
            print("curl not found on PATH", file=sys.stderr)
            raise SystemExit(1)
        cmd = [curl, "-H", f"Authorization: Bearer {cfg.token}", *self.args]
        raise SystemExit(subprocess.call(cmd))


@cappa.command(name="oh", help="OpenHost compute space CLI — manage things in your compute space.")
@attrs.define
class Oh:
    subcommand: cappa.Subcommands[Status | AppCmd | TokensCmd | LogsCmd | InstanceCmd | Curl | Version | Diagnostics]
    instance: Annotated[
        str | None,
        cappa.Arg(long=True, default=None, propagate=True, help="Target a specific named instance"),
    ] = None


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    cappa.invoke(Oh, color=False)
