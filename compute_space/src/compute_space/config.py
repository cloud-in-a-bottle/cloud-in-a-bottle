import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import cattrs
import tomli_w
import typed_settings

from compute_space.core.tls.keycloak import KeycloakClientCredentials

# TLS cert provider selection (see Config.cert_provider).
# "acme" is the default bring-your-own-ACME-credentials path (unchanged, fully
# backward compatible). "cert_api" fetches certs from the openhost-cert-api
# broker, which holds the ACME account so the instance never sees ACME creds.
CERT_PROVIDER_ACME = "acme"
CERT_PROVIDER_CERT_API = "cert_api"

# The email-specific fields that must ALL be present for email to be enabled.
# Email has no separate on/off flag; ``Config.email_enabled`` is derived from
# these (plus public_ip). Kept as a module constant so validation and the
# property agree on one list. These authenticate to the email frontend and relay
# outbound through the central proxy.
#
# NOTE: public_ip is deliberately NOT in this list. It is a general-purpose
# CoreDNS field present on essentially every instance (email or not), so using it
# as an email-participation signal would make every non-email instance look
# "partially configured". Instead, public_ip is required *additionally* only when
# these email fields are all set (inbound is always direct-to-instance, so the
# MX/A records point at the instance's IP, which must then be known).
#
# NOTE: the Keycloak client-credentials are resolved (email_keycloak_*_resolved),
# which fall back to the cert-api client — so an instance that was cert-api
# provisioned already satisfies the Keycloak prerequisites for free. In practice
# the only value an upgraded instance is missing is email_proxy_base_url. This is
# what makes "enable email on an existing instance" seamless: set one value.
_EMAIL_PREREQ_ATTRS = (
    "email_proxy_base_url",
    "email_keycloak_issuer_url_resolved",
    "email_keycloak_client_id_resolved",
    "email_keycloak_client_secret_resolved",
)
# The explicit email Keycloak override fields. Setting some-but-not-all is a typo
# (set all three to override the cert-api client, or none to inherit it) — checked
# in __attrs_post_init__.
_EMAIL_KEYCLOAK_OVERRIDE_FIELDS = (
    "email_keycloak_issuer_url",
    "email_keycloak_client_id",
    "email_keycloak_client_secret",
)

# Config keys that older versions wrote into config.toml but that no longer map
# to a Config field. typed-settings (and cattrs, forbid_extra_keys) reject any
# unknown key in a config file, so a config.toml written by an older deploy would
# otherwise fail to load after the field is removed — breaking the router on a
# code-only redeploy (ansible does not re-render config.toml unless forced).
# These keys are silently dropped on load. Add a key here when removing a field.
#   - email_inbound_mode / email_inbound_mx_host: the SES-based inbound mode was
#     removed; inbound is now always delivered directly to the instance.
#   - email_enabled: replaced by a derived property (email is on when its
#     prerequisites are present); older configs may still carry the literal key.
_OBSOLETE_CONFIG_KEYS = frozenset(
    {
        "email_inbound_mode",
        "email_inbound_mx_host",
        "email_enabled",
    }
)


def _drop_obsolete_keys(section: dict[str, Any]) -> dict[str, Any]:
    """Return ``section`` without any keys in ``_OBSOLETE_CONFIG_KEYS``."""
    return {k: v for k, v in section.items() if k not in _OBSOLETE_CONFIG_KEYS}


def _lowercase(s: str) -> str:
    # mypy can't handle str.lower apparently
    return s.lower()


# A DNS label: 1-63 chars, alphanumeric plus internal hyphens.  A well-formed
# domain is one or more such labels joined by single dots (no empty labels, so no
# leading/trailing/double dots).  Deliberately conservative — used to reject a
# malformed email_custom_domain at config load.
_DOMAIN_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def _is_well_formed_domain(domain: str) -> bool:
    domain = domain.strip().lower().rstrip(".")
    if not domain or len(domain) > 253:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    return all(_DOMAIN_LABEL_RE.match(label) for label in labels)


