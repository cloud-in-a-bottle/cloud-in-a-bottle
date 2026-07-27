import datetime
import json
import subprocess
import time
from pathlib import Path

from acme import challenges
from acme import client
from acme import errors
from acme import messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from josepy import JWKRSA  # type: ignore[attr-defined]

import compute_space.core.dns as dns_module
from compute_space.core.logging import logger


def load_account_key(path: Path) -> JWKRSA:
    """Load a pre-registered ACME account key from a certbot JWK JSON file."""
    with open(path) as f:
        jwk_data = json.load(f)
    return JWKRSA.from_json(jwk_data)  # type: ignore[return-value]


def _generate_tls_key() -> ec.EllipticCurvePrivateKey:
    """Generate an ephemeral TLS private key (ECDSA P-256)."""
    return ec.generate_private_key(ec.SECP256R1())


def _create_csr(private_key: ec.EllipticCurvePrivateKey, domains: str | list[str]) -> x509.CertificateSigningRequest:
    """Create a CSR for one or more domains."""
    if isinstance(domains, str):
        domains = [domains]
    san_names = [x509.DNSName(d) for d in domains]
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domains[0])]))
        .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
        .sign(private_key, hashes.SHA256())
    )


def _wait_for_txt_propagation(
    zone_domain: str,
    expected_values: list[str],
    timeout: float = 120,
    interval: float = 5,
    resolver: str = "8.8.8.8",
) -> bool:
    """Poll an external resolver until all expected TXT values are visible.

    Returns True if all values were found, False on timeout.  On timeout the
    caller should proceed anyway — the ACME retry loop is the fallback.
    """
    qname = f"_acme-challenge.{zone_domain}"
    deadline = time.monotonic() + timeout
    expected_set = set(expected_values)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["dig", f"@{resolver}", qname, "TXT", "+short", "+timeout=5", "+tries=1"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            found = set()
            for line in result.stdout.strip().splitlines():
                # dig +short TXT output looks like: "token-value-here"
                found.add(line.strip().strip('"'))
            if expected_set <= found:
                logger.info(f"DNS propagation confirmed: {qname} has all {len(expected_set)} expected TXT value(s)")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        remaining = deadline - time.monotonic()
        logger.info(f"Waiting for DNS propagation of {qname} ({remaining:.0f}s remaining)")
        time.sleep(interval)

    logger.warning(f"DNS propagation timeout: {qname} not fully visible after {timeout}s, proceeding anyway")
    return False


def _acquire_cert_dns01(
    domains: list[str],
    directory_url: str,
    coredns_zonefile_path: Path,
    account_key: JWKRSA,
    verify_ssl: bool = True,
    acme_email: str | None = None,
) -> tuple[bytes, bytes]:
    """Acquire cert via DNS-01 challenge by writing TXT records to the local zone file."""
    tls_key = _generate_tls_key()

    logger.info(f"DNS-01: connecting to ACME directory {directory_url}")
    net = client.ClientNetwork(
        account_key,
        user_agent="openhost-router/0.1",
        timeout=30,
        verify_ssl=verify_ssl,
    )
    directory = messages.Directory.from_json(net.get(directory_url).json())
    acme_client = client.ClientV2(directory, net)

    logger.info("DNS-01: looking up existing account")
    try:
        reg = messages.NewRegistration(only_return_existing=True)
        account = acme_client.query_registration(acme_client.new_account(reg))
    except errors.ConflictError as e:
        # Account exists but only_return_existing returns a conflict.
        account = messages.RegistrationResource(uri=e.location)
    except messages.Error as e:
        # Account doesn't exist for this key -- register a new one.
        if "accountDoesNotExist" in str(e):
            logger.info("DNS-01: no existing account for this key, registering new one")
            reg_kwargs: dict[str, object] = {"terms_of_service_agreed": True}
            if acme_email:
                reg_kwargs["contact"] = (f"mailto:{acme_email}",)
            reg = messages.NewRegistration(**reg_kwargs)
            try:
                account = acme_client.new_account(reg)
            except errors.ConflictError as ce:
                account = messages.RegistrationResource(uri=ce.location)
        else:
            raise
    acme_client.net.account = account
    logger.info(f"DNS-01: found account {account.uri}")

    # Retry loop: transient DNS errors (e.g. SERVFAIL during CAA lookup) can
    # cause ACME validation to fail.  On failure we create a fresh order since
    # the failed authorizations are not reusable.
    max_attempts = 3
    csr = _create_csr(tls_key, domains)
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"DNS-01: creating order for {domains} (attempt {attempt}/{max_attempts})")
            order = acme_client.new_order(csr_pem)
            logger.info(f"DNS-01: order created, status={order.body.status}")

            # Collect all DNS-01 challenge values first, then write them all at once.
            # For wildcard certs, both the base domain and *.domain create separate
            # authorizations that both need _acme-challenge TXT records simultaneously.
            logger.info(f"DNS-01: collecting challenges from {len(order.authorizations)} authorization(s)")
            pending_challenges = []
            validation_values = []
            for i, authz in enumerate(order.authorizations):
                logger.info(f"DNS-01: checking authz {i}: {authz.body.identifier} status={authz.body.status}")
                if authz.body.status == messages.STATUS_VALID:
                    continue
                for challenge_body in authz.body.challenges:
                    if isinstance(challenge_body.chall, challenges.DNS01):
                        if challenge_body.status != messages.STATUS_PENDING:
                            logger.info(f"DNS-01: challenge already {challenge_body.status}, skipping")
                            break
                        validation_values.append(challenge_body.validation(account_key))
                        pending_challenges.append(challenge_body)
                        break

            logger.info(f"DNS-01: {len(pending_challenges)} pending challenges to answer")
            if pending_challenges:
                # Write all TXT records to the zone file at once
                logger.info(f"Setting {len(validation_values)} DNS-01 challenge TXT record(s)")
                dns_module.append_txt_records(
                    coredns_zonefile_path,
                    [dns_module.TxtRecord(record_name="_acme-challenge", record_value=v) for v in validation_values],
                )

                # Wait for CoreDNS to pick up the zone file change (reload interval = 2s)
                time.sleep(3)

                # Wait until an external resolver can see our TXT records before
                # telling the ACME server to validate.  Without this, the ACME
                # server's resolvers may get NXDOMAIN if the NS delegation from
                # the parent zone hasn't propagated yet.
                zone_domain = domains[0].lstrip("*.")
                _wait_for_txt_propagation(zone_domain, validation_values)

                # Now answer all challenges
                for challenge_body in pending_challenges:
                    acme_client.answer_challenge(challenge_body, challenge_body.response(account_key))

            deadline = datetime.datetime.now() + datetime.timedelta(seconds=120)
            while datetime.datetime.now() < deadline:
                order = acme_client.poll_and_finalize(order, deadline=deadline)
                if order.fullchain_pem:
                    break
                time.sleep(2)

            # Clean up DNS record
            dns_module.clear_acme_challenge_records(coredns_zonefile_path)

            if not order.fullchain_pem:
                raise RuntimeError(f"Failed to get cert for {domains}: order not finalized")

            return _extract_cert_and_key(order, tls_key)

        except (errors.ValidationError, RuntimeError) as exc:
            # Clean up DNS records before retrying
            dns_module.clear_acme_challenge_records(coredns_zonefile_path)

            if attempt < max_attempts:
                wait = 30 * attempt
                logger.warning(
                    f"ACME validation failed (attempt {attempt}/{max_attempts}): {exc}. Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                logger.error(f"ACME cert acquisition failed after {max_attempts} attempts")
                raise

    # Unreachable: the loop always returns or re-raises on the last attempt.
    raise RuntimeError(f"Failed to get cert for {domains} after {max_attempts} attempts")


def tls_private_key_to_pem(tls_key: ec.EllipticCurvePrivateKey) -> bytes:
    """Serialize a TLS private key to unencrypted PEM bytes."""
    return tls_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _extract_cert_and_key(order: messages.OrderResource, tls_key: ec.EllipticCurvePrivateKey) -> tuple[bytes, bytes]:
    """Extract PEM cert and key from a finalized ACME order."""
    return order.fullchain_pem.encode(), tls_private_key_to_pem(tls_key)
