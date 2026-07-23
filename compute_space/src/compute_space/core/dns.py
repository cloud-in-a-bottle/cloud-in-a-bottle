"""Manage the VM-side CoreDNS zone file.

The VM runs CoreDNS authoritative for its zone domain (e.g. alice.host.imbue.com).
This module writes and updates the zone file. CoreDNS watches for SOA serial
changes and auto-reloads.

For DNS-01 ACME challenges, the router calls append_txt_records() to add TXT
records, waits for CoreDNS to pick them up, then calls clear_txt() after the cert
is issued.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import attr
from jinja2 import Environment
from jinja2 import FileSystemLoader

from compute_space.core.containers import CONTAINER_GATEWAY_IP
from compute_space.core.logging import logger

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR))

_SERIAL_RE = re.compile(r"^(\s+)(\d+)(\s+;\s*serial\s*)$", re.MULTILINE)

# Fallback upstream resolvers for the container-facing DNS view's catch-all
# forward block, used only if the host's own resolvers can't be discovered.
_FALLBACK_UPSTREAM_DNS = ("8.8.8.8", "1.1.1.1")


def _gateway_ip_is_bindable(gateway_ip: str) -> bool:
    """True if ``gateway_ip`` is a local address CoreDNS can bind.

    The ``openhost0`` dummy interface (10.200.0.1) only exists on
    ansible-provisioned hosts; in dev/CI it won't, and binding it would crash
    CoreDNS.  Probe a UDP bind (CoreDNS serves DNS on UDP) to decide.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind((gateway_ip, 0))
        probe.close()
        return True
    except OSError:
        return False


def _host_upstream_resolvers() -> list[str]:
    """Discover the host's real upstream resolvers for the container DNS view.

    The container-facing CoreDNS view forwards non-zone queries upstream.  We
    can't forward to the host's 127.0.0.53 stub (unreachable from the container
    netns) nor loop back to ourselves, so read concrete nameservers from
    /etc/resolv.conf, dropping loopback/stub and our own gateway address.
    """
    resolvers: list[str] = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    addr = parts[1]
                    if addr.startswith("127.") or addr == CONTAINER_GATEWAY_IP or addr == "::1":
                        continue
                    resolvers.append(addr)
    except OSError:
        pass
    return resolvers or list(_FALLBACK_UPSTREAM_DNS)


