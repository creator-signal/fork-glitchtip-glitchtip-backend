import json
from unittest.mock import AsyncMock, patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.stripe.exceptions import StripeResourceNotFound
from apps.stripe.schema import Customer


def make_customer(
    id: str = "cus_test123",
    email: str | None = "user@example.com",
):
    return Customer(object="customer", id=id, email=email, metadata={}, name=None)


class CustomerByEmailTestCase(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()
        self.url = reverse("api:customer_by_email")

    def _post(self, body, **kwargs):
        if isinstance(body, dict):
            body = json.dumps(body)
        return self.client.post(
            self.url, data=body, content_type="application/json", **kwargs
        )

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_known_email_sends_license_key_email_to_stripe_address(self, mock_fetch):
        # Submit one casing, Stripe has another — we send to Stripe's.
        mock_fetch.return_value = make_customer(
            id="cus_realCustomer", email="user@example.com"
        )

        res = self._post({"email": "USER@example.com"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["user@example.com"])  # Stripe's address
        self.assertIn("cus_realCustomer", sent.body)
        self.assertIn("GlitchTip", sent.subject)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_unknown_email_returns_ok_without_sending(self, mock_fetch):
        mock_fetch.return_value = None

        res = self._post({"email": "nobody@example.com"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 0)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_stripe_not_found_returns_ok_without_sending(self, mock_fetch):
        mock_fetch.side_effect = StripeResourceNotFound()

        res = self._post({"email": "user@example.com"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 0)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_customer_with_null_email_is_not_sent_to_caller(self, mock_fetch):
        """Stripe customer has no email on file → do not leak match status by
        sending to the caller-supplied address."""
        mock_fetch.return_value = make_customer(id="cus_noEmail", email=None)

        res = self._post({"email": "user@example.com"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 0)

    def test_malformed_email_returns_400(self):
        res = self._post({"email": "garbage"})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"detail": "Invalid email"})
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_email_field_returns_4xx(self):
        # ninja's default for missing required body field is 422. We accept
        # this — the user-facing form will never submit empty.
        res = self._post({})

        self.assertIn(res.status_code, (400, 422))
        self.assertEqual(len(mail.outbox), 0)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_ip_rate_limit_kicks_in_after_ten_requests(self, mock_fetch):
        mock_fetch.return_value = None

        # Distinct emails so we hit only the IP tier, not the email tier.
        for i in range(10):
            res = self._post({"email": f"user{i}@example.com"})
            self.assertEqual(res.status_code, 200, f"hit {i + 1} should pass")

        res = self._post({"email": "user_overflow@example.com"})
        self.assertEqual(res.status_code, 429)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_email_rate_limit_kicks_in_across_ips(self, mock_fetch):
        mock_fetch.return_value = None

        # Three hits on the same email from rotating IPs (REMOTE_ADDR override)
        # should pass; the fourth must 429 on the email tier.
        for i in range(3):
            res = self._post(
                {"email": "target@example.com"},
                REMOTE_ADDR=f"203.0.113.{i + 1}",
            )
            self.assertEqual(res.status_code, 200, f"hit {i + 1} should pass")

        res = self._post({"email": "target@example.com"}, REMOTE_ADDR="203.0.113.99")
        self.assertEqual(res.status_code, 429)

    @patch(
        "apps.stripe.tasks.fetch_customer_by_email",
        new_callable=AsyncMock,
    )
    def test_email_is_normalized_for_throttle_key(self, mock_fetch):
        """USER@example.com and user@example.com share the throttle bucket."""
        mock_fetch.return_value = None

        variants = [
            "user@example.com",
            "USER@example.com",
            "  user@example.com  ",
        ]
        for i, addr in enumerate(variants):
            res = self._post({"email": addr}, REMOTE_ADDR=f"198.51.100.{i + 1}")
            self.assertEqual(res.status_code, 200, f"hit {i + 1} should pass")

        # Fourth normalized-equivalent hit must 429 on the email tier.
        res = self._post({"email": "User@Example.COM"}, REMOTE_ADDR="198.51.100.250")
        self.assertEqual(res.status_code, 429)
