import os
import re
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import cattrs
import tomli_w
import typed_settings

# TLS cert provider selection (see Config.cert_provider).
# "acme" is the default bring-your-own-ACME-credentials path (unchanged, fully
# backward compatible). "cert_api" fetches certs from the openhost-cert-api
# broker, which holds the ACME account so the instance never sees ACME creds.
CERT_PROVIDER_ACME = "acme"
CERT_PROVIDER_CERT_API = "cert_api"

_CERT_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def _normalized_cert_domain(domain_name: str) -> str:
    name = domain_name.strip().split(":", 1)[0].lower()
    if not _CERT_DOMAIN_RE.fullmatch(name):
        raise ValueError(f"invalid certificate domain: {domain_name!r}")
    return name


@attr.s(auto_attribs=True, frozen=True)
class Config:
    ## Server
    # the local IP to bind the compute space web server to.
    host: str
    # the local port to bind the compute space web server to.
    port: int

    ## TLS
    acquire_tls_cert_if_missing: bool
    acme_email: str | None
    acme_account_key_path: str | None
    acme_directory_url: str | None

    # Which cert provider to use when acquiring a missing TLS cert:
    #   CERT_PROVIDER_ACME ("acme", default) — bring-your-own ACME account key (BYO-ACME).
    #   CERT_PROVIDER_CERT_API ("cert_api")  — fetch from the openhost-cert-api broker.
    # The broker path still uses CoreDNS for the DNS-01 write, but needs no ACME account key.
    cert_provider: str
    # openhost-cert-api broker base URL, e.g. "https://cert-api.example.com" (cert_api provider only).
    cert_api_base_url: str | None
    # Keycloak client-credentials auth for the broker (cert_api provider only).  The instance
    # fetches a bearer token from this issuer and presents it to cert-api, so no shared secret
    # or ACME account key lives on the instance.  Provisioning injects these per instance.
    #   issuer URL, e.g. "https://keycloak.<zone>/realms/openhost-customers"
    cert_api_keycloak_issuer_url: str | None
    #   per-instance client id, e.g. "instance-<subdomain>"
    cert_api_keycloak_client_id: str | None
    #   per-instance client secret (the only sensitive value — treat like the ACME account key)
    cert_api_keycloak_client_secret: str | None

    ## coredns (only really needed if acquiring TLS certs via DNS-01, or if using NS dns records)
    coredns_enabled: bool
    public_ip: str | None

    start_caddy: bool

    my_openhost_redirect_domain: str

    ## Data
    data_root_dir: str
    apps_dir_override: str | None

    # Minimum free disk space in MB the storage guard enforces (0 = no enforcement).
    storage_min_free_mb: int

    # How often (seconds) to prune dangling container images (0 = disabled).
    image_prune_interval_seconds: int

    # Age (seconds) above which a tagged Cloud in a Bottle app image with no matching app
    # in the DB is treated as orphaned and pruned (0 = never prune orphaned
    # tagged images).
    image_orphan_max_age_seconds: int

    ## Ports
    port_range_start: int
    port_range_end: int

    # First-boot claim-token gate. When True, /setup rejects any request that
    # doesn't supply a token matching the one in claim_token_path — preventing
    # a MITM from racing the operator to set the owner password. When True but
    # no token file is present, /setup rejects everyone (fail-safe). Set this
    # explicitly to False only when /setup is reachable only by the operator
    # (e.g. loopback-only local dev).
    claim_token_required: bool

    # Apps to deploy at /setup completion (set to [] to opt out).
    # Each entry is either:
    #   - a bare dirname under apps_dir (vendored builtin, e.g. "secrets_v2"), or
    #   - a remote git URL the router will clone on first boot
    #     (e.g. "https://github.com/cloud-in-a-bottle/app-catalog").
    # Remote URLs are dispatched through the same clone path as
    # /api/add_app and do not need to be present on disk ahead of time.
    default_apps: list[str]

    def __attrs_post_init__(self) -> None:
        # Validate cert provider selection up front so any Config object can be
        # trusted as valid by the rest of the system (rather than discovering a
        # misconfiguration only at cert-acquisition time).
        if self.cert_provider not in (CERT_PROVIDER_ACME, CERT_PROVIDER_CERT_API):
            raise ValueError(
                f"Unknown cert_provider {self.cert_provider!r} (expected "
                f"{CERT_PROVIDER_ACME!r} or {CERT_PROVIDER_CERT_API!r})"
            )
        if self.cert_provider == CERT_PROVIDER_CERT_API:
            # The cert_api broker path needs the broker URL. The per-instance
            # credential lives in the DB settings table (the shared Imbue identity),
            # so it can't be validated here at construction time; its presence is
            # checked at cert-acquisition time. The cert_api_keycloak_* config fields
            # are a deprecated fallback for already-deployed instances.
            if not self.cert_api_base_url:
                raise ValueError("cert_api_base_url must be set in config to use the cert_api provider")

    def evolve(self, **kwargs: Any) -> Self:
        return attr.evolve(self, **kwargs)

    def _to_toml_dict(self) -> dict[str, dict[str, Any]]:
        d = {k: v for k, v in attr.asdict(self).items() if v is not None}
        return {"openhost": d}

    def to_toml_str(self) -> str:
        return tomli_w.dumps(self._to_toml_dict())

    def to_toml(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump(self._to_toml_dict(), f)

    @classmethod
    def from_toml(cls, path: str) -> Self:
        with open(path, "rb") as f:
            d = tomllib.load(f)
        return cattrs.structure(d.get("openhost", d), cls)

    @property
    def persistent_data_dir(self) -> str:
        return os.path.join(self.data_root_dir, "persistent_data")

    @property
    def temporary_data_dir(self) -> str:
        return os.path.join(self.data_root_dir, "temporary_data")

    @property
    def app_archive_dir(self) -> str:
        # JuiceFS FUSE mountpoint for the archive tier.  Lives under
        # data_root_dir (NOT persistent_data_dir) so restic backups don't
        # double-store bytes that already live in S3.  The archive tier is
        # ALWAYS a JuiceFS mount here regardless of backend; only JuiceFS's
        # object storage differs (local file store vs S3 — see
        # ``local_archive_object_store_dir``).
        return os.path.join(self.data_root_dir, "app_archive")

    @property
    def local_archive_object_store_dir(self) -> str:
        # Directory that backs JuiceFS's ``file`` object store on the default
        # 'local' backend.  This holds JuiceFS's raw chunk objects (NOT a
        # POSIX view of app files — apps always go through the mount at
        # ``app_archive_dir``).  Kept under ``persistent_data_dir`` so it
        # (a) survives container rebuilds and (b) IS captured by restic
        # backups — local archive data has no other durable copy, unlike the
        # S3-backed tier (whose bytes live in the operator's bucket, so the
        # mountpoint is excluded from backups).
        return os.path.join(self.persistent_data_dir, "app_archive_local_objects")

    @property
    def apps_dir(self) -> str:
        # where openhost/apps/ is mounted
        if self.apps_dir_override:
            return self.apps_dir_override
        return os.path.join(self.data_root_dir, "apps")

    @property
    def openhost_data_path(self) -> Path:
        # Cloud in a Bottle-specific data, including the sqlite db and TLS certs.
        return Path(self.persistent_data_dir) / "openhost"

    @property
    def openhost_repo_path(self) -> Path:
        # compute_space/src/compute_space/config.py -> openhost repo root
        return Path(__file__).resolve().parent.parent.parent.parent

    @property
    def db_path(self) -> str:
        return str(self.openhost_data_path / "router.db")

    @property
    def certs_dir(self) -> Path:
        """Directory containing one named TLS certificate pair per domain."""
        return self.openhost_data_path / "certs"

    def cert_path_for(self, domain_name: str) -> Path:
        return self.certs_dir / f"{_normalized_cert_domain(domain_name)}.pem"

    def key_path_for(self, domain_name: str) -> Path:
        return self.certs_dir / f"{_normalized_cert_domain(domain_name)}.key"

    def cert_key_paths_for(self, domain_name: str) -> tuple[Path, Path]:
        """Return the domain's normalized, role-independent certificate paths."""
        return self.cert_path_for(domain_name), self.key_path_for(domain_name)

    @property
    def coredns_corefile_path(self) -> Path:
        return self.openhost_data_path / "Corefile"

    @property
    def zones_dir(self) -> Path:
        """Directory for per-domain CoreDNS zone files (domains beyond the primary)."""
        return self.openhost_data_path / "zones"

    @property
    def caddyfile_path(self) -> Path:
        return self.openhost_data_path / "Caddyfile"

    @property
    def caddy_admin_socket_path(self) -> Path:
        """Unix socket for Caddy's admin API — the control surface used for zero-downtime config
        reloads.  A filesystem path (owned by the router user), so it's never network-exposed."""
        return self.openhost_data_path / "caddy-admin.sock"

    @property
    def keys_dir(self) -> str:
        return str(Path(self.openhost_data_path) / "keys")

    @property
    def claim_token_path(self) -> str:
        return str(Path(self.openhost_data_path) / "claim_token")

    @property
    def default_apps_sentinel_path(self) -> str:
        return str(Path(self.openhost_data_path) / "default_apps.json")

    def make_all_dirs(self) -> None:
        """Make all necessary directories for the config."""
        assert os.path.exists(self.data_root_dir)
        os.makedirs(self.persistent_data_dir, exist_ok=True)
        os.makedirs(self.temporary_data_dir, exist_ok=True)
        # Skip app_archive_dir: it is the JuiceFS FUSE mountpoint and must be
        # created + mounted by ``archive_backend.attach_on_startup`` (which
        # formats the local file volume on first boot and starts the mount)
        # once the DB — which holds the backend state — is readable, not here.
        # The local object store dir (``local_archive_object_store_dir``) is
        # likewise created by ``format_local_volume``.
        os.makedirs(self.apps_dir, exist_ok=True)
        os.makedirs(self.openhost_data_path, exist_ok=True)
        os.makedirs(self.keys_dir, exist_ok=True)


@attr.s(auto_attribs=True, frozen=True)
class DefaultConfig(Config):
    # Server
    host: str = "127.0.0.1"
    port: int = 8080

    # coredns (only truly needed for DNS-01 TLS cert acquisition)
    coredns_enabled: bool = False
    public_ip: str | None = None

    # TLS
    acquire_tls_cert_if_missing: bool = False
    acme_email: str | None = None
    acme_account_key_path: str | None = None
    acme_directory_url: str | None = None

    # Default to the BYO-ACME path so existing deployments are unaffected.
    cert_provider: str = CERT_PROVIDER_ACME
    # TODO: swap back to the canonical broker "https://api.selfhost.imbue.com" once the
    # service is deployed (a DNS record will be added when it goes up).  For now this points
    # at the QA broker instance so the cert_api path can be exercised end-to-end.
    # Only consulted when cert_provider == CERT_PROVIDER_CERT_API.
    cert_api_base_url: str | None = "https://openhost-cert-api.openhost-qa.selfhost.imbue.com/"
    # Keycloak client-credentials config — all injected by provisioning, no safe default.
    cert_api_keycloak_issuer_url: str | None = None
    cert_api_keycloak_client_id: str | None = None
    cert_api_keycloak_client_secret: str | None = None

    start_caddy: bool = True

    my_openhost_redirect_domain: str = "my.selfhost.imbue.com"

    # Data
    data_root_dir: str = "/opt/openhost"
    apps_dir_override: str | None = None  # if None, defaults to data_root_dir/apps

    # Minimum free disk space in MB the storage guard enforces (0 = no enforcement).
    # Enabled by default with a modest headroom so a runaway disk can't silently
    # take an instance fully down before the owner notices. Operators who want a
    # different threshold (or to disable it) set this in the router config and
    # reboot.
    storage_min_free_mb: int = 500

    # How often (seconds) the periodic pruner removes dangling container images
    # (0 = disabled).  Rebuilds re-tag ``openhost-{app}:latest`` and orphan the
    # previous image, so untagged layers accumulate; pruning them on a schedule
    # keeps them from filling the disk.  Only dangling images are removed, so
    # stopped apps never need rebuilding.  Defaults to every 6 hours.
    image_prune_interval_seconds: int = 6 * 60 * 60

    # Age (seconds) above which a tagged ``openhost-{name}:latest`` image whose
    # app no longer exists in the DB (in any status) is pruned by the periodic
    # sweep.  App removal already deletes the app's image, so this only reclaims
    # tagged images orphaned by a removal that failed or predated that logic.
    # The age guard ensures an image built for an app whose DB row is not yet
    # committed (mid-deploy) is never reaped.  0 disables orphan pruning.
    # Defaults to 7 days.
    image_orphan_max_age_seconds: int = 7 * 24 * 60 * 60

    # Fail-safe default: require a claim token at /setup. Callers that want
    # the open-setup behavior (local-dev loopback) must set this False.
    claim_token_required: bool = True

    # Ports
    port_range_start: int = 9000
    port_range_end: int = 9999

    # Apps to auto-deploy at /setup completion.  Entries are either:
    #   - a bare dirname under apps_dir (vendored builtin), or
    #   - a remote git URL cloned on demand (see core/default_apps).
    default_apps: list[str] = attr.Factory(
        lambda: [
            "https://github.com/cloud-in-a-bottle/secrets",
            "https://github.com/cloud-in-a-bottle/bottled-filestash",
            "oauth_provider",
            "https://github.com/cloud-in-a-bottle/app-catalog",
            "https://github.com/cloud-in-a-bottle/backup",
            "https://github.com/cloud-in-a-bottle/bottled-community-chat",
        ]
    )


def load_config() -> Config:
    """Load config from OPENHOST_ prefixed env vars, env-selected TOML file, or default config, in that order.

    Prefer ``OPENHOST_ROUTER_CONFIG`` (new CLI name) and fall back to
    ``OPENHOST_CONFIG`` for backward compatibility.
    """
    path = os.environ.get("OPENHOST_ROUTER_CONFIG") or os.environ.get("OPENHOST_CONFIG")
    if path:
        return typed_settings.load(DefaultConfig, appname="openhost", config_files=[path])
    else:
        return typed_settings.load(DefaultConfig, appname="openhost")


_active_config: Config | None = None


def set_active_config(config: Config) -> None:
    """Register the active config for the running web app.

    Called once at app-factory time so ``get_config()`` works framework-neutrally
    (the previous Quart implementation read it from ``current_app``).
    """
    global _active_config
    _active_config = config


def get_config() -> Config:
    """Return the active config registered via ``set_active_config``."""
    if _active_config is None:
        raise RuntimeError("set_active_config() must be called before get_config()")
    return _active_config


def provide_config() -> Config:
    """Litestar dependency: hand the active config to a route or other dep.

    Wraps ``get_config()`` so handlers can take ``config: Config`` instead of
    calling the module-level accessor.  ``get_config()`` stays available for
    non-DI callers (middleware, ``core/`` helpers).

    litestar got confused by returning a DefaultConfig so we convert it back to plain Config.
    """
    active = get_config()
    if type(active) is Config:
        return active
    return Config(**{f.name: getattr(active, f.name) for f in attr.fields(Config)})