def _coredns_bind_ip(public_ip: str) -> str:
    """Return the local address CoreDNS should bind for authoritative DNS.

    Binding wildcard :53 conflicts with Podman's aardvark-dns on 10.89.0.1:53.
    Binding the configured public IP works on hosts where that IP is assigned to
    an interface (for example Hetzner), but fails on AWS/GCP where public IPs are
    NATed to a private VM address. The default-route source address is the local
    interface address that receives that NATed traffic.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return public_ip


def start_coredns(
    zone_domain: str,
    public_ip: str,
    corefile_path: Path,
    zonefile_path: Path,
    container_gateway_ip: str | None = CONTAINER_GATEWAY_IP,
    coredns_bin: str = "coredns",
    custom_zone_domain: str | None = None,
    custom_zonefile_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Write CoreDNS config + zone file, start CoreDNS, return the process.

    When ``container_gateway_ip`` is set (the default, and the dummy
    ``openhost0`` gateway in production), a second server view is bound there
    that resolves the zone wildcard to the gateway so pasta app containers can
    reach sibling apps' public HTTPS URLs through Caddy (NAT hairpin), with a
    catch-all forward for everything else.  Pass ``None`` to disable (e.g. in
    environments without the gateway interface).

    When ``custom_zone_domain`` is set (the owner's delegated custom mail domain),
    an additional authoritative zone is served for it from ``custom_zonefile_path``.
    The email records for the custom zone are appended later by
    ``apply_email_records`` (same as the primary zone), once the SES identity's
    DKIM tokens are known.
    """

    bind_serial = int(time.time())
    if custom_zone_domain and custom_zonefile_path is None:
        raise ValueError("custom_zonefile_path is required when custom_zone_domain is set")

    # Only emit the container-facing view when the gateway IP is actually
    # bindable (the openhost0 dummy interface exists in production but not in
    # dev/CI), otherwise CoreDNS would fail to start.
    if container_gateway_ip and not _gateway_ip_is_bindable(container_gateway_ip):
        logger.info("Container gateway %s not bindable; skipping container-facing DNS view", container_gateway_ip)
        container_gateway_ip = None

    # Container-facing zone file: the wildcard points at the gateway IP.  Lives
    # next to the public zone file with a `.container` suffix.
    container_zonefile_path = zonefile_path.with_name(zonefile_path.name + ".container")

    # Write Corefile. this is coredns's config.
    corefile = _jinja_env.get_template("Corefile").render(
        zone_domain=zone_domain,
        bind_ip=_coredns_bind_ip(public_ip),
        zone_file_path=zonefile_path,
        container_gateway_ip=container_gateway_ip,
        container_zone_file_path=container_zonefile_path,
        upstream_dns=" ".join(_host_upstream_resolvers()),
        custom_zone_domain=custom_zone_domain,
        custom_zone_file_path=custom_zonefile_path,
    )
    with open(corefile_path, "w") as f:
        f.write(corefile)

    # Write zone file. this is the actual DNS data. CoreDNS watches for changes to this file and auto-reloads.
    content = _jinja_env.get_template("zonefile").render(
        zone_domain=zone_domain,
        public_ip=public_ip,
        # Use current timestamp as initial SOA serial. This is simple and ensures it's always increasing on each run.
        serial=bind_serial,
    )
    with open(zonefile_path, "w") as f:
        f.write(content)

    if container_gateway_ip:
        container_content = _jinja_env.get_template("zonefile_container").render(
            zone_domain=zone_domain,
            gateway_ip=container_gateway_ip,
            serial=bind_serial,
        )
        with open(container_zonefile_path, "w") as f:
            f.write(container_content)

    if custom_zone_domain:
        assert custom_zonefile_path is not None  # guarded at entry
        custom_content = _jinja_env.get_template("zonefile_custom").render(
            custom_zone_domain=custom_zone_domain,
            zone_domain=zone_domain,
            public_ip=public_ip,
            serial=bind_serial,
        )
        with open(custom_zonefile_path, "w") as f:
            f.write(custom_content)
        logger.info(f"Serving delegated custom mail domain {custom_zone_domain}")

    logger.info(f"Starting CoreDNS for {zone_domain}")
    proc = subprocess.Popen(
        [coredns_bin, "-conf", corefile_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _stream_coredns_logs(proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(f"[coredns] {line.decode(errors='replace').rstrip()}")
        proc.wait()
        logger.warning(f"CoreDNS exited with code {proc.returncode}")

    threading.Thread(target=_stream_coredns_logs, args=(proc,), daemon=True).start()
    logger.info(f"Started CoreDNS for {zone_domain} (pid {proc.pid})")
    return proc


def _bump_serial(content: str) -> str:
    """Read the current SOA serial from the zone file content and increment it.

    Incrementing the SOA serial is necessary to trigger CoreDNS to reload the zone file.
    """
    m = _SERIAL_RE.search(content)
    if not m:
        raise ValueError("Could not find SOA serial in zone file")
    new_serial = int(m.group(2)) + 1
    return _SERIAL_RE.sub(f"{m.group(1)}{new_serial}{m.group(3)}", content, count=1)


@attr.s(auto_attribs=True, frozen=True)
class TxtRecord:
    """A TXT record to publish.

    ``record_name`` is written into the zone file verbatim, so the caller chooses
    the addressing: a name relative to the zone's $ORIGIN (e.g. '_acme-challenge')
    has CoreDNS append the origin, while an absolute FQDN ending in '.' is honored
    as-is.
    """

    record_name: str
    record_value: str


@attr.s(auto_attribs=True, frozen=True)
class DkimCname:
    """One DKIM CNAME record SES requires the zone to publish.

    ``name`` and ``target`` are absolute (SES returns fully-qualified names), so
    both are written as FQDNs (trailing dot) into the zone file.
    """

    name: str
    target: str


def render_email_records(
    zone_domain: str,
    *,
    mail_from_host: str,
    dkim_cnames: list[DkimCname],
    dmarc_rua: str | None = None,
    inbound_mail_host: str | None = None,
    inbound_mail_ip: str | None = None,
) -> str:
    """Render the persistent email DNS records for a zone as zone-file lines.

    Produces SPF (apex TXT authorizing SES — outbound always relays via SES), a
    DMARC policy (_dmarc TXT), the MX record, and the DKIM CNAMEs SES requires.
    These are deterministic given the inputs, so they can be re-applied
    idempotently on every boot (the zone file is regenerated from template at
    start_coredns time).

    Inbound (the MX target) depends on the mode:

    * **SES inbound** (``inbound_mail_host`` is None): MX points at the
      SES-managed inbound host (``mail_from_host``). Used where the instance can't
      accept inbound SMTP directly.
    * **Direct inbound** (``inbound_mail_host`` + ``inbound_mail_ip`` given): MX
      points at the instance's own mail host (e.g. ``mail.<zone>``) and an A
      record is published for that host -> the instance IP, so the instance's own
      mail server receives mail on port 25. Only outbound still goes through SES.
    """
    lines: list[str] = ["; --- openhost email records (managed) ---"]
    # SPF: authorize Amazon SES to send for this domain (outbound is always SES).
    lines.append('@   IN TXT  "v=spf1 include:amazonses.com ~all"')
    # DMARC: a conservative default policy. rua is optional aggregate-report addr.
    dmarc = "v=DMARC1; p=quarantine"
    if dmarc_rua:
        dmarc += f"; rua=mailto:{dmarc_rua}"
    lines.append(f'_dmarc   IN TXT  "{dmarc}"')
    # MX: direct-to-instance when a mail host is given, else the SES inbound host.
    if inbound_mail_host is not None:
        if not inbound_mail_ip:
            raise ValueError("inbound_mail_host requires inbound_mail_ip for the A record")
        host = inbound_mail_host.rstrip(".")
        lines.append(f"@   IN MX   10 {host}.")
        # Publish the mail host's A record so the MX resolves to the instance.
        lines.append(f"{host}.   IN A   {inbound_mail_ip}")
    else:
        lines.append(f"@   IN MX   10 {mail_from_host.rstrip('.')}.")
    # DKIM: SES CNAMEs (absolute names).
    for c in dkim_cnames:
        lines.append(f"{c.name.rstrip('.')}.   IN CNAME  {c.target.rstrip('.')}.")
    lines.append("; --- end openhost email records ---")
    return "\n".join(lines) + "\n"


def apply_email_records(
    zone_file_path: Path,
    zone_domain: str,
    *,
    mail_from_host: str,
    dkim_cnames: list[DkimCname],
    dmarc_rua: str | None = None,
    inbound_mail_host: str | None = None,
    inbound_mail_ip: str | None = None,
) -> None:
    """Append the persistent email records to the zone file and bump the serial.

    Intended to be called once after ``start_coredns`` on each boot, since the
    zone file is regenerated from template there. Appending (rather than
    templating) keeps the DKIM tokens — which are only known after the SES
    identity is created at provision time — out of the boot-time template.
    """
    block = render_email_records(
        zone_domain,
        mail_from_host=mail_from_host,
        dkim_cnames=dkim_cnames,
        dmarc_rua=dmarc_rua,
        inbound_mail_host=inbound_mail_host,
        inbound_mail_ip=inbound_mail_ip,
    )
    with open(zone_file_path) as f:
        content = f.read()
    content = _bump_serial(content)
    content = content.rstrip("\n") + "\n" + block
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Applied {len(dkim_cnames)} DKIM CNAME(s) + SPF/DMARC/MX to {zone_file_path}")


def append_txt_records(zone_file_path: Path, records: list[TxtRecord]) -> None:
    """Append TXT record(s) to the zone file and bump the SOA serial.

    Each record's ``record_name`` is written verbatim (see TxtRecord), so this
    serves both local DNS-01 challenges (relative '_acme-challenge' names) and
    openhost-cert-api broker challenges (absolute FQDNs). Bumping the SOA serial
    triggers a CoreDNS reload.
    """
    with open(zone_file_path) as f:
        content = f.read()
    content = _bump_serial(content)
    for record in records:
        content += f'{record.record_name}   IN TXT  "{record.record_value}"\n'
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Appended {len(records)} TXT record(s)")


# ACME DNS-01 challenge TXT records are always published at this owner name
# (relative to the zone $ORIGIN, or as the absolute FQDN). clear_txt scopes its
# removal to these so it never deletes persistent email TXT records (SPF at the
# apex, DMARC at _dmarc) that share the "IN TXT" shape.
_ACME_CHALLENGE_LABEL = "_acme-challenge"


def _is_acme_challenge_txt(line: str) -> bool:
    """True iff ``line`` is an ACME-challenge TXT record.

    Matches the owner name this module writes challenges under, whether relative
    (``_acme-challenge``) or absolute (``_acme-challenge.<zone>.``). Only lines
    that are TXT records AND owned by that name are considered challenges.
    """
    if "IN TXT" not in line:
        return False
    owner = line.split(None, 1)[0] if line.split() else ""
    return owner == _ACME_CHALLENGE_LABEL or owner.startswith(_ACME_CHALLENGE_LABEL + ".")


def clear_txt(zone_file_path: Path) -> None:
    """Remove ACME-challenge TXT records from the zone file and bump the SOA serial.

    Only ``_acme-challenge`` TXT records are removed; persistent email records
    (SPF at the apex, DMARC at ``_dmarc``, DKIM CNAMEs) are left intact so a cert
    renewal never disturbs mail deliverability.
    """
    with open(zone_file_path) as f:
        content = f.read()
    lines = [line for line in content.splitlines() if not _is_acme_challenge_txt(line)]
    content = _bump_serial("\n".join(lines) + "\n")
    with open(zone_file_path, "w") as f:
        f.write(content)
    logger.info(f"Cleared ACME-challenge TXT records from {zone_file_path}")
