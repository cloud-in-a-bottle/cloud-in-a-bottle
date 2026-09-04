import asyncio
import datetime
import json
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

from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.logging import logger
from compute_space.core.tls import dns_challenge


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


async def _acquire_cert_dns01(
    domains: list[str],
    directory_url: str,
    dns_provider: InternalDnsProvider,
    account_key: JWKRSA,
    verify_ssl: bool = True,
    acme_email: str | None = None,
) -> tuple[bytes, bytes]:
    """Acquire a cert via DNS-01, publishing the challenge records through the DNS provider."""
    tls_key = _generate_tls_key()

    logger.info(f"DNS-01: connecting to ACME directory {directory_url}")
    net = client.ClientNetwork(
        account_key,
        user_agent="openhost-router/0.1",
        timeout=30,
        verify_ssl=verify_ssl,
    )
    directory = messages.Directory.from_json((await asyncio.to_thread(net.get, directory_url)).json())
    acme_client = client.ClientV2(directory, net)

    logger.info("DNS-01: looking up existing account")
    try:
        reg = messages.NewRegistration(only_return_existing=True)
        account = await asyncio.to_thread(lambda: acme_client.query_registration(acme_client.new_account(reg)))
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
                account = await asyncio.to_thread(acme_client.new_account, reg)
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
            order = await asyncio.to_thread(acme_client.new_order, csr_pem)
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
            challenge_published = False
            acquisition_succeeded = False
            try:
                if pending_challenges:
                    # Publish every challenge value at once.  For a wildcard cert the base domain
                    # and *.domain are separate authorizations that both need a TXT record live at
                    # the same time.
                    logger.info(f"Setting {len(validation_values)} DNS-01 challenge TXT record(s)")
                    dns_challenge.publish(dns_provider, validation_values)
                    challenge_published = True

                    # Wait until an external resolver can see the records before telling the ACME
                    # server to validate.  Without this the CA's resolvers may get NXDOMAIN — the
                    # zone file reload hasn't happened yet, or the NS delegation from the parent
                    # zone hasn't propagated.
                    await dns_challenge.wait_until_visible(domains[0], validation_values)

                    # Now answer all challenges
                    for challenge_body in pending_challenges:
                        await asyncio.to_thread(
                            acme_client.answer_challenge, challenge_body, challenge_body.response(account_key)
                        )

                deadline = datetime.datetime.now() + datetime.timedelta(seconds=120)
                while datetime.datetime.now() < deadline:
                    order = await asyncio.to_thread(acme_client.poll_and_finalize, order, deadline)
                    if order.fullchain_pem:
                        break
                    await asyncio.sleep(2)

                if not order.fullchain_pem:
                    raise RuntimeError(f"Failed to get cert for {domains}: order not finalized")

                result = _extract_cert_and_key(order, tls_key)
                acquisition_succeeded = True
                return result
            finally:
                # In a `finally` so a cancellation between publishing and finalizing still takes
                # the tokens back down; only if we actually published, so a failure before that
                # doesn't delete another run's records.
                if challenge_published:
                    try:
                        dns_challenge.clear(dns_provider)
                    except Exception:
                        if acquisition_succeeded:
                            raise
                        logger.exception("Failed to clear DNS challenge records while handling acquisition failure")

        except (errors.ValidationError, RuntimeError) as exc:
            if attempt < max_attempts:
                wait = 30 * attempt
                logger.warning(
                    f"ACME validation failed (attempt {attempt}/{max_attempts}): {exc}. Retrying in {wait}s..."
                )
                await asyncio.sleep(wait)
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
