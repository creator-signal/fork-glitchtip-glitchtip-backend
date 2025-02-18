import logging

from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.organizations_ext.models import Organization

from .client import list_prices, list_products, list_subscriptions
from .utils import unix_to_datetime

logger = logging.getLogger(__name__)


class StripeModel(models.Model):
    stripe_id = models.CharField(primary_key=True, max_length=30)

    class Meta:
        abstract = True


class StripeProduct(StripeModel):
    name = models.CharField()
    description = models.TextField()
    default_price = models.ForeignKey(
        "StripePrice", on_delete=models.CASCADE, blank=True, null=True
    )
    events = models.PositiveBigIntegerField()
    is_public = models.BooleanField()

    def __str__(self):
        return f"{self.name} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        stripe_ids = set()
        async for products_page in list_products():
            logger.info(f"Found {len(products_page)} products in Stripe")
            products_page = [
                product for product in products_page if "events" in product.metadata
            ]
            products = [
                StripeProduct(
                    stripe_id=product.id,
                    name=product.name,
                    description=product.description if product.description else "",
                    events=product.metadata["events"],
                    is_public=product.metadata.get("is_public") == "true",
                )
                for product in products_page
            ]
            prices = [
                StripePrice(
                    stripe_id=product.default_price.id,
                    price=product.default_price.unit_amount / 100,
                    nickname=product.default_price.nickname or "",
                    product_id=product.id,
                )
                for product in products_page
                if product.default_price
                and product.default_price.unit_amount is not None
            ]
            product_updated = await StripeProduct.objects.abulk_create(
                products,
                update_conflicts=True,
                update_fields=["name", "description", "events", "is_public"],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(product_updated)} products in Django")
            price_updated = await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=["price", "nickname", "product_id"],
                unique_fields=["stripe_id"],
            )
            logger.info(f"Created/updated {len(price_updated)} prices in Django")
            for product in product_updated:
                for price in price_updated:
                    if (
                        price.product_id == product.stripe_id
                        and product.default_price_id != price.stripe_id
                    ):
                        product.default_price_id = price.stripe_id
                        await product.asave(update_fields=["default_price_id"])

            for obj in product_updated:
                stripe_ids.add(obj.stripe_id)

        result = await StripeProduct.objects.exclude(stripe_id__in=stripe_ids).adelete()
        if result[0]:
            logger.info(f"Deleted {result[0]} products in Django")


class StripePrice(StripeModel):
    price = models.DecimalField(max_digits=10, decimal_places=2)
    nickname = models.CharField(max_length=255)
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.nickname} {self.price} {self.stripe_id}"

    @classmethod
    async def sync_from_stripe(cls):
        async for prices_page in list_prices():
            product_ids = {price.product for price in prices_page}
            products = StripeProduct.objects.filter(stripe_id__in=product_ids)
            known_product_ids = set()
            async for product in products:
                known_product_ids.add(product.stripe_id)

            prices = [
                StripePrice(
                    stripe_id=price.id,
                    price=price.unit_amount / 100,
                    nickname=price.nickname or "",
                    product_id=price.product,
                )
                for price in prices_page
                if price.unit_amount is not None and price.product in known_product_ids
            ]
            await StripePrice.objects.abulk_create(
                prices,
                update_conflicts=True,
                update_fields=["price", "nickname", "product_id"],
                unique_fields=["stripe_id"],
            )


class StripeSubscription(StripeModel):
    is_active = models.BooleanField()
    is_primary = models.BooleanField(default=False)
    created = models.DateTimeField()
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    product = models.ForeignKey(StripeProduct, on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.SET_NULL, null=True
    )

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["organization", "is_primary"],
                condition=Q(is_primary=True),
                name="unique_primary_subscription_per_organization",
            ),
        ]

    @classmethod
    async def sync_from_stripe(cls):
        organization_ids = set()
        active_organization_ids = set()
        async for subscriptions in list_subscriptions():
            logger.info(f"Found {len(subscriptions)} subcriptions in Stripe")

            subscription_objects = []
            for subscription in subscriptions:
                org_metadata = subscription.customer.metadata
                try:
                    organization_id = int(
                        org_metadata.get(
                            "organization_id", org_metadata.get("djstripe_subscriber")
                        )
                    )
                except (ValueError, KeyError):
                    continue  # Skip if no organization ID in metadata

                items = subscription.items.data
                if not items or not items[0].get("price", {}).get("product"):
                    continue  # Skip

                product_id = items[0]["price"]["product"]

                # If unseen organization id, check if it exists
                if organization_id not in organization_ids:
                    organization_ids.add(organization_id)
                    organization = await Organization.objects.filter(
                        id=organization_id
                    ).afirst()
                    if organization:
                        active_organization_ids.add(organization_id)
                        if not organization.stripe_customer_id:
                            organization.stripe_customer_id = subscription.customer.id
                            await organization.asave(
                                update_fields=["stripe_customer_id"]
                            )
                # Only save subscriptions with organizations that exist
                if organization_id in active_organization_ids:
                    subscription_objects.append(
                        StripeSubscription(
                            stripe_id=subscription.id,
                            created=unix_to_datetime(subscription.created),
                            current_period_start=unix_to_datetime(
                                subscription.current_period_start
                            ),
                            current_period_end=unix_to_datetime(
                                subscription.current_period_end
                            ),
                            product_id=product_id,
                            organization_id=organization_id,
                            is_active=subscription.status == "active",
                        )
                    )

            stripe_subscriptions = await StripeSubscription.objects.abulk_create(
                subscription_objects,
                update_conflicts=True,
                update_fields=[
                    "created",
                    "current_period_start",
                    "current_period_end",
                    "product_id",
                    "organization_id",
                    "is_active",
                ],
                unique_fields=["stripe_id"],
            )
            logger.info(
                f"Created/updated {len(stripe_subscriptions)} subscriptions in Django"
            )