@attr.s(auto_attribs=True, frozen=True)
class DelegationRecord:
    """A single DNS record the owner must add at their registrar.

    Used to tell the owner exactly what to paste to delegate a custom mail domain
    to this instance (see Config.custom_domain_delegation_record).
    """

    name: str
    record_type: str
    value: str

    def as_display_line(self) -> str:
        """A registrar-style one-liner, e.g. 'mail.mydomain.com  NS  ns.<zone>'."""
        return f"{self.name}   {self.record_type}   {self.value}"


@attr.s(auto_attribs=True, frozen=True)
class Config:
    ## Server
    # zone_domain is where the compute space is hosted, eg `host.example.com`
    # it can optionally include a non-80/443 port, if necessary.
    zone_domain: str = attr.ib(converter=_lowercase)
    # the local IP to bind the compute space web server to.
    host: str
    # the local port to bind the compute space web server to.
    port: int

    ## Instance identity (shared Imbue credential)
    # A single per-instance credential the instance uses to authenticate to Imbue for any Imbue-provided service
    # (cert acquisition, email, ...). It is a client-credentials grant: the instance fetches a short-lived bearer from
    # ``imbue_identity_issuer_url`` using ``imbue_identity_client_id`` + ``imbue_identity_client_secret``, and presents
    # it (with the per-service audience) to that service. It reaches the instance one of two ways that produce the same
    # result: injected at provision time for managed spaces, or obtained via the "Connect to Imbue" flow in Settings
    # for non-managed spaces. When absent, dependent services simply stay off (no boot failure).
    #
    # The per-service ``cert_api_keycloak_*`` / ``email_keycloak_*`` fields below are DEPRECATED overrides kept for
    # backward compatibility with already-deployed configs: each service resolves its credential from its own override
    # first, then falls back to this shared identity (see the ``*_resolved`` properties). New provisioning writes only
    # these shared fields.
    imbue_identity_issuer_url: str | None
    imbue_identity_client_id: str | None
    imbue_identity_client_secret: str | None

    ## TLS
    tls_enabled: bool
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

    ## Email
    # Email has no on/off flag: it is enabled automatically when its prerequisites are present. The derived
    # ``email_enabled`` property is True iff the proxy URL, the per-instance Keycloak client-credentials, and the
    # public IP are all set. Provisioning supplies these when email infra is configured; otherwise the instance runs
    # without email (no boot failure).
    # Base URL of the email API, e.g. "https://openhost.imbue.com". The instance calls its /api/email/* endpoints.
    email_proxy_base_url: str | None
    # Keycloak client-credentials for the email API. Reuses the same per-instance client as cert_api when present, but
    # kept as distinct fields so email can be enabled independently.
    email_keycloak_issuer_url: str | None
    email_keycloak_client_id: str | None
    email_keycloak_client_secret: str | None
    # Inbound mail is ALWAYS delivered directly to this instance: MX points at mail.<zone> -> public_ip and the mail
    # server receives on port 25, so inbound never traverses OpenHost infra and the platform cannot read tenant mail.
    # Outbound relays through the central proxy -> SES. Requires inbound port 25 be reachable.
    # Optional DMARC aggregate-report address published in the _dmarc record.
    email_dmarc_rua: str | None
    # Optional custom mail domain the owner delegated to this instance's CoreDNS with a single NS record (e.g.
    # "mail.mydomain.com"). When set, the instance serves it as a second authoritative zone and publishes the same
    # SPF/DKIM/DMARC/MX records, so mail can send/receive as that domain in addition to the built-in <zone> subdomain.
    email_custom_domain: str | None
    # App name(s) allowed to fetch the SMTP relay config from /api/email/relay-config. The relay credential is not
    # stored on the instance; it is fetched at runtime and the endpoint is scoped to these mailbox app names.
    email_mailbox_app_names: list[str]
    # Default apps (bare dirnames or remote git URLs, same as ``default_apps``) auto-deployed ONLY when email is
    # enabled — the mailbox server + webmail client. Kept separate from ``default_apps`` so a non-email instance has
    # no mailbox; appended by ``effective_default_apps`` when ``email_enabled`` is True.
    email_default_apps: list[str]

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

    # Age (seconds) above which a tagged OpenHost app image with no matching app
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
    #     (e.g. "https://github.com/imbue-openhost/openhost-catalog").
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
            # The cert_api broker path needs the broker URL. Validate it directly.
            if not self.cert_api_base_url:
                raise ValueError("cert_api_base_url must be set in config to use the cert_api provider")
            # It also needs the per-instance client-credentials, which resolve from the deprecated
            # cert_api_keycloak_* override or the shared imbue_identity_* fields (either source satisfies
            # the provider). Report the settable field name, not the internal *_resolved property.
            for field_name, resolved_name in (
                ("cert_api_keycloak_issuer_url", "cert_api_keycloak_issuer_url_resolved"),
                ("cert_api_keycloak_client_id", "cert_api_keycloak_client_id_resolved"),
                ("cert_api_keycloak_client_secret", "cert_api_keycloak_client_secret_resolved"),
            ):
                if not getattr(self, resolved_name):
                    raise ValueError(f"{field_name} must be set in config to use the cert_api provider")
        # Email has no explicit on/off flag: it is enabled automatically when all
        # of its prerequisites resolve (see the email_enabled property). The
        # Keycloak client-credentials fall back to the cert-api client, so in the
        # common case the only email_* value that needs setting is
        # email_proxy_base_url. We do NOT hard-fail when email is simply off. But
        # we surface two misconfigurations:
        #
        #  (a) The stored email_keycloak_* fields are partially set (a typo — set
        #      all three to override the cert-api client, or none to inherit it).
        kc_set = sum(1 for name in _EMAIL_KEYCLOAK_OVERRIDE_FIELDS if getattr(self, name))
        if 0 < kc_set < len(_EMAIL_KEYCLOAK_OVERRIDE_FIELDS):
            missing = [name for name in _EMAIL_KEYCLOAK_OVERRIDE_FIELDS if not getattr(self, name)]
            raise ValueError(
                "email Keycloak credentials are partially configured: set all of "
                f"{sorted(_EMAIL_KEYCLOAK_OVERRIDE_FIELDS)} to override the cert-api client, "
                f"or none to inherit it. Missing: {sorted(missing)}"
            )
        #  (b) email_proxy_base_url is set with a PARTIALLY-resolved credential
        #      (some but not all of issuer/id/secret) — a typo. A fully-absent
        #      credential is NOT an error: that is the "front door configured, but
        #      the instance hasn't been connected to Imbue yet" state (email simply
        #      stays off until the Connect-to-Imbue flow supplies the credential).
        if self.email_proxy_base_url:
            cred_attrs = (
                "email_keycloak_issuer_url_resolved",
                "email_keycloak_client_id_resolved",
                "email_keycloak_client_secret_resolved",
            )
            cred_set = sum(1 for a in cred_attrs if getattr(self, a))
            if 0 < cred_set < len(cred_attrs):
                missing = [a for a in cred_attrs if not getattr(self, a)]
                raise ValueError(
                    "email_proxy_base_url is set but the instance credential is only partially "
                    f"resolved (missing {sorted(missing)}). Provide a complete credential "
                    "(imbue_identity_*, the cert-api client, or explicit email_keycloak_*) or none."
                )
            # When the credential fully resolves, email is enabled — inbound is
            # always direct-to-instance, so the MX/A records need the public IP.
            if cred_set == len(cred_attrs) and not self.public_ip:
                raise ValueError("public_ip must be set in config when email is enabled")
        # Validate the custom mail domain shape (if set) regardless of whether
        # email is enabled, so a typo surfaces at config load rather than at the
        # first boot that turns email on.
        custom = self.email_custom_domain_normalized
        if custom is not None:
            if not _is_well_formed_domain(custom):
                raise ValueError(f"email_custom_domain {self.email_custom_domain!r} is not a well-formed domain")
            # The custom domain must be distinct from the built-in zone (which is
            # already served); overlapping would double-declare the same names.
            zone = self.zone_domain_no_port.strip().lower().rstrip(".")
            if custom == zone or custom.endswith("." + zone) or zone.endswith("." + custom):
                raise ValueError(
                    f"email_custom_domain {custom!r} overlaps the instance zone {zone!r}; "
                    "the built-in zone already handles that name"
                )

    # --- Instance identity, resolved per service ---
    # Every service authenticates with the same per-instance credential. Each service resolves it from its own
    # DEPRECATED per-service override first (kept so already-deployed configs keep working), then falls back to the
    # shared ``imbue_identity_*`` fields that new provisioning writes. cert-api additionally falls back to the shared
    # identity, and email additionally inherits cert-api's override — so an existing cert-api instance enables email by
    # setting only email_proxy_base_url, and a shared-identity instance satisfies both services from one credential.

    @property
    def cert_api_keycloak_issuer_url_resolved(self) -> str | None:
        return self.cert_api_keycloak_issuer_url or self.imbue_identity_issuer_url

    @property
    def cert_api_keycloak_client_id_resolved(self) -> str | None:
        return self.cert_api_keycloak_client_id or self.imbue_identity_client_id

    @property
    def cert_api_keycloak_client_secret_resolved(self) -> str | None:
        return self.cert_api_keycloak_client_secret or self.imbue_identity_client_secret

    @property
    def email_keycloak_issuer_url_resolved(self) -> str | None:
        return self.email_keycloak_issuer_url or self.cert_api_keycloak_issuer_url_resolved

    @property
    def email_keycloak_client_id_resolved(self) -> str | None:
        return self.email_keycloak_client_id or self.cert_api_keycloak_client_id_resolved

    @property
    def email_keycloak_client_secret_resolved(self) -> str | None:
        return self.email_keycloak_client_secret or self.cert_api_keycloak_client_secret_resolved

    @property
    def imbue_connect_base_url(self) -> str | None:
        """Base URL of the Imbue front door for the "Connect to Imbue" flow.

        The same front door the email API lives behind, so it reuses
        ``email_proxy_base_url`` (e.g. "https://openhost.imbue.com"). Returns None
        when no Imbue URL is configured, in which case the connect flow is
        unavailable and the Settings button is hidden.
        """
        return self.email_proxy_base_url

    @property
    def instance_identity(self) -> KeycloakClientCredentials | None:
        """The shared per-instance credential, or None when no identity is configured.

        Resolves through the cert-api override for backward compatibility, so an
        instance provisioned before the shared field existed still yields a
        credential. Returns None (rather than a partial object) unless all three
        parts resolve, so callers can treat None as "no Imbue identity configured".
        """
        issuer = self.cert_api_keycloak_issuer_url_resolved
        client_id = self.cert_api_keycloak_client_id_resolved
        client_secret = self.cert_api_keycloak_client_secret_resolved
        if issuer and client_id and client_secret:
            return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=client_secret)
        return None

    @property
    def email_enabled(self) -> bool:
        """Whether email is active on this instance.

        Derived, not a stored flag: email is on iff all of ``_EMAIL_PREREQ_ATTRS``
        resolve — the proxy URL plus the Keycloak client-credentials (which fall
        back to the cert-api client). When they do, ``public_ip`` is also required
        (enforced in ``__attrs_post_init__``) for the direct-inbound MX/A records.

        Because the Keycloak creds inherit from cert-api, a cert-api instance
        enables email simply by having ``email_proxy_base_url`` set — so a
        freshly-provisioned instance "just has working email", and an existing
        instance is upgraded by injecting that single value. Environments without
        the email infra leave it unset and run without email.
        """
        return all(getattr(self, name) for name in _EMAIL_PREREQ_ATTRS)

    @property
    def zone_domain_no_port(self) -> str:
        return self.zone_domain.split(":")[0]

    def inbound_mail_host_for(self, domain: str) -> str:
        """The mail hostname whose A record the MX points at, for direct inbound.

        Uses ``mail.<domain>`` — a dedicated mail host under the served zone, so
        the apex A record is left untouched. If the domain is *already* a
        ``mail.`` host (common for delegated custom domains like
        ``mail.mydomain.com``), it is used as-is rather than doubled to
        ``mail.mail.mydomain.com``.
        """
        d = domain.strip().lower().rstrip(".")
        return d if d.startswith("mail.") else f"mail.{d}"

    @property
    def email_custom_domain_normalized(self) -> str | None:
        """The custom mail domain lowercased and stripped of any trailing dot.

        Returns None when no custom domain is configured (or it is blank after
        normalization), so callers can treat "unset" and "blank" identically.
        """
        if not self.email_custom_domain:
            return None
        normalized = self.email_custom_domain.strip().lower().rstrip(".")
        return normalized or None

    def evolve(self, **kwargs: Any) -> Self:
        return attr.evolve(self, **kwargs)

    def _to_toml_dict(self) -> dict[str, dict[str, Any]]:
        return {"openhost": {k: v for k, v in attr.asdict(self).items() if v is not None}}

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
        section = d.get("openhost", d)
        return cattrs.structure(_drop_obsolete_keys(section), cls)

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
        # openhost-specific data, including the sqlite db and TLS certs.
        return Path(self.persistent_data_dir) / "openhost"

    @property
    def openhost_repo_path(self) -> Path:
        # compute_space/src/compute_space/config.py -> openhost repo root
        return Path(__file__).resolve().parent.parent.parent.parent

    @property
    def db_path(self) -> str:
        return str(self.openhost_data_path / "router.db")

    @property
    def tls_cert_path(self) -> Path:
        return self.openhost_data_path / "openhost-tls-cert.pem"

    @property
    def tls_key_path(self) -> Path:
        return self.openhost_data_path / "openhost-tls-key.pem"

    @property
    def coredns_corefile_path(self) -> Path:
        return self.openhost_data_path / "Corefile"

    @property
    def coredns_zonefile_path(self) -> Path:
        return self.openhost_data_path / "zonefile"

    @property
    def coredns_custom_zonefile_path(self) -> Path:
        """Zone file for the delegated custom mail domain (second authoritative zone)."""
        return self.openhost_data_path / "zonefile.custom"

    def custom_domain_delegation_record(self) -> DelegationRecord | None:
        """The single NS record the owner must add at their registrar to delegate
        their custom mail domain to this instance, or None if none is configured.

        The nameserver host lives under the instance's own zone (``ns.<zone>``),
        which already resolves to the instance's public IP, so this one record is
        all that is required.
        """
        custom = self.email_custom_domain_normalized
        if custom is None:
            return None
        return DelegationRecord(
            name=custom,
            record_type="NS",
            value=f"ns.{self.zone_domain_no_port}",
        )

    @property
    def caddyfile_path(self) -> Path:
        return self.openhost_data_path / "Caddyfile"

    @property
    def keys_dir(self) -> str:
        return str(Path(self.openhost_data_path) / "keys")

    @property
    def claim_token_path(self) -> str:
        return str(Path(self.openhost_data_path) / "claim_token")

    @property
    def default_apps_sentinel_path(self) -> str:
        return str(Path(self.openhost_data_path) / "default_apps.json")

    @property
    def effective_default_apps(self) -> list[str]:
        """The apps to auto-deploy: ``default_apps`` plus the email apps when
        email is enabled.

        The mailbox + webmail apps are only useful (and only correctly scoped for
        the relay-config endpoint) when email is on, so they are appended here
        rather than living in ``default_apps`` — an instance with email off ships
        no mailbox.  De-duplicated preserving order so an operator who already
        listed one of them in ``default_apps`` doesn't get it twice.
        """
        specs = list(self.default_apps)
        if self.email_enabled:
            for spec in self.email_default_apps:
                if spec not in specs:
                    specs.append(spec)
        return specs

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
    # needs set at runtime, no reasonable default value
    # zone_domain: str

    # Server
    host: str = "127.0.0.1"
    port: int = 8080

    # coredns (only truly needed if tls_enabled)
    coredns_enabled: bool = False
    public_ip: str | None = None

    # TLS
    tls_enabled: bool = False
    acquire_tls_cert_if_missing: bool = False
    acme_email: str | None = None
    acme_account_key_path: str | None = None
    acme_directory_url: str | None = None

    # Shared per-instance Imbue credential — injected by provisioning (managed spaces) or obtained via the
    # "Connect to Imbue" flow (non-managed). No safe default; absent means dependent services stay off.
    imbue_identity_issuer_url: str | None = None
    imbue_identity_client_id: str | None = None
    imbue_identity_client_secret: str | None = None

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

    # Email — no on/off flag; enabled automatically when its prerequisites (the
    # proxy URL + keycloak client + public_ip) are present. Provisioning injects
    # them when the operator has email infra configured.
    email_proxy_base_url: str | None = None
    email_keycloak_issuer_url: str | None = None
    email_keycloak_client_id: str | None = None
    email_keycloak_client_secret: str | None = None
    email_dmarc_rua: str | None = None
    email_custom_domain: str | None = None
    email_mailbox_app_names: list[str] = attr.Factory(lambda: ["stalwart-email-server"])
    # The mailbox server (Stalwart) + webmail client (Bulwark) that give the
    # instance a real inbox/outbox.  Deployed only when email is enabled (see
    # effective_default_apps).  Stalwart's manifest name must stay in
    # email_mailbox_app_names for it to be allowed to fetch the relay config.
    email_default_apps: list[str] = attr.Factory(
        lambda: [
            "https://github.com/imbue-openhost/openhost-stalwart-email-server",
            "https://github.com/imbue-openhost/openhost-bulwark-email-client",
        ]
    )

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
            "https://github.com/imbue-openhost/secrets",
            "https://github.com/imbue-openhost/openhost-filestash",
            "oauth_provider",
            "https://github.com/imbue-openhost/openhost-catalog",
            "https://github.com/imbue-openhost/openhost-backup",
            "https://github.com/imbue-openhost/openhost-community-chat",
        ]
    )


