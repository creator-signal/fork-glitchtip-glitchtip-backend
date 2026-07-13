"""Provision the Stripe objects metered overage billing needs.

Idempotent and **test-mode only**: it refuses to run unless STRIPE_SECRET_KEY
is an ``sk_test`` key, so it can never mutate a live Stripe account. It creates
(or reuses):

1. a Billing Meter for ``GLITCHTIP_OVERAGE_METER_EVENT_NAME``;
2. an overage product (metadata ``product_type=overage``) with a graduated,
   metered price built from ``GLITCHTIP_OVERAGE_TIERS``;
3. a base "Small" hosted plan, if no hosted plan exists yet, so there's
   something to attach overage to during local end-to-end testing.

Then it syncs everything into the local DB via ``sync_stripe_models``.

``--verify`` instead runs read-only checks and is allowed with a live key: it
confirms the meter, overage product, and metered price exist and that the live
tier schedule matches ``GLITCHTIP_OVERAGE_TIERS``. Live provisioning is done by
hand (this command won't mutate a live account), and the local cap math trusts
the settings schedule — drift between the two can bill an org past its
advertised spend cap, so verify after any manual change.
"""

import json
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stripe.client import (
    create_meter,
    create_metered_price,
    create_product,
    find_meter,
    list_prices,
    list_products,
    stripe_get,
    stripe_post,
)
from apps.stripe.maintenance import sync_stripe_models


async def _find_product_by_type(product_type: str):
    async for page in list_products():
        for product in page:
            if product.metadata.get("product_type", "").lower() == product_type:
                return product
    return None


async def _find_metered_price(product_id: str, meter_id: str):
    async for page in list_prices():
        for price in page:
            recurring = price.recurring or {}
            if price.product == product_id and recurring.get("meter") == meter_id:
                return price
    return None


def _tier_problems(raw_price: dict) -> list[str]:
    """Differences between a Stripe price's tiers and GLITCHTIP_OVERAGE_TIERS."""
    problems = []
    if raw_price.get("tiers_mode") != "graduated":
        problems.append(
            f"tiers_mode is {raw_price.get('tiers_mode')!r}, expected 'graduated'."
        )
    if raw_price.get("currency") != "usd":
        problems.append(f"currency is {raw_price.get('currency')!r}, expected 'usd'.")
    tiers = raw_price.get("tiers") or []
    expected = [
        (up_to, Decimal(rate) * 100) for up_to, rate in settings.GLITCHTIP_OVERAGE_TIERS
    ]
    actual = [
        (tier.get("up_to"), Decimal(tier.get("unit_amount_decimal") or "0"))
        for tier in tiers
    ]
    if len(actual) != len(expected):
        problems.append(
            f"{len(actual)} tiers in Stripe vs {len(expected)} in settings."
        )
    for i, ((up_to, cents), (live_up_to, live_cents)) in enumerate(
        zip(expected, actual)
    ):
        if up_to != live_up_to or cents != live_cents:
            problems.append(
                f"tier {i}: Stripe has (up_to={live_up_to}, {live_cents} cents/unit),"
                f" settings has (up_to={up_to}, {cents} cents/unit)."
            )
    for i, tier in enumerate(tiers):
        # Local cost/cap math is per-unit only; a flat amount would invoice
        # beyond what units_for_budget accounts for.
        if tier.get("flat_amount") or tier.get("flat_amount_decimal"):
            problems.append(f"tier {i} has a flat_amount; only per-unit is supported.")
    return problems


