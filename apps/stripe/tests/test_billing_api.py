from unittest.mock import AsyncMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
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


@override_settings(
    STRIPE_PORTAL_LOGIN_URL="https://billing.stripe.com/p/login/testfallback"
)
class LicenseInvoiceTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("api:license_invoice")

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_valid_customer_with_invoice_redirects_to_hosted_url(self, mock_fetch):
        mock_fetch.return_value = make_invoice(
            "https://invoice.stripe.com/i/test_abc123"
        )

        res = self.client.get(self.url, {"customer_id": "cus_validTestId"})

        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "https://invoice.stripe.com/i/test_abc123")
        mock_fetch.assert_called_once_with("cus_validTestId")

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_customer_with_no_invoices_redirects_to_fallback(self, mock_fetch):
        mock_fetch.return_value = None

        res = self.client.get(self.url, {"customer_id": "cus_emptyTestId"})

        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            res["Location"], "https://billing.stripe.com/p/login/testfallback"
        )

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_invoice_without_hosted_url_redirects_to_fallback(self, mock_fetch):
        mock_fetch.return_value = make_invoice(hosted_invoice_url=None)

        res = self.client.get(self.url, {"customer_id": "cus_draftTestId"})

        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            res["Location"], "https://billing.stripe.com/p/login/testfallback"
        )

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_unknown_customer_redirects_to_fallback(self, mock_fetch):
        mock_fetch.side_effect = StripeResourceNotFound()

        res = self.client.get(self.url, {"customer_id": "cus_nonexistent"})

        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            res["Location"], "https://billing.stripe.com/p/login/testfallback"
        )

    def test_malformed_customer_id_returns_400(self):
        res = self.client.get(self.url, {"customer_id": "not_a_valid_format"})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"detail": "Invalid customer ID format"})

    def test_missing_customer_id_returns_400(self):
        res = self.client.get(self.url)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json(), {"detail": "Invalid customer ID format"})

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_rate_limit_kicks_in_after_ten_requests(self, mock_fetch):
        mock_fetch.return_value = make_invoice()

        for _ in range(10):
            res = self.client.get(self.url, {"customer_id": "cus_someTestId"})
            self.assertEqual(res.status_code, 302)

        res = self.client.get(self.url, {"customer_id": "cus_someTestId"})
        self.assertEqual(res.status_code, 429)

    @patch(
        "apps.stripe.billing_api.fetch_latest_invoice_for_customer",
        new_callable=AsyncMock,
    )
    def test_unexpected_stripe_error_returns_500(self, mock_fetch):
        mock_fetch.side_effect = Exception("Stripe API Error: 500 - oh no")

        res = self.client.get(self.url, {"customer_id": "cus_validTestId"})

        self.assertEqual(res.status_code, 500)
        self.assertNotIn("oh no", res.content.decode())
