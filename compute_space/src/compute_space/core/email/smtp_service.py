"""Router-mediated outbound email: the ``email`` v2 service, spoken over SMTP.

Email is an OpenHost-provided service (not an app). Any app that wants to send
mail requests the ``email`` service's ``send`` grant in its manifest; the router
runs a local SMTP submission listener that the app relays through, and the router
attaches the real relay credential and forwards to the Imbue email proxy
smarthost. The app never sees that credential.

Auth: the app connects with SMTP AUTH LOGIN/PLAIN, username = its app name,
password = its ``OPENHOST_APP_TOKEN`` (the same per-app bearer used for HTTP
service calls; no new secret). The router validates the token, resolves the
app_id, and checks the app was granted the ``email`` service ``send`` permission
(``permissions_v2``) — i.e. permissioned exactly like every other service.

Relay: on an accepted message, the router fetches the per-instance relay
credential (``RelayCredentialProvider``, cached) and relays the raw message to
the Imbue smarthost over implicit TLS. The listener binds only on loopback + the
container gateway (``host.containers.internal``), never a public interface;
inbound mail is unaffected (still delivered directly to Stalwart on 25).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
import threading

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP
from aiosmtpd.smtp import AuthResult
from aiosmtpd.smtp import Envelope
from aiosmtpd.smtp import LoginPassword
from aiosmtpd.smtp import Session

from compute_space.config import Config
from compute_space.core.auth.auth import validate_app_token
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialError
from compute_space.core.email.relay_credential import get_relay_credential_provider
from compute_space.core.email.service import EMAIL_SERVICE_URL
from compute_space.core.email.service import ROUTER_SMTP_PORT
from compute_space.core.email.service import grants_allow_send
from compute_space.core.logging import logger
from compute_space.db.connection import get_db

__all__ = ["ROUTER_SMTP_PORT", "start_email_smtp_service", "EmailServiceAuthenticator", "EmailServiceHandler"]


def _authenticated_app_id(username: str, password: str) -> str | None:
    """Resolve the app whose token is ``password`` and whose name is ``username``.

    Returns the app_id on success, or None if the token is invalid or does not
    belong to the named app. A fresh DB connection is opened because this runs on
    the aiosmtpd event-loop thread, not a request thread.
    """
    db = get_db()
    try:
        authed = validate_app_token(password, db)
        if authed is None:
            return None
        row = db.execute("SELECT name FROM apps WHERE app_id = ?", (authed.app_id,)).fetchone()
        if row is None or row["name"] != username:
            # Token must match the claimed app name (defense in depth; the token
            # alone already identifies the app).
            return None
        return authed.app_id
    finally:
        db.close()


def _app_may_send(app_id: str) -> bool:
    """True iff ``app_id`` holds the email service ``send`` grant."""
    granted = get_granted_permissions_v2(app_id, EMAIL_SERVICE_URL)
    return grants_allow_send([g.grant for g in granted])


class EmailServiceAuthenticator:
    """aiosmtpd auth callback: authenticate the local app + check the send grant."""

    def __call__(
        self, server: SMTP, session: Session, envelope: Envelope, mechanism: str, auth_data: object
    ) -> AuthResult:
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        try:
            username = auth_data.login.decode("utf-8", "strict")
            password = auth_data.password.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return AuthResult(success=False, handled=False)
        app_id = _authenticated_app_id(username, password)
        if app_id is None:
            logger.info(f"email service: SMTP auth failed for app {username!r}")
            return AuthResult(success=False, handled=False)
        if not _app_may_send(app_id):
            logger.info(f"email service: app {username!r} lacks the email 'send' grant")
            return AuthResult(success=False, handled=False)
        # Bind the authenticated app_id to the session for the DATA handler.
        return AuthResult(success=True, auth_data=app_id)


class EmailServiceHandler:
    """aiosmtpd handler: relay an authenticated app's message to the Imbue smarthost."""

    def __init__(self, config: Config) -> None:
        self._config = config

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        app_id = getattr(session, "auth_data", None)
        if not isinstance(app_id, str):
            return "530 5.7.0 Authentication required"

        raw = envelope.original_content or envelope.content
        if raw is None:
            return "554 5.5.0 empty message"
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        from_address = envelope.mail_from or ""
        to_addresses = [a for a in envelope.rcpt_tos if a]
        if not to_addresses:
            return "554 5.5.0 no recipients"

        # Credential fetch (httpx) and the smarthost relay (smtplib) are blocking
        # network I/O; run them off the aiosmtpd event loop so one slow upstream
        # call can't stall every other in-flight SMTP session.
        return await asyncio.to_thread(self._deliver, raw, from_address, to_addresses)

    def _deliver(self, raw: bytes, from_address: str, to_addresses: list[str]) -> str:
        """Blocking: fetch the relay credential and relay the message. Returns the
        SMTP status line. Runs in a worker thread (see handle_DATA)."""
        cred = self._fetch_credential()
        if cred is None:
            return "451 4.3.0 email relay not configured on this instance"
        try:
            _relay_to_smarthost(cred, raw, from_address, to_addresses)
        except smtplib.SMTPException as e:
            logger.warning(f"email service: relay to smarthost failed: {e}")
            return f"451 4.4.0 upstream relay failed: {e}"
        except OSError as e:
            logger.warning(f"email service: smarthost connection failed: {e}")
            return f"451 4.4.0 upstream relay unreachable: {e}"
        return "250 OK message accepted for delivery"

    def _fetch_credential(self) -> RelayCredential | None:
        db = get_db()
        try:
            return get_relay_credential_provider(self._config).get(db)
        except RelayCredentialError as e:
            logger.warning(f"email service: could not fetch relay credential: {e}")
            return None
        finally:
            db.close()


