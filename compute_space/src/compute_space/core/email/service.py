"""Identity + grant model for the OpenHost ``email`` v2 service.

Email is an OpenHost-provided service (like ``installer``): apps declare they
consume it in their manifest and request the ``send`` grant; the router runs an
SMTP submission listener that authenticates the app, checks this grant, and
relays the message to the Imbue email proxy smarthost with the per-instance
relay credential attached. The app never sees that credential.

The service URL is only compared (never fetched) by the grant check, mirroring
``INSTALLER_SERVICE_URL``. Human-readable spec lives at ``services/email`` in
this repo.
"""

from __future__ import annotations

from compute_space.core.auth.permissions_v2 import Grant

# Service URL apps put in their manifest's [[services.v2.consumes]] to use email.
EMAIL_SERVICE_URL = "github.com/imbue-openhost/openhost/services/email"

# SemVer this build of the email service exposes.
EMAIL_SERVICE_VERSION = "0.1.0"

# Internal SMTP submission port the router listens on for local apps (the email
# service). Not public (bound to loopback + the container gateway only) and
# distinct from 25, which Stalwart uses for inbound delivery. Lives here (a
# dependency-light module) so container provisioning can inject it without
# pulling in aiosmtpd.
ROUTER_SMTP_PORT = 2525

# The single grant the email service currently defines: permission to send
# outbound mail through the instance's relay. An opaque string grant (the doc's
# "simple flag-style permission" shape).
EMAIL_GRANT_SEND = "send"


def grants_allow_send(grants: list[Grant]) -> bool:
    """True iff any granted payload authorizes outbound send.

    Accepts the bare ``"send"` string grant. Kept as a function (not an ``in``
    check at the call site) so richer grant shapes (e.g. per-address objects)
    can be added here without touching the SMTP auth path.
    """
    return any(g == EMAIL_GRANT_SEND for g in grants)