def active_config_path() -> str | None:
    """The config.toml path the instance loads, or None when config is env-driven.

    Prefers ``OPENHOST_ROUTER_CONFIG`` (new CLI name) and falls back to
    ``OPENHOST_CONFIG`` for backward compatibility. Shared by ``load_config`` and
    the runtime credential-persistence path so the precedence rule lives once.
    """
    return os.environ.get("OPENHOST_ROUTER_CONFIG") or os.environ.get("OPENHOST_CONFIG")


def load_config() -> Config:
    """Load config from OPENHOST_ prefixed env vars, env-selected TOML file, or default config, in that order.

    Prefer ``OPENHOST_ROUTER_CONFIG`` (new CLI name) and fall back to
    ``OPENHOST_CONFIG`` for backward compatibility.
    """
    path = active_config_path()
    if not path:
        return typed_settings.load(DefaultConfig, appname="openhost")
    scrubbed = _scrub_obsolete_keys_to_temp(path)
    try:
        return typed_settings.load(DefaultConfig, appname="openhost", config_files=[scrubbed])
    finally:
        # Delete the temp copy (if we made one) so it doesn't linger in /tmp —
        # it may carry secrets like email_keycloak_client_secret.
        if scrubbed != path:
            try:
                os.unlink(scrubbed)
            except OSError:
                pass


def _scrub_obsolete_keys_to_temp(path: str) -> str:
    """If ``path`` contains obsolete config keys, write a cleaned copy and return
    its path; otherwise return ``path`` unchanged.

    typed-settings rejects unknown keys, so a config.toml written by an older
    deploy (still carrying removed fields) would fail to load. We strip those
    keys before handing the file to typed-settings, without modifying the file on
    disk (ansible owns that; it will re-render on the next config change).

    The caller is responsible for deleting the returned path when it differs from
    the input (it is a temp file that may carry secrets).
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # Let typed-settings surface the real error with its own messaging.
        return path
    section = data.get("openhost")
    if not isinstance(section, dict) or not (_OBSOLETE_CONFIG_KEYS & section.keys()):
        return path
    data["openhost"] = _drop_obsolete_keys(section)
    fd, tmp_path = tempfile.mkstemp(prefix="openhost-config-", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
    except Exception:
        # Don't leave a (possibly secret-bearing) temp file behind if writing
        # fails — the caller only unlinks paths it receives, and it won't get
        # this one when we raise.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


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
