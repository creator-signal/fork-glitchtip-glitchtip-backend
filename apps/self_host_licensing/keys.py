"""
Ed25519 key material for self-host license signing and verification.

The TRUSTED_PUBLIC_KEYS list ships in every build of this repo — both on
app.glitchtip.com (the issuer) and on self-host installs (the verifiers).
Self-host installs use this list to verify license blobs offline, with no
network call to the issuer.

To rotate keys: generate a new keypair, append it to TRUSTED_PUBLIC_KEYS with
a new kid, release, wait a release cycle so self-hosts have the new public key
embedded, then switch the issuer's SELF_HOST_LICENSE_SIGNING_KID setting to
the new kid. Old blobs keep verifying until they naturally expire.
"""

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings

# kid -> base64-encoded raw Ed25519 public key (32 bytes)
#
# The placeholder below must be replaced with the real production public key
# before enabling self-host licensing. Generate one with:
#
#     python manage.py generate_self_host_license_keypair
#
TRUSTED_PUBLIC_KEYS: list[tuple[str, str]] = [
    # ("v1", "REPLACE_ME_BASE64_ED25519_PUBLIC_KEY"),
]


def load_trusted_public_keys() -> dict[str, Ed25519PublicKey]:
    """Combine compiled-in keys with any extras from settings."""
    entries: list[tuple[str, str]] = list(TRUSTED_PUBLIC_KEYS)
    extra = getattr(settings, "SELF_HOST_LICENSE_EXTRA_PUBLIC_KEYS", None) or []
    entries.extend(extra)

    result: dict[str, Ed25519PublicKey] = {}
    for kid, b64 in entries:
        raw = base64.b64decode(b64)
        result[kid] = Ed25519PublicKey.from_public_bytes(raw)
    return result


def load_signing_key() -> tuple[str, Ed25519PrivateKey] | None:
    """Return (kid, private_key) if the issuer is configured, else None."""
    raw_b64 = getattr(settings, "SELF_HOST_LICENSE_SIGNING_KEY", None)
    kid = getattr(settings, "SELF_HOST_LICENSE_SIGNING_KID", None)
    if not raw_b64 or not kid:
        return None
    raw = base64.b64decode(raw_b64)
    return kid, Ed25519PrivateKey.from_private_bytes(raw)
