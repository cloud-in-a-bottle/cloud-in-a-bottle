"""Cloud in a Bottle federated identity provider.

Each zone acts as an identity provider for its owner. This module handles:
- Exposing the zone's public identity (domain + public key)
- Signing identity tokens so the owner can prove their identity to remote apps

The identity keypair is separate from the auth keypair. Auth keys are ephemeral
(regenerated on reboot, used for zone_auth cookies). Identity keys are persistent
(stored on the data disk, so remote apps can recognize this zone across reboots).

Flow (from the perspective of this zone's owner visiting a remote app):
1. Remote app redirects owner here: /identity/approve?callback=URL
2. Owner sees "approve login?" page, clicks approve
3. This zone signs a JWT with the callback URL as the audience claim
4. Redirects back to callback URL with the signed identity token
"""

import sqlite3
import time
from pathlib import Path

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from compute_space.core.domains import primary_domain_or_none
from compute_space.core.logging import logger
from compute_space.core.util import write_restricted

# Identity tokens are short-lived (5 minutes) since they're one-time-use
IDENTITY_TOKEN_EXPIRY: int = 300

_identity_private_key: str | None = None
_identity_public_key: str | None = None


def _identity_keys_dir(data_dir: str) -> Path:
    """Identity keys live on the persistent data disk."""
    return Path(data_dir) / "vm_data" / "identity_keys"


def load_identity_keys(data_dir: str) -> None:
    """Load or generate the persistent identity keypair.

    Must be called after the data disk is mounted (i.e. from init_app).
    Logs a warning and leaves keys as None if the data disk is not available.
    """
    global _identity_private_key, _identity_public_key

    try:
        keys_dir = _identity_keys_dir(data_dir)
        priv_path = keys_dir / "private.pem"
        pub_path = keys_dir / "public.pem"

        if priv_path.exists() and pub_path.exists():
            _identity_private_key = priv_path.read_text()
            _identity_public_key = pub_path.read_text()
            logger.info("Loaded persistent identity keys from %s", keys_dir)
        else:
            keys_dir.mkdir(parents=True, exist_ok=True)
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            _identity_private_key = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
            _identity_public_key = (
                private_key.public_key()
                .public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                .decode()
            )
            write_restricted(priv_path, _identity_private_key)
            pub_path.write_text(_identity_public_key)
            logger.info("Generated new persistent identity keys at %s", keys_dir)
    except (OSError, PermissionError, ValueError) as e:
        logger.warning("Could not load/generate identity keys: %s", e)


def _zone_domain_name(db: sqlite3.Connection) -> str:
    primary = primary_domain_or_none(db)
    return primary.name if primary else "localhost"


def get_zone_identity(db: sqlite3.Connection) -> dict[str, str]:
    """Return this zone's public identity info."""
    if _identity_public_key is None:
        raise RuntimeError("Identity keys not loaded. Call load_identity_keys() after disk mount.")
    return {
        "domain": _zone_domain_name(db),
        "public_key_pem": _identity_public_key,
        "protocol": "openhost-identity-v1",
    }


def sign_identity_token(callback_url: str, db: sqlite3.Connection) -> str:
    """Sign an identity assertion token for the owner.

    Returns a JWT signed with this zone's persistent identity key containing:
    - sub: the zone domain (identity of the signer)
    - aud: the callback URL (prevents token reuse across apps)
    - iat/exp: timestamps
    """
    if _identity_private_key is None:
        raise RuntimeError("Identity keys not loaded")

    now = int(time.time())
    payload = {
        "sub": _zone_domain_name(db),
        "aud": callback_url,
        "iat": now,
        "exp": now + IDENTITY_TOKEN_EXPIRY,
    }
    return pyjwt.encode(payload, _identity_private_key, algorithm="RS256")
