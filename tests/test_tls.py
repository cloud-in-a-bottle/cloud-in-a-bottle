"""
TLS certificate acquisition tests using Pebble ACME server.

Starts CoreDNS and Pebble on the host, acquires a wildcard TLS cert via
DNS-01 ACME challenge, and verifies the result. Runs on bare metal — no VMs.

Prerequisites:
    - pebble binary in PATH
      (install: go install github.com/letsencrypt/pebble/v2/cmd/pebble@latest)
    - coredns binary in PATH
      (install via pixi, or: go install github.com/coredns/coredns@latest)

Run:
    pytest tests/test_tls.py -v -s --run-tls --timeout=300
"""

import asyncio
import datetime
import ipaddress
import json
import os
import signal
import socket
import subprocess

import pytest
from acme import client
from acme import messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from josepy import JWKRSA

from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.dns.router_records import publish_router_addresses
from compute_space.core.tls.acquire_cert import acquire_tls_cert
from compute_space.core.tls.util import _acquire_cert_dns01
from compute_space.core.tls.util import load_account_key
from compute_space.tests.utils import kill_tree
from compute_space.tests.utils import poll
from compute_space.tests.utils import port_connectable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PEBBLE_ACME_PORT = 14000
PEBBLE_MGMT_PORT = 15000
COREDNS_PORT = 15353
ZONE_DOMAIN = "tls-test.localhost"