async def _verify(stdout) -> None:
    """Read-only checks that the Stripe account matches settings (live-safe)."""
    event_name = settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME

    meter = await find_meter(event_name)
    if meter is None:
        raise CommandError(f"No active Billing Meter with event_name {event_name!r}.")
    stdout(f"Meter {meter.id} ({event_name})")

    product = await _find_product_by_type("overage")
    if product is None:
        raise CommandError("No product with metadata product_type=overage.")
    stdout(f"Overage product {product.id}")

    problems = []
    if product.metadata.get("is_public", "").lower() == "true":
        problems.append(
            f"Product {product.id} has is_public=true; the overage product "
            "must not appear in the public plan list."
        )

    price = await _find_metered_price(product.id, meter.id)
    if price is None:
        raise CommandError(
            f"No metered price on product {product.id} bound to meter {meter.id}."
        )
    stdout(f"Metered price {price.id}")

    raw_price = json.loads(
        await stripe_get(f"prices/{price.id}", {"expand": ["tiers"]})
    )
    problems += _tier_problems(raw_price)
    if problems:
        raise CommandError("\n".join(problems))


async def _provision(stdout) -> None:
    # Defense in depth: this function creates real Stripe objects, so guard the
    # test-key check here too, not only at the command entry point.
    if not (settings.STRIPE_SECRET_KEY or "").startswith("sk_test"):
        raise CommandError("Refusing to provision against a non-test Stripe key.")

    event_name = settings.GLITCHTIP_OVERAGE_METER_EVENT_NAME

    # 1. Billing Meter
    meter = await find_meter(event_name)
    if meter is None:
        meter = await create_meter("GlitchTip Overage Events", event_name)
        stdout(f"Created meter {meter.id} ({event_name})")
    else:
        stdout(f"Reusing meter {meter.id} ({event_name})")

    # 2. Overage product + graduated metered price
    product = await _find_product_by_type("overage")
    if product is None:
        product_id = await create_product(
            "GlitchTip Overage",
            metadata={"product_type": "overage"},
            description="Usage-based charges for events above your plan quota.",
        )
        stdout(f"Created overage product {product_id}")
    else:
        product_id = product.id
        stdout(f"Reusing overage product {product_id}")

    price = await _find_metered_price(product_id, meter.id)
    if price is None:
        price = await create_metered_price(
            product_id,
            meter.id,
            settings.GLITCHTIP_OVERAGE_TIERS,
            nickname="GlitchTip overage (graduated)",
        )
        await stripe_post(f"products/{product_id}", {"default_price": price.id})
        stdout(f"Created metered overage price {price.id}")
    else:
        stdout(f"Reusing metered overage price {price.id}")

    # 3. A base hosted plan to attach overage to (for local testing only).
    hosted = await _find_product_by_type("hosted")
    if hosted is None:
        base_id = await create_product(
            "GlitchTip Small (test)",
            metadata={
                "product_type": "hosted",
                "events": "100000",
                "is_public": "true",
            },
            description="Up to 100k events per month.",
        )
        base_price = await stripe_post(
            "prices",
            {
                "product": base_id,
                "currency": "usd",
                "unit_amount": 1500,
                "recurring[interval]": "month",
                "nickname": "GlitchTip Small (test)",
                "metadata[is_public]": "true",
            },
        )
        base_price_id = json.loads(base_price)["id"]
        await stripe_post(f"products/{base_id}", {"default_price": base_price_id})
        stdout(f"Created base hosted plan {base_id} / price {base_price_id}")
    else:
        stdout(f"Found existing hosted plan {hosted.id}")

    await sync_stripe_models()
    stdout("Synced Stripe products/prices/subscriptions into the database.")


class Command(BaseCommand):
    help = (
        "Provision (idempotently) the Stripe meter/product/price for overage billing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Read-only: check the meter, overage product, and tier schedule "
            "against settings without creating anything. Safe with a live key.",
        )

    def handle(self, *args, **options):
        if options["verify"]:
            async_to_sync(_verify)(lambda msg: self.stdout.write(msg))
            self.stdout.write(
                self.style.SUCCESS("Overage billing configuration verified.")
            )
            return
        key = settings.STRIPE_SECRET_KEY or ""
        if not key.startswith("sk_test"):
            raise CommandError(
                "Refusing to run: STRIPE_SECRET_KEY is not a test key (sk_test...). "
                "This command must only touch a Stripe test account."
            )
        async_to_sync(_provision)(lambda msg: self.stdout.write(msg))
        self.stdout.write(self.style.SUCCESS("Overage billing provisioned."))