def _relay_to_smarthost(cred: RelayCredential, raw: bytes, from_address: str, to_addresses: list[str]) -> None:
    """Relay ``raw`` to the Imbue smarthost over implicit TLS with the credential.

    The smarthost (the Imbue email proxy) terminates implicit TLS at its edge on
    whatever submission port it publishes (465/587), so we always connect with
    implicit TLS. Envelope from/to are preserved; the message body is sent
    verbatim (SES signs DKIM downstream).
    """
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host=cred.smtp_relay_host, port=cred.smtp_relay_port, context=context, timeout=30) as client:
        client.login(cred.smtp_relay_user, cred.smtp_relay_password)
        client.sendmail(from_address, to_addresses, raw)


_controllers: list[Controller] = []
_start_lock = threading.Lock()


def _make_controller(config: Config, host: str, port: int) -> Controller:
    return Controller(
        EmailServiceHandler(config),
        hostname=host,
        port=port,
        authenticator=EmailServiceAuthenticator(),
        # Require AUTH; do not accept unauthenticated submission. auth_require_tls
        # is False because the listener is loopback/gateway-only (never public)
        # and the app<->router hop stays on the host.
        auth_required=True,
        auth_require_tls=False,
    )


def start_email_smtp_service(config: Config, *, hosts: list[str], port: int = ROUTER_SMTP_PORT) -> list[Controller]:
    """Start the router's SMTP submission listener(s) for the email service.

    Idempotent: starts at most once per process. Binds one listener per host in
    ``hosts`` (loopback for network_host apps, the container gateway for pasta
    apps, reached via ``host.containers.internal``). A host that can't be bound
    (interface absent in dev/CI) is skipped rather than failing boot. Returns the
    started controllers.
    """
    global _controllers
    with _start_lock:
        if _controllers:
            return _controllers
        started: list[Controller] = []
        for host in hosts:
            try:
                controller = _make_controller(config, host, port)
                controller.start()
            except OSError as e:
                logger.warning(f"email service: could not bind SMTP listener on {host}:{port}: {e}")
                continue
            logger.info(f"email service: SMTP submission listener started on {host}:{port}")
            started.append(controller)
        _controllers = started
        return started
