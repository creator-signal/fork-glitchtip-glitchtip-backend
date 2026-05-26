import base64
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, override_settings

from apps.self_host_licensing import verifier
from apps.self_host_licensing.signing import InvalidLicense, decode, encode


def _make_keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    pub_b64 = base64.b64encode(
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return private, {"v1": public}, pub_b64


class SigningTests(SimpleTestCase):
    def test_encode_decode_roundtrip(self):
        private, public_keys, _ = _make_keypair()
        claims = {"sub": "sub_123", "exp": int(time.time()) + 3600}
        blob = encode(claims, private, "v1")

        verified = decode(blob, public_keys)
        self.assertEqual(verified.kid, "v1")
        self.assertEqual(verified.claims, claims)

    def test_decode_rejects_tampered_payload(self):
        private, public_keys, _ = _make_keypair()
        blob = encode({"sub": "sub_123"}, private, "v1")
        header, payload, sig = blob.split(".")
        tampered = f"{header}.{payload}x.{sig}"
        with self.assertRaises(InvalidLicense):
            decode(tampered, public_keys)

    def test_decode_rejects_unknown_kid(self):
        private, _, _ = _make_keypair()
        blob = encode({"sub": "sub_123"}, private, "v1")
        _, other_public_keys, _ = _make_keypair()
        with self.assertRaises(InvalidLicense):
            decode(blob, other_public_keys)

    def test_decode_rejects_malformed(self):
        _, public_keys, _ = _make_keypair()
        with self.assertRaises(InvalidLicense):
            decode("not-a-jwt", public_keys)


class VerifierTests(SimpleTestCase):
    def _setup_blob_and_pubkey(self, claims):
        private = Ed25519PrivateKey.generate()
        public = private.public_key()
        pub_b64 = base64.b64encode(
            public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode()
        blob = encode(claims, private, "vtest")
        return blob, pub_b64

    def test_active_license(self):
        exp = int(time.time()) + 3600
        blob, pub_b64 = self._setup_blob_and_pubkey(
            {"sub": "sub_abc", "exp": exp, "pln": "workspace_annual"}
        )
        with override_settings(
            GLITCHTIP_LICENSE_KEY=blob,
            SELF_HOST_LICENSE_EXTRA_PUBLIC_KEYS=[("vtest", pub_b64)],
        ):
            status = verifier.get_license_status()
        self.assertTrue(status.active)
        self.assertEqual(status.plan, "workspace_annual")
        self.assertEqual(status.expires_at, exp)

    def test_expired_past_grace(self):
        exp = int(time.time()) - verifier.GRACE_PERIOD_SECONDS - 60
        blob, pub_b64 = self._setup_blob_and_pubkey({"sub": "sub_abc", "exp": exp})
        with override_settings(
            GLITCHTIP_LICENSE_KEY=blob,
            SELF_HOST_LICENSE_EXTRA_PUBLIC_KEYS=[("vtest", pub_b64)],
        ):
            status = verifier.get_license_status()
        self.assertFalse(status.active)

    def test_within_grace(self):
        # Expired 1 day ago — still within 14-day grace.
        exp = int(time.time()) - 86400
        blob, pub_b64 = self._setup_blob_and_pubkey({"sub": "sub_abc", "exp": exp})
        with override_settings(
            GLITCHTIP_LICENSE_KEY=blob,
            SELF_HOST_LICENSE_EXTRA_PUBLIC_KEYS=[("vtest", pub_b64)],
        ):
            status = verifier.get_license_status()
        self.assertTrue(status.active)

    def test_no_license_configured(self):
        with override_settings(GLITCHTIP_LICENSE_KEY=None):
            self.assertFalse(verifier.get_license_status().active)

    def test_garbage_license(self):
        with override_settings(GLITCHTIP_LICENSE_KEY="garbage"):
            self.assertFalse(verifier.get_license_status().active)
