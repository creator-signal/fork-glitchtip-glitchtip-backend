from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.stripe.exceptions import StripeResourceNotFound
from apps.stripe.schema import Invoice


def make_invoice(
    hosted_invoice_url: str | None = "https://invoice.stripe.com/i/test_abc",
):
    return Invoice(
        object="invoice",
        id="in_test123",
        status="paid",
        hosted_invoice_url=hosted_invoice_url,
    )


def make_subscription_with_customer(customer_email: str = "billing@example.com"):
    """Stand-in for a SubscriptionExpandCustomer. Only `.customer.id` and
    `.customer.email` are read by the endpoint."""
    return SimpleNamespace(
        customer=SimpleNamespace(id="cus_test", email=customer_email)
    )


UNIFORM_BODY = {"detail": "Could not verify license"}


class LicenseInvoiceTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("api:license_invoice")

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_match_redirects_to_invoice(self, mock_inv, mock_sub):
        mock_sub.return_value = make_subscription_with_customer("billing@example.com")
        mock_inv.return_value = make_invoice("https://invoice.stripe.com/i/abc")

        res = self.client.get(
            self.url,
            {"license_key": "sub_validTest", "email": "billing@example.com"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "https://invoice.stripe.com/i/abc")

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    def test_wrong_email_returns_uniform_404(self, mock_sub):
        mock_sub.return_value = make_subscription_with_customer("right@example.com")

        res = self.client.get(
            self.url, {"license_key": "sub_validTest", "email": "wrong@example.com"}
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    def test_unknown_subscription_returns_uniform_404(self, mock_sub):
        mock_sub.side_effect = StripeResourceNotFound()

        res = self.client.get(
            self.url,
            {"license_key": "sub_nonexistent", "email": "any@example.com"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    def test_malformed_license_key_returns_uniform_404(self):
        res = self.client.get(
            self.url, {"license_key": "garbage", "email": "any@example.com"}
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    def test_cus_format_rejected_uniform_404(self):
        """Customer ID (cus_...) format is rejected; only subscription IDs accepted."""
        res = self.client.get(
            self.url,
            {"license_key": "cus_OldCustomerId", "email": "any@example.com"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    def test_malformed_email_returns_uniform_404(self):
        res = self.client.get(
            self.url, {"license_key": "sub_validTest", "email": "garbage"}
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    def test_missing_license_key_returns_uniform_404(self):
        res = self.client.get(self.url, {"email": "any@example.com"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    def test_missing_email_returns_uniform_404(self):
        res = self.client.get(self.url, {"license_key": "sub_validTest"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_no_invoice_returns_uniform_404(self, mock_inv, mock_sub):
        mock_sub.return_value = make_subscription_with_customer("billing@example.com")
        mock_inv.return_value = None

        res = self.client.get(
            self.url,
            {"license_key": "sub_validTest", "email": "billing@example.com"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), UNIFORM_BODY)

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_email_compare_is_case_insensitive(self, mock_inv, mock_sub):
        mock_sub.return_value = make_subscription_with_customer("Billing@EXAMPLE.com")
        mock_inv.return_value = make_invoice()

        res = self.client.get(
            self.url,
            {"license_key": "sub_validTest", "email": "billing@example.com"},
        )
        self.assertEqual(res.status_code, 302)

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_rate_limit(self, mock_inv, mock_sub):
        mock_sub.return_value = make_subscription_with_customer("billing@example.com")
        mock_inv.return_value = make_invoice()
        params = {"license_key": "sub_validTest", "email": "billing@example.com"}

        for _ in range(10):
            res = self.client.get(self.url, params)
            self.assertEqual(res.status_code, 302)

        res = self.client.get(self.url, params)
        self.assertEqual(res.status_code, 429)

    @patch(
        "apps.stripe.billing_api.fetch_subscription_with_customer",
        new_callable=AsyncMock,
    )
    def test_unexpected_subscription_error_returns_500(self, mock_sub):
        mock_sub.side_effect = Exception("Stripe API Error: 500 - oh no")

        res = self.client.get(
            self.url,
            {"license_key": "sub_validTest", "email": "billing@example.com"},
        )
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("oh no", res.content.decode())