requires_tls = pytest.mark.requires_tls

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _port_free(port):
    """Check that a TCP port is not already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _generate_pebble_certs(cert_dir):
    """Generate self-signed TLS certs for Pebble's own HTTPS endpoint."""
    ca_key = rsa_module.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Pebble Test CA")])
    now = datetime.datetime.now(datetime.UTC)
    leaf_key = rsa_module.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_path = os.path.join(cert_dir, "pebble-cert.pem")
    key_path = os.path.join(cert_dir, "pebble-key.pem")

    with open(cert_path, "wb") as f:
        f.write(leaf_cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

    return cert_path, key_path


def _generate_acme_account_key(path):
    """Generate an RSA key and save in certbot JWK JSON format."""
    private_key = rsa_module.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = JWKRSA(key=private_key)
    with open(path, "w") as f:
        json.dump(jwk.to_json(), f)
    return jwk


def _register_acme_account(directory_url, account_key):
    """Register an ACME account with Pebble so the cert code can look it up."""
    net = client.ClientNetwork(account_key, user_agent="openhost-test/0.1", verify_ssl=False)
    directory = messages.Directory.from_json(net.get(directory_url).json())
    acme_client = client.ClientV2(directory, net)
    reg = messages.NewRegistration(
        contact=("mailto:test@example.com",),
        terms_of_service_agreed=True,
    )
    acme_client.new_account(reg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tls_tmpdir(tmp_path_factory):
    """Shared temp dir for all TLS test artifacts."""
    return tmp_path_factory.mktemp("tls_test")


@pytest.fixture(scope="module")
def pebble_certs(tls_tmpdir):
    """Generate self-signed certs for Pebble's HTTPS endpoint."""
    cert_dir = str(tls_tmpdir / "pebble_certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_path, key_path = _generate_pebble_certs(cert_dir)
    return {"cert": cert_path, "key": key_path}


@pytest.fixture(scope="module")
def dns_provider(tls_tmpdir):
    """A real provider rendering the zone file the test's CoreDNS serves, so cert acquisition
    drives DNS through exactly the path production uses.

    Never started: the test runs its own CoreDNS on a non-privileged port, so the provider is only
    here to write records into the zone file.
    """
    provider = InternalDnsProvider(
        corefile_path=tls_tmpdir / "provider-Corefile",  # unused; the test writes its own
        zones_dir=tls_tmpdir / "zones",
        bind_ip="127.0.0.1",
        zones=(ZONE_DOMAIN,),
    )
    publish_router_addresses(provider, "127.0.0.1")
    return provider


@pytest.fixture(scope="module")
def zonefile_path(dns_provider):
    return dns_provider.zones_dir / f"{ZONE_DOMAIN}.zone"


@pytest.fixture(scope="module")
def coredns_server(tls_tmpdir, zonefile_path):
    """Start CoreDNS on a non-privileged port, serving the test zone."""
    assert _port_free(COREDNS_PORT), f"Port {COREDNS_PORT} already in use"

    corefile_path = tls_tmpdir / "Corefile"
    corefile_content = (
        f"{ZONE_DOMAIN}:{COREDNS_PORT} {{\n"
        f"    file {zonefile_path} {{\n"
        f"        reload 2s\n"
        f"    }}\n"
        f"    log\n"
        f"    errors\n"
        f"}}\n"
    )
    with open(corefile_path, "w") as f:
        f.write(corefile_content)

    log_path = tls_tmpdir / "coredns.log"
    log_file = open(log_path, "w")

    proc = subprocess.Popen(
        ["coredns", "-conf", str(corefile_path)],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    try:
        poll(
            lambda: port_connectable("127.0.0.1", COREDNS_PORT),
            timeout=10,
            interval=0.3,
            fail_msg="CoreDNS did not start",
        )
        yield proc
    except Exception as exc:
        with open(log_path) as f:
            raise RuntimeError(f"CoreDNS failed to start:\n{f.read()}") from exc
    finally:
        kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(proc, signal.SIGKILL)
            proc.wait(timeout=5)
        log_file.close()


@pytest.fixture(scope="module")
def pebble_server(tls_tmpdir, pebble_certs, coredns_server):
    """Start Pebble ACME test server, pointed at the test CoreDNS."""
    assert _port_free(PEBBLE_ACME_PORT), f"Port {PEBBLE_ACME_PORT} already in use"

    config_path = str(tls_tmpdir / "pebble-config.json")
    with open(config_path, "w") as f:
        json.dump(
            {
                "pebble": {
                    "listenAddress": f"0.0.0.0:{PEBBLE_ACME_PORT}",
                    "managementListenAddress": f"0.0.0.0:{PEBBLE_MGMT_PORT}",
                    "certificate": pebble_certs["cert"],
                    "privateKey": pebble_certs["key"],
                    "httpPort": 5002,
                    "tlsPort": 5001,
                }
            },
            f,
        )

    env = os.environ.copy()
    env["PEBBLE_VA_NOSLEEP"] = "1"
    env["PEBBLE_WFE_NONCEREJECT"] = "0"

    log_path = tls_tmpdir / "pebble.log"
    log_file = open(log_path, "w")

    proc = subprocess.Popen(
        [
            "pebble",
            "-config",
            config_path,
            "-dnsserver",
            f"127.0.0.1:{COREDNS_PORT}",
        ],
        env=env,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    directory_url = f"https://127.0.0.1:{PEBBLE_ACME_PORT}/dir"

    try:
        poll(
            lambda: port_connectable("127.0.0.1", PEBBLE_ACME_PORT),
            timeout=10,
            interval=0.3,
            fail_msg="Pebble did not start",
        )
        yield {"proc": proc, "directory_url": directory_url}
    except Exception as exc:
        with open(log_path) as f:
            raise RuntimeError(f"Pebble failed to start:\n{f.read()}") from exc
    finally:
        kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(proc, signal.SIGKILL)
            proc.wait(timeout=5)
        log_file.close()


@pytest.fixture(scope="module")
def acme_account_key(tls_tmpdir, pebble_server):
    """Generate an ACME account key and register it with Pebble."""
    key_path = tls_tmpdir / "acme_account_key.json"
    jwk = _generate_acme_account_key(str(key_path))
    _register_acme_account(pebble_server["directory_url"], jwk)
    return {"jwk": jwk, "path": key_path}


@pytest.fixture(scope="module")
def acquired_cert(pebble_server, acme_account_key, dns_provider):
    """Acquire a wildcard cert via DNS-01 — reused by multiple tests."""
    domains = [ZONE_DOMAIN, f"*.{ZONE_DOMAIN}"]
    # The fixture is sync and owns no loop, so asyncio.run belongs here.
    cert_pem, key_pem = asyncio.run(
        _acquire_cert_dns01(
            domains=domains,
            directory_url=pebble_server["directory_url"],
            dns_provider=dns_provider,
            account_key=acme_account_key["jwk"],
            verify_ssl=False,
        )
    )
    return {"cert_pem": cert_pem, "key_pem": key_pem, "domains": domains}


# ---------------------------------------------------------------------------
# Tests — Cert Acquisition (DNS-01 via Pebble)
# ---------------------------------------------------------------------------


@requires_tls
class TestCertAcquisition:
    """Test DNS-01 ACME cert acquisition end-to-end with Pebble."""

    def test_cert_acquired(self, acquired_cert):
        """_acquire_cert_dns01 returns PEM cert and key."""
        assert acquired_cert["cert_pem"]
        assert acquired_cert["key_pem"]
        assert b"BEGIN CERTIFICATE" in acquired_cert["cert_pem"]
        assert b"BEGIN EC PRIVATE KEY" in acquired_cert["key_pem"]

    def test_cert_covers_base_domain(self, acquired_cert):
        """Cert SAN includes the base domain."""
        cert = x509.load_pem_x509_certificate(acquired_cert["cert_pem"])
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert ZONE_DOMAIN in dns_names

    def test_cert_covers_wildcard(self, acquired_cert):
        """Cert SAN includes the wildcard for app subdomains."""
        cert = x509.load_pem_x509_certificate(acquired_cert["cert_pem"])
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert f"*.{ZONE_DOMAIN}" in dns_names

    def test_cert_uses_ecdsa_p256(self, acquired_cert):
        """TLS key is ECDSA P-256 (fast handshakes)."""
        key = serialization.load_pem_private_key(acquired_cert["key_pem"], password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_cert_key_match(self, acquired_cert):
        """Cert's public key matches the private key."""
        cert = x509.load_pem_x509_certificate(acquired_cert["cert_pem"])
        key = serialization.load_pem_private_key(acquired_cert["key_pem"], password=None)

        cert_pub = cert.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_pub = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        assert cert_pub == key_pub

    def test_cert_not_expired(self, acquired_cert):
        """Cert is currently valid (not before <= now <= not after)."""
        cert = x509.load_pem_x509_certificate(acquired_cert["cert_pem"])
        now = datetime.datetime.now(datetime.UTC)
        assert cert.not_valid_before_utc <= now
        assert cert.not_valid_after_utc > now

    def test_dns_txt_records_cleaned_up(self, acquired_cert, zonefile_path):
        """After cert acquisition, ACME TXT records are removed from the zone file."""
        with open(zonefile_path) as f:
            content = f.read()
        assert "IN TXT" not in content


# ---------------------------------------------------------------------------
# Tests — acquire_tls_cert (high-level public API)
# ---------------------------------------------------------------------------


@requires_tls
class TestAcquireTlsCert:
    """Test the public acquire_tls_cert function that writes cert files to disk."""

    def test_acquire_writes_cert_files(self, pebble_server, acme_account_key, dns_provider, tls_tmpdir):
        """acquire_tls_cert writes cert and key PEM files with correct permissions."""
        cert_path = tls_tmpdir / "acquired-cert.pem"
        key_path = tls_tmpdir / "acquired-key.pem"

        asyncio.run(
            acquire_tls_cert(
                domain=ZONE_DOMAIN,
                cert_path=cert_path,
                key_path=key_path,
                acme_account_key_path=acme_account_key["path"],
                dns_provider=dns_provider,
                directory_url=pebble_server["directory_url"],
                verify_ssl=False,
            )
        )

        assert cert_path.exists()
        assert key_path.exists()

        # Key file should have restricted permissions (0600)
        assert oct(key_path.stat().st_mode & 0o777) == "0o600"

        # Cert should be a valid PEM covering our domain
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert ZONE_DOMAIN in dns_names
        assert f"*.{ZONE_DOMAIN}" in dns_names


# ---------------------------------------------------------------------------
# Tests — Account Key Helpers
# ---------------------------------------------------------------------------


@requires_tls
class TestAccountKey:
    """Test ACME account key generation and loading."""

    def test_load_generated_key(self, acme_account_key):
        """A generated key can be loaded by the production load_account_key function."""
        loaded = load_account_key(acme_account_key["path"])
        assert loaded is not None

    def test_round_trip_key(self, tls_tmpdir):
        """Generate, save, and reload an account key — the JWK should be identical."""
        key_path = tls_tmpdir / "roundtrip_key.json"
        original = _generate_acme_account_key(str(key_path))
        loaded = load_account_key(key_path)
        assert original.to_json() == loaded.to_json()
