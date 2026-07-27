from __future__ import annotations

import socket
from pathlib import Path

import pytest

import compute_space.core.dns as dns_mod
from compute_space.core.dns import DkimCname
from compute_space.core.dns import TxtRecord
from compute_space.core.dns import append_txt_records
from compute_space.core.dns import apply_email_records
from compute_space.core.dns import clear_acme_challenge_records


def _write_zonefile(path: Path, serial: int = 100) -> None:
    path.write_text(
        "$ORIGIN app.example.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.app.example.com. admin.app.example.com. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.app.example.com.\n"
        "@   IN A    127.0.0.1\n"
    )


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr

    def getsockname(self) -> tuple[str, int]:
        return ("10.0.0.5", 12345)


def test_coredns_bind_ip_uses_default_route_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(dns_mod.socket, "socket", lambda *args: fake_socket)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "10.0.0.5"
    assert fake_socket.addr == ("8.8.8.8", 80)


def test_coredns_bind_ip_falls_back_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(*args: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", raise_os_error)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "203.0.113.10"


def test_append_txt_records_writes_relative_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)

    # Local DNS-01 path: several values share one relative name, left for CoreDNS
    # to resolve against $ORIGIN.
    append_txt_records(
        zonefile,
        [
            TxtRecord(record_name="_acme-challenge", record_value="base-value"),
            TxtRecord(record_name="_acme-challenge", record_value="wildcard-value"),
        ],
    )

    content = zonefile.read_text()
    assert '_acme-challenge   IN TXT  "base-value"' in content
    assert '_acme-challenge   IN TXT  "wildcard-value"' in content
    # Relative name is not turned into an absolute FQDN.
    assert "_acme-challenge.   IN TXT" not in content
    # Serial bumped so CoreDNS reloads.
    assert "101   ; serial" in content


def test_append_txt_records_writes_absolute_fqdn_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    # Broker path: names arrive as absolute FQDNs (trailing dot) so CoreDNS does
    # not re-append $ORIGIN.
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    content = zonefile.read_text()
    assert '_acme-challenge.app.example.com.   IN TXT  "v"' in content
    # Not doubled up into _acme-challenge.app.example.com.app.example.com.
    assert "app.example.com.app.example.com" not in content


def test_clear_acme_challenge_records_removes_acme_records(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    clear_acme_challenge_records(zonefile)

    assert "IN TXT" not in zonefile.read_text()


def test_clear_acme_challenge_records_preserves_email_txt_records(tmp_path: Path) -> None:
    # clear_acme_challenge_records runs on every cert renewal; it must remove ACME challenges but
    # NOT the persistent SPF/DMARC TXT records, or mail would break on renewal.
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    apply_email_records(
        zonefile,
        "app.example.com",
        inbound_mail_host="mail.app.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[DkimCname(name="tok1._domainkey.app.example.com", target="tok1.dkim.amazonses.com")],
        dmarc_rua="dmarc@app.example.com",
    )
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge", record_value="challenge")])

    clear_acme_challenge_records(zonefile)

    content = zonefile.read_text()
    # ACME challenge gone...
    assert "challenge" not in content
    # ...but SPF, DMARC, MX, DKIM, and the mail-host A record survive.
    assert "v=spf1 include:amazonses.com" in content
    assert "v=DMARC1" in content
    assert "IN MX" in content
    assert "mail.app.example.com.   IN A   203.0.113.9" in content
    assert "tok1._domainkey.app.example.com.   IN CNAME  tok1.dkim.amazonses.com." in content


def test_apply_email_records_bumps_serial_and_is_appendable(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)
    apply_email_records(
        zonefile,
        "app.example.com",
        inbound_mail_host="mail.app.example.com",
        inbound_mail_ip="203.0.113.9",
        dkim_cnames=[
            DkimCname(name="a._domainkey.app.example.com", target="a.dkim.amazonses.com"),
            DkimCname(name="b._domainkey.app.example.com", target="b.dkim.amazonses.com"),
        ],
    )
    content = zonefile.read_text()
    assert "101   ; serial" in content
    assert '@   IN TXT  "v=spf1 include:amazonses.com ~all"' in content
    # MX points at the instance's own mail host, with a matching A record.
    assert "@   IN MX   10 mail.app.example.com." in content
    assert "mail.app.example.com.   IN A   203.0.113.9" in content
    assert "a._domainkey.app.example.com.   IN CNAME  a.dkim.amazonses.com." in content
    assert "b._domainkey.app.example.com.   IN CNAME  b.dkim.amazonses.com." in content
    # Default DMARC has no rua when none provided.
    assert '_dmarc   IN TXT  "v=DMARC1; p=quarantine"' in content


class _FakeProc:
    pid = 4242
    stdout = None

    def wait(self) -> int:
        return 0


def _stub_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Don't spawn the log-streaming thread (its target reads proc.stdout).
    monkeypatch.setattr(dns_mod.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())


def test_container_dns_view_rendered_when_gateway_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["9.9.9.9"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, zonefile, container_gateway_ip="10.200.0.1")

    cf = corefile.read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf

    # Public zonefile points at the public IP; container zonefile at the gateway.
    assert "203.0.113.10" in zonefile.read_text()
    container_zone = tmp_path / "zonefile.container"
    assert container_zone.exists()
    cz = container_zone.read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert "203.0.113.10" not in cz


def test_container_dns_view_skipped_when_gateway_not_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, zonefile, container_gateway_ip="10.200.0.1")

    cf = corefile.read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    # No container zonefile written.
    assert not (tmp_path / "zonefile.container").exists()


