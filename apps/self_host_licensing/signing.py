"""
Compact signed license blob.

Format: base64url(header).base64url(payload).base64url(signature)
Equivalent to a JWT with alg=EdDSA, no external JWT library required.

Ed25519 was chosen because verification is fast, keys are small (32 bytes),
and `cryptography` is already a transitive dependency of this project, so the
self-host verifier ships with no new packages.
"""

import base64
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


@dataclass
class VerifiedClaims:
    kid: str
    claims: dict


class InvalidLicense(Exception):
    pass


def encode(claims: dict, private_key: Ed25519PrivateKey, kid: str) -> str:
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    header_b = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    payload_b = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{header_b}.{payload_b}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{header_b}.{payload_b}.{_b64url_encode(signature)}"


def decode(blob: str, public_keys: dict[str, Ed25519PublicKey]) -> VerifiedClaims:
    """Verify signature and return claims. Does NOT check expiration."""
    try:
        header_b, payload_b, sig_b = blob.split(".")
    except ValueError as e:
        raise InvalidLicense("malformed blob") from e

    try:
        header = json.loads(_b64url_decode(header_b))
        claims = json.loads(_b64url_decode(payload_b))
        signature = _b64url_decode(sig_b)
    except (ValueError, json.JSONDecodeError) as e:
        raise InvalidLicense("unable to decode blob") from e

    if header.get("alg") != "EdDSA":
        raise InvalidLicense("unexpected alg")

    kid = header.get("kid")
    if not isinstance(kid, str):
        raise InvalidLicense("missing kid")

    public_key = public_keys.get(kid)
    if public_key is None:
        raise InvalidLicense(f"unknown kid {kid}")

    signing_input = f"{header_b}.{payload_b}".encode("ascii")
    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature as e:
        raise InvalidLicense("bad signature") from e

    if not isinstance(claims, dict):
        raise InvalidLicense("claims must be an object")

    return VerifiedClaims(kid=kid, claims=claims)
