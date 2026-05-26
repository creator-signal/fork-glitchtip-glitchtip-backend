import base64
import hmac
import json
import time
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.self_host_licensing.models import IssuedLicense
from apps.self_host_licensing.signing import decode


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _keypair():
    priv = Ed25519PrivateKey.generate()
    priv_b64 = _b64(
        priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_b64 = _b64(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return priv_b64, pub_b64


def _sign_stripe(payload: bytes, secret: str) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.{payload.decode('utf-8')}"
    sig = hmac.new(secret.encode(), signed.encode(), digestmod="sha256").hexdigest()
    return f"t={timestamp},v1={sig}"


FAKE_SUBSCRIPTION_JSON = json.dumps(
    {
        "object": "subscription",
        "id": "sub_test_abc",
        "customer": "cus_test_abc",
        "created": 1700000000,
        "status": "active",
        "livemode": False,
        "metadata": {},
        "cancel_at_period_end": False,
        "start_date": 1700000000,
        "collection_method": "charge_automatically",
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_test",
                    "object": "subscription_item",
                    "created": 1700000000,
                    "current_period_start": 1700000000,
                    "current_period_end": int(time.time()) + 30 * 86400,
                    "metadata": {},
                    "price": {
                        "object": "price",
                        "id": "price_workspace",
                        "active": True,
                        "billing_scheme": "per_unit",
                        "created": 1700000000,
                        "currency": "usd",
                        "livemode": False,
                        "lookup_key": "self_host_workspace_annual",
                        "nickname": None,
                        "product": "prod_abc",
                        "recurring": {"interval": "year"},
                        "tax_behavior": None,
                        "tiers_mode": None,
                        "type": "recurring",
                        "unit_amount": 180000,
                        "unit_amount_decimal": "180000",
                        "metadata": {},
                    },
                    "quantity": 5,
                    "subscription": "sub_test_abc",
                    "tax_rates": [],
                }
            ],
        },
    }
)

FAKE_CUSTOMER_JSON = json.dumps(
    {
        "object": "customer",
        "id": "cus_test_abc",
        "email": "buyer@example.com",
        "metadata": {},
        "name": "Test Buyer",
    }
)


async def fake_stripe_get(endpoint: str, params=None):
    if endpoint.startswith("subscriptions/"):
        return FAKE_SUBSCRIPTION_JSON
    if endpoint.startswith("customers/"):
        return FAKE_CUSTOMER_JSON
    raise AssertionError(f"unexpected stripe_get endpoint: {endpoint}")


WEBHOOK_SECRET = "whsec_test_self_host"
SIGNING_KEY_B64, PUBLIC_KEY_B64 = _keypair()


@override_settings(
    STRIPE_SELF_HOST_WEBHOOK_SECRET=WEBHOOK_SECRET,
    SELF_HOST_LICENSE_SIGNING_KEY=SIGNING_KEY_B64,
    SELF_HOST_LICENSE_SIGNING_KID="vtest",
    SELF_HOST_LICENSE_EXTRA_PUBLIC_KEYS=[("vtest", PUBLIC_KEY_B64)],
    SELF_HOST_LICENSING_ENABLED=True,
)
class SelfHostWebhookTests(TestCase):
    def setUp(self):
        cache.clear()

    def _post(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        sig = _sign_stripe(body, WEBHOOK_SECRET)
        return self.client.post(
            "/self-host-licensing/stripe-webhook/",
            data=body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=sig,
        )

    @patch("apps.self_host_licensing.issuer.stripe_get", side_effect=fake_stripe_get)
    @patch("apps.self_host_licensing.issuer.send_license_email")
    def test_checkout_completed_mints_license(self, mock_email, _mock_stripe):
        payload = {
            "id": "evt_test_1",
            "object": "event",
            "api_version": "2024-01-01",
            "created": int(time.time()),
            "type": "checkout.session.completed",
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": None, "idempotency_key": None},
            "data": {
                "object": {
                    "id": "cs_test",
                    "object": "checkout.session",
                    "subscription": "sub_test_abc",
                }
            },
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)

        issued = IssuedLicense.objects.get(stripe_subscription_id="sub_test_abc")
        self.assertEqual(issued.email, "buyer@example.com")
        self.assertEqual(issued.plan, "self_host_workspace_annual")
        self.assertEqual(issued.status, "active")
        self.assertEqual(issued.last_stripe_event_id, "evt_test_1")

        self.assertEqual(mock_email.call_count, 1)
        emitted_blob = mock_email.call_args.args[1]
        from apps.self_host_licensing.keys import load_trusted_public_keys

        verified = decode(emitted_blob, load_trusted_public_keys())
        self.assertEqual(verified.claims["sub"], "sub_test_abc")
        self.assertEqual(verified.claims["cus"], "cus_test_abc")
        self.assertEqual(verified.claims["eml"], "buyer@example.com")
        self.assertEqual(verified.claims["sts"], 5)

    @patch("apps.self_host_licensing.issuer.stripe_get", side_effect=fake_stripe_get)
    @patch("apps.self_host_licensing.issuer.send_license_email")
    def test_webhook_is_idempotent(self, mock_email, _mock_stripe):
        payload = {
            "id": "evt_test_dup",
            "object": "event",
            "api_version": "2024-01-01",
            "created": int(time.time()),
            "type": "customer.subscription.updated",
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": None, "idempotency_key": None},
            "data": {"object": {"id": "sub_test_abc", "object": "subscription"}},
        }
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(IssuedLicense.objects.count(), 1)
        self.assertEqual(mock_email.call_count, 1)

    def test_bad_signature_rejected(self):
        body = json.dumps({"id": "evt_bad"}).encode()
        response = self.client.post(
            "/self-host-licensing/stripe-webhook/",
            data=body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=deadbeef",
        )
        self.assertEqual(response.status_code, 403)
