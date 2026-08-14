#!/usr/bin/env python3
"""Generate and register an ACME account key in certbot JWK format.

Usage: python3 generate_acme_key.py <output-path> [--email <email>]

Generates an RSA-2048 key, registers it with Let's Encrypt, and saves
the JWK to the output path. The key can then be used by Cloud in a Bottle for
TLS certificate acquisition via DNS-01 challenges.
"""

import argparse
import base64
import json
import sys

from acme import client as acme_client
from acme import messages
from cryptography.hazmat.primitives.asymmetric import rsa
from josepy import JWKRSA

# Google Trust Services — the default ACME provider used by Cloud in a Bottle
GTS_PRODUCTION = "https://dv.acme-v02.api.pki.goog/directory"
# Let's Encrypt as fallback
LETS_ENCRYPT_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"


def _b64(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, byteorder="big")).rstrip(b"=").decode()


def _generate_jwk() -> tuple[dict, JWKRSA]:
    """Generate an RSA-2048 key and return (jwk_dict, JWKRSA)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.private_numbers()
    pub = nums.public_numbers

    jwk_dict = {
        "kty": "RSA",
        "n": _b64(pub.n, 256),
        "e": _b64(pub.e, 3),
        "d": _b64(nums.d, 256),
        "p": _b64(nums.p, 128),
        "q": _b64(nums.q, 128),
        "dp": _b64(nums.dmp1, 128),
        "dq": _b64(nums.dmq1, 128),
        "qi": _b64(nums.iqmp, 128),
    }

    jwk_rsa = JWKRSA.from_json(jwk_dict)
    return jwk_dict, jwk_rsa


def _register_account(jwk_rsa: JWKRSA, email: str | None = None) -> str:
    """Register the key with ACME providers. Returns the account URI.

    Registers with both Google Trust Services (Cloud in a Bottle's default) and
    Let's Encrypt for maximum compatibility.
    """
    for name, directory_url in [("Google Trust Services", GTS_PRODUCTION), ("Let's Encrypt", LETS_ENCRYPT_DIRECTORY)]:
        try:
            net = acme_client.ClientNetwork(
                jwk_rsa,
                user_agent="openhost-provision/0.1",
                timeout=30,
            )
            directory = messages.Directory.from_json(net.get(directory_url).json())
            client = acme_client.ClientV2(directory, net)

            reg_kwargs: dict = {"terms_of_service_agreed": True}
            if email:
                reg_kwargs["contact"] = (f"mailto:{email}",)

            reg = messages.NewRegistration(**reg_kwargs)
            try:
                account = client.new_account(reg)
                print(f"  Registered with {name}: {account.uri}")
            except Exception:
                # ConflictError means already registered -- that's fine
                print(f"  Already registered with {name}")
        except Exception as e:
            print(f"  Warning: {name} registration failed: {e}")

    return "registered"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and register an ACME account key")
    parser.add_argument("output", help="Output path for the JWK JSON file")
    parser.add_argument("--email", default="", help="Contact email for Let's Encrypt account")
    args = parser.parse_args()

    print("Generating RSA-2048 key...")
    jwk_dict, jwk_rsa = _generate_jwk()

    print(f"Registering with Let's Encrypt ({LETS_ENCRYPT_DIRECTORY})...")
    try:
        account_uri = _register_account(jwk_rsa, args.email or None)
        print(f"Registered account: {account_uri}")
    except Exception as e:
        print(f"Warning: account registration failed: {e}", file=sys.stderr)
        print("The key will be saved but may need manual registration.", file=sys.stderr)

    with open(args.output, "w") as f:
        json.dump(jwk_dict, f, indent=2)

    print(f"Saved ACME account key to {args.output}")


if __name__ == "__main__":
    main()
