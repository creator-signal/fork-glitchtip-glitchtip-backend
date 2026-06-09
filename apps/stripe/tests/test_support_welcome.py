import json
from unittest.mock import AsyncMock, patch

from django.core import mail
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import SupportLicenseWelcome
from apps.stripe.schema import (
    Price,
    Subscription,
    SubscriptionItem,
    SubscriptionItems,
)
from apps.stripe.views import update_subscription

SUPPORT_SUB_ID = "sub_support123"


def build_subscription(
    subscription_id=SUPPORT_SUB_ID,
    product_id="prod_support",
    status=SubscriptionStatus.ACTIVE,
):
    now = int(timezone.now().timestamp())
    return Subscription(
        object="subscription",
        id=subscription_id,
        customer="cus_test",
        items=SubscriptionItems(
            object="list",
            data=[
                SubscriptionItem(
                    id="si_test",
                    object="subscription_item",
                    created=now,
                    current_period_end=now + 2592000,
                    current_period_start=now,
                    metadata={},
                    price=Price(
                        object="price",
                        active=True,
                        billing_scheme=None,
                        created=0,
                        currency="usd",
                        livemode=False,
                        lookup_key=None,
                        nickname=None,
                        recurring=None,
                        tax_behavior=None,
                        tiers_mode=None,
                        type="",
                        unit_amount_decimal="1",
                        metadata={},
                        id="price_support",
                        product=product_id,
                        unit_amount=1,
                    ),
                    quantity=1,
                    subscription=subscription_id,
                    tax_rates=[],
                )
            ],
        ),
        created=now,
        status=status,
        livemode=False,
        metadata={},
        cancel_at_period_end=False,
        start_date=now,
        collection_method="charge_automatically",
    )


def stripe_get_side_effect(product_type="support", email="buyer@example.com"):
    """Return customer JSON for customers/*, product JSON for products/*."""

    async def _side_effect(path):
        if path.startswith("customers/"):
            return json.dumps(
                {
                    "object": "customer",
                    "id": "cus_test",
                    "email": email,
                    "metadata": {},
                    "name": None,
                }
            )
        if path.startswith("products/"):
            return json.dumps(
                {
                    "object": "product",
                    "id": "prod_support",
                    "active": True,
                    "attributes": [],
                    "created": 0,
                    "default_price": None,
                    "description": None,
                    "images": [],
                    "livemode": False,
                    "marketing_features": [],
                    "metadata": {"product_type": product_type},
                    "name": "Support Plan",
                    "statement_descriptor": None,
                    "tax_code": None,
                    "type": "service",
                    "unit_label": None,
                    "updated": 0,
                    "url": None,
                }
            )
        raise AssertionError(f"unexpected stripe_get path: {path}")

    return _side_effect


@override_settings(STRIPE_REGION="")
class SupportWelcomeEmailTestCase(TestCase):
    def setUp(self):
        self.request = RequestFactory().post("/")

    async def test_sends_welcome_and_records_idempotency(self):
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(),
        ):
            await update_subscription(build_subscription(), self.request)

        self.assertTrue(
            await SupportLicenseWelcome.objects.filter(
                stripe_id=SUPPORT_SUB_ID
            ).aexists()
        )
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(SUPPORT_SUB_ID, body)
        # The deep link carries only the key; the billing email is never
        # embedded in the link or sent in the body (in either alternative).
        for content in (body, html):
            self.assertIn(f"#sub={SUPPORT_SUB_ID}", content)
            self.assertNotIn("buyer@example.com", content)
            self.assertNotIn("&email=", content)
            self.assertNotIn("#email=", content)

    async def test_idempotent_no_duplicate_email(self):
        side = stripe_get_side_effect()
        with patch(
            "apps.stripe.views.stripe_get", new_callable=AsyncMock, side_effect=side
        ):
            await update_subscription(build_subscription(), self.request)
        with patch(
            "apps.stripe.views.stripe_get", new_callable=AsyncMock, side_effect=side
        ):
            await update_subscription(build_subscription(), self.request)

        self.assertEqual(
            await SupportLicenseWelcome.objects.filter(
                stripe_id=SUPPORT_SUB_ID
            ).acount(),
            1,
        )
        self.assertEqual(len(mail.outbox), 1)

    async def test_non_support_product_sends_nothing(self):
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(product_type="hosted"),
        ):
            await update_subscription(build_subscription(), self.request)

        self.assertFalse(await SupportLicenseWelcome.objects.aexists())
        self.assertEqual(len(mail.outbox), 0)

    async def test_inactive_status_defers_welcome(self):
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(),
        ):
            await update_subscription(
                build_subscription(status=SubscriptionStatus.INCOMPLETE),
                self.request,
            )
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(await SupportLicenseWelcome.objects.aexists())

        # Becomes active later -> welcome fires once.
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)

    async def test_missing_customer_email_defers(self):
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(email=None),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(await SupportLicenseWelcome.objects.aexists())

        # A later event that carries an email sends exactly once.
        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)

    async def test_send_failure_releases_row_for_retry(self):
        # A failed send must not burn the only delivery: no row is left behind,
        # the exception propagates (so the webhook 500s and Stripe retries), and
        # a later working delivery sends exactly once.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.email.SupportLicenseWelcomeEmail.send_email",
                side_effect=RuntimeError("smtp down"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await update_subscription(build_subscription(), self.request)
        self.assertFalse(await SupportLicenseWelcome.objects.aexists())
        self.assertEqual(len(mail.outbox), 0)

        with patch(
            "apps.stripe.views.stripe_get",
            new_callable=AsyncMock,
            side_effect=stripe_get_side_effect(),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(await SupportLicenseWelcome.objects.acount(), 1)