def test_custom_zone_served_as_second_authoritative_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # When a delegated custom mail domain is configured, CoreDNS serves it as a
    # second authoritative zone with its own zone file (SOA/NS/A -> instance IP).
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    custom_zonefile = tmp_path / "zonefile.custom"
    dns_mod.start_coredns(
        "app.example.com",
        "203.0.113.10",
        corefile,
        zonefile,
        container_gateway_ip=None,
        custom_zone_domain="mail.mydomain.com",
        custom_zonefile_path=custom_zonefile,
    )

    cf = corefile.read_text()
    # Both zones are declared as authoritative server blocks, bound to the same IP.
    assert "app.example.com:53 {" in cf
    assert "mail.mydomain.com:53 {" in cf

    assert custom_zonefile.exists()
    cz = custom_zonefile.read_text()
    assert "$ORIGIN mail.mydomain.com." in cz
    # NS host lives under the instance's own zone (so one NS record delegates it).
    assert "@   IN NS   ns.app.example.com." in cz
    # A/wildcard point at the instance public IP.
    assert "@   IN A    203.0.113.10" in cz
    assert "*   IN A    203.0.113.10" in cz


def test_custom_zone_not_served_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    dns_mod.start_coredns(
        "app.example.com", "203.0.113.10", corefile, tmp_path / "zonefile", container_gateway_ip=None
    )

    assert "mail.mydomain.com" not in corefile.read_text()
    assert not (tmp_path / "zonefile.custom").exists()


def test_custom_zone_requires_zonefile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_popen(monkeypatch)
    with pytest.raises(ValueError, match="custom_zonefile_path is required"):
        dns_mod.start_coredns(
            "app.example.com",
            "203.0.113.10",
            tmp_path / "Corefile",
            tmp_path / "zonefile",
            container_gateway_ip=None,
            custom_zone_domain="mail.mydomain.com",
        )


def test_host_upstream_resolvers_filters_loopback_and_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(
        "nameserver 127.0.0.53\n"
        f"nameserver {dns_mod.CONTAINER_GATEWAY_IP}\n"
        "nameserver 185.12.64.1\n"
        "nameserver 1.1.1.1\n"
        "search example.com\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == ["185.12.64.1", "1.1.1.1"]


def test_host_upstream_resolvers_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*a: object, **k: object) -> object:
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", raise_oserror)
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_host_upstream_resolvers_falls_back_when_only_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host using only the systemd-resolved stub (127.0.0.53) would leave the
    # container view with no forwardable upstream; we must fall back, never emit
    # an empty/loopback forward (which would be unreachable from the container).
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_container_view_forward_uses_discovered_resolvers_and_distinct_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The public view and the container view must bind different addresses (the
    # default-route source vs the gateway), and the container catch-all must
    # forward to the discovered upstreams.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["185.12.64.1", "1.1.1.1"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, tmp_path / "zonefile")
    cf = corefile.read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert "bind 10.200.0.1" in cf  # container view + catch-all
    assert "forward . 185.12.64.1 1.1.1.1" in cf
    # Catch-all is scoped to the container gateway only (never the public bind),
    # so the public IP is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert "bind 10.200.0.1" in catch_all
    assert "bind 10.0.0.5" not in catch_all
