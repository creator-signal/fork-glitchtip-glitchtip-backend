from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker


class StripeAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make(
            "organizations_ext.Organization", stripe_customer_id="cust_1"
        )
        cls.org_user = cls.organization.add_user(cls.user)
        cls.product = baker.make("stripe.StripeProduct", is_public=True, events=5)

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_stripe_products(self):
        url = reverse("api:list_stripe_products")
        res = self.client.get(url)
        self.assertContains(res, self.product.name)

    def test_get_stripe_subscription(self):
        sub = baker.make(
            "stripe.StripeSubscription", organization=self.organization, is_active=True
        )
        url = reverse("api:get_stripe_subscription", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, sub.stripe_id)

    @patch("apps.stripe.api.create_session")
    def test_create_stripe_session(self, mock_create_session):
        price = baker.make("stripe.StripePrice", product=self.product)
        url = reverse("api:create_stripe_session", args=[self.organization.slug])
        mock_create_session.return_value = {}
        res = self.client.post(
            url, {"price": price.stripe_id}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

    @patch("apps.stripe.api.create_portal_session")
    def test_manage_billing(self, mock_create_portal_session):
        url = reverse("api:stripe_billing_portal", args=[self.organization.slug])
        res = self.client.post(url, {}, content_type="application/json")
        self.assertEqual(res.status_code, 200)
